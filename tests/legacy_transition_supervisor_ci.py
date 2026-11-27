"""Terminate a synthetic coordinator at a selected boundary and inspect its resources."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from nginx_recovery_ci import ROOT, production_ids, run
from legacy_transition_digest import logical_digest

sys.path.insert(0, str(ROOT))
from scripts.historical_transition_evidence import FINAL_HEAD, INITIAL_HEAD
from scripts.selfhost_backup import verify_transition_accounting


def identity(kind, name, token):
    resource = json.loads(run("docker", kind, "inspect", name))[0]
    labels = resource["Config"]["Labels"] if kind == "container" else resource["Labels"]
    if labels.get("j2m.synthetic.transition") != token:
        raise RuntimeError("Unexpected recovery resource label")
    return resource


def exercise(directory, *, reconstructed=False, boundary='migrate'):
    directory.mkdir(mode=0o700, exist_ok=False)
    nonce = uuid4().hex
    token = "j2m-legacy-tls-" + nonce
    container_name = token + "-db"
    volume_name = token + "-pgdata"
    journal = directory / "evidence.jsonl"
    process_output = directory / "coordinator.log"
    args = [sys.executable, str(ROOT / "tests/legacy_wallet_transition_docker.py"),
            "--journal", str(journal), "--supervised-run", nonce, "--interrupt-at", boundary]
    if reconstructed:
        args.append("--reconstructed-legacy")
    original = production_ids()
    resources = {}
    with process_output.open("wb") as stream:
        child = subprocess.Popen(args, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        try:
            evidence = None
            deadline = time.monotonic() + 150
            while child.poll() is None and time.monotonic() < deadline:
                for line in process_output.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("J2M_TRANSITION_BOUNDARY "):
                        evidence = json.loads(line.partition(" ")[2])
                        break
                if evidence is not None:
                    break
                time.sleep(0.25)
            if evidence is None:
                raise RuntimeError("Synthetic transition boundary was not reached")
            for kind, name in (("container", container_name), ("network", token), ("volume", volume_name)):
                resources[kind] = identity(kind, name, token)
            child.kill()
            if child.wait(timeout=10) == 0:
                raise RuntimeError("Coordinator did not terminate abruptly")
            db = identity("container", container_name, token)
            network = identity("network", token, token)
            volume = identity("volume", volume_name, token)
            if (db["Id"] != resources["container"]["Id"] or db["Id"] != evidence["container"]
                    or db["Image"] != evidence["postgres_image"] or network["Id"] != evidence["network"]
                    or not network["Internal"] or set(network["Containers"]) != {db["Id"]}
                    or db["HostConfig"]["PortBindings"] or volume != resources["volume"]
                    or not any(m["Name"] == volume_name and m["Destination"] == "/var/lib/postgresql/data"
                               for m in db["Mounts"] if m["Type"] == "volume")):
                raise RuntimeError("Synthetic resources changed across interruption")

            def query(database, statement):
                return run("docker", "exec", db["Id"], "psql", "-XAt", "-v", "ON_ERROR_STOP=1",
                           "-U", "j2m_test", "-d", database, "-c", statement)

            if evidence["phase"] != boundary or evidence["checkout"] != run("git", "-C", str(ROOT), "rev-parse", "HEAD"):
                raise RuntimeError("Unexpected transition boundary or checkout")
            observed = query("j2m_test", "SELECT system_identifier FROM pg_control_system()")
            if observed != evidence["cluster"]:
                raise RuntimeError("Unexpected PostgreSQL cluster")
            expected_head = FINAL_HEAD if boundary in ("migrate", "verify") else INITIAL_HEAD
            has_copy = boundary != "fence"
            databases = [("source", "j2m_test", INITIAL_HEAD)]
            if has_copy:
                databases.append(("restored", "restore_test", expected_head))
            elif query("j2m_test", "SELECT count(*) FROM pg_database WHERE datname='restore_test'") != "0":
                raise RuntimeError("Restore unexpectedly exists before restore stage")
            for field, database, revision in databases:
                if query(database, "SELECT current_database(), oid FROM pg_database "
                                   "WHERE datname=current_database()") != evidence[field]:
                    raise RuntimeError("Unexpected database identity")
                if query(database, "SELECT version_num FROM alembic_version") != revision:
                    raise RuntimeError("Unexpected database revision after interruption")
                verify_transition_accounting(lambda statement: query(database, statement))
                if logical_digest(db["Id"], database) != evidence[field + "_digest"]:
                    raise RuntimeError("Logical state changed across coordinator termination")
                if query(database, "SELECT count(*) FROM pg_stat_activity "
                                   "WHERE datname=current_database() AND pid<>pg_backend_pid()") != "0":
                    raise RuntimeError("Unexpected connection after interruption")
            if boundary == "restore" and evidence["source_digest"] != evidence["restored_digest"]:
                raise RuntimeError("Restored snapshot differs from source")
            if query("j2m_test", "SELECT count(*) FROM pg_roles WHERE rolname IN "
                                 "('j2m_web','j2m_worker') AND NOT rolcanlogin AND NOT rolsuper "
                                 "AND NOT rolbypassrls") != "2":
                raise RuntimeError("Writer identities are not fenced")
            for role in ("j2m_web", "j2m_worker"):
                denied = subprocess.run(
                    ["docker", "exec", "-e", "PGPASSWORD=synthetic", db["Id"], "psql",
                     "-h", "127.0.0.1", "-XAt", "-U", role, "-d",
                     "restore_test" if has_copy else "j2m_test", "-c", "SELECT 1"],
                    capture_output=True, timeout=10)
                if denied.returncode == 0 or b"not permitted to log in" not in denied.stderr:
                    raise RuntimeError("Writer reconnect was not rejected by NOLOGIN")
            state = json.loads(journal.with_suffix(".jsonl.state").joinpath("transition.json").read_text())
            if (state["phase"], state["status"]) != (boundary, "pending"):
                raise RuntimeError("Expected unconfirmed transition stage in the coordinator journal")
            report = {**evidence, "volume": volume_name, "initial_head": INITIAL_HEAD,
                      "actual_head": expected_head if has_copy else None,
                      "coordinator_terminated": True, "accounting_verified": True,
                      "logical_digest_verified": True, "writer_reconnect_denied": True,
                      "migration_replayed": False, "reopened": False}
            (directory / "supervisor.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"Coordinator killed after {boundary}; same cluster, logical state and writer denial verified; no replay.")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            for kind, name in (("container", container_name), ("volume", volume_name), ("network", token)):
                if not run("docker", kind, "inspect", "--format", "{{.Name}}", name, check=False):
                    continue
                current = identity(kind, name, token)
                key = "Name" if kind == "volume" else "Id"
                if kind in resources and current[key] != resources[kind][key]:
                    raise RuntimeError("Unexpected identity during synthetic cleanup")
                run("docker", kind, "rm", *(["-f"] if kind == "container" else []), name)
            if production_ids() != original:
                raise RuntimeError("Operational container IDs changed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconstructed-legacy", action="store_true")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--boundary", choices=("fence", "restore", "migrate", "verify"), default="migrate")
    options = parser.parse_args()
    if options.evidence_dir:
        exercise(options.evidence_dir, reconstructed=options.reconstructed_legacy, boundary=options.boundary)
    else:
        with tempfile.TemporaryDirectory() as folder:
            exercise(Path(folder) / "supervisor", reconstructed=options.reconstructed_legacy, boundary=options.boundary)
