"""Terminate a synthetic coordinator at a selected boundary and inspect its resources."""

import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4

from nginx_recovery_ci import ROOT, production_ids, run
from legacy_transition_digest import logical_digest

sys.path.insert(0, str(ROOT))
from scripts.historical_transition_evidence import FINAL_HEAD, INITIAL_HEAD
from scripts.selfhost_backup import _hash_file, verify_integral_set, verify_transition_accounting


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
    serving = {role: token + "-" + role for role in ("proxy", "web", "worker")} if boundary == "https" else {}
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
            for role, name in serving.items():
                resources[role] = identity("container", name, token)
            child.kill()
            if child.wait(timeout=10) == 0:
                raise RuntimeError("Coordinator did not terminate abruptly")
            db = identity("container", container_name, token)
            network = identity("network", token, token)
            volume = identity("volume", volume_name, token)
            serving_ids = set()
            for role, name in serving.items():
                resource = identity("container", name, token)
                if (resource["Id"] != resources[role]["Id"]
                        or resource["Id"] + "|" + resource["Image"] != evidence["serving"][role]
                        or resource["HostConfig"]["PortBindings"]
                        or set(resource["NetworkSettings"]["Networks"]) != {token}):
                    raise RuntimeError("Unexpected HTTPS container after interruption")
                serving_ids.add(resource["Id"])
            if (db["Id"] != resources["container"]["Id"] or db["Id"] != evidence["container"]
                    or db["Image"] != evidence["postgres_image"] or network["Id"] != evidence["network"]
                    or not network["Internal"] or set(network["Containers"]) != {db["Id"], *serving_ids}
                    or db["HostConfig"]["PortBindings"] or volume != resources["volume"]
                    or not any(m["Name"] == volume_name and m["Destination"] == "/var/lib/postgresql/data"
                               for m in db["Mounts"] if m["Type"] == "volume")):
                raise RuntimeError("Synthetic resources changed across interruption")

            def query(database, statement):
                return run("docker", "exec", db["Id"], "psql", "-XAt", "-v", "ON_ERROR_STOP=1",
                           "-U", "j2m_test", "-d", database, "-c", statement)

            if serving:
                # Cut ingress first, then fence PostgreSQL and drain previously
                # authenticated sessions before inspecting the ledger.
                run("docker", "stop", resources["proxy"]["Id"])
                query("restore_test", "ALTER ROLE j2m_web NOLOGIN; ALTER ROLE j2m_worker NOLOGIN")
                run("docker", "stop", resources["web"]["Id"], resources["worker"]["Id"])
                query("restore_test", "SELECT pg_terminate_backend(pid, 3000) FROM pg_stat_activity "
                                      "WHERE usename IN ('j2m_web','j2m_worker')")
                if any(identity("container", name, token)["State"]["Running"] for name in serving.values()):
                    raise RuntimeError("HTTPS ingress or writer process remains active")
            if evidence["phase"] != boundary or evidence["checkout"] != run("git", "-C", str(ROOT), "rev-parse", "HEAD"):
                raise RuntimeError("Unexpected transition boundary or checkout")
            observed = query("j2m_test", "SELECT system_identifier FROM pg_control_system()")
            if observed != evidence["cluster"]:
                raise RuntimeError("Unexpected PostgreSQL cluster")
            expected_head = FINAL_HEAD if boundary in ("migrate", "verify", "https") else INITIAL_HEAD
            partial = boundary == "restore-partial"
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
                if partial and field == "restored":
                    if any(query(database, f"SELECT count(*) FROM public.{table}") != "0"
                           for table in ("alembic_version", "customers", "transactions", "loyalty_ledger_entries")):
                        raise RuntimeError("A partially restored database was mistaken for a complete backup")
                else:
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
            if serving and query("restore_test", "SELECT count(*) FROM pg_roles WHERE rolname IN "
                                 "('j2m_web','j2m_worker') AND ("
                                 "has_any_column_privilege(oid, 'public.customers', 'UPDATE') OR "
                                 "has_table_privilege(oid, 'public.transactions', 'INSERT') OR "
                                 "has_table_privilege(oid, 'public.loyalty_ledger_entries', 'INSERT'))") != "0":
                raise RuntimeError("Unexpected economic write privilege after HTTPS interruption")
            for role in ("j2m_web", "j2m_worker"):
                denied = subprocess.run(
                    ["docker", "exec", "-e", "PGPASSWORD=synthetic", db["Id"], "psql",
                     "-h", "127.0.0.1", "-XAt", "-U", role, "-d",
                     "restore_test" if has_copy else "j2m_test", "-c", "SELECT 1"],
                    capture_output=True, timeout=10)
                if denied.returncode == 0 or b"not permitted to log in" not in denied.stderr:
                    raise RuntimeError("Writer reconnect was not rejected by NOLOGIN")
            state = json.loads(journal.with_suffix(".jsonl.state").joinpath("transition.json").read_text())
            if (state["phase"], state["status"]) != ("restore" if partial else boundary, "pending"):
                raise RuntimeError("Expected unconfirmed transition stage in the coordinator journal")
            report = {**evidence, "volume": volume_name, "initial_head": INITIAL_HEAD,
                      "actual_head": expected_head if has_copy and not partial else None,
                      "partial_restore_verified": partial,
                      "coordinator_terminated": True, "source_accounting_verified": True,
                      "restored_accounting_verified": has_copy and not partial,
                      "logical_digest_verified": True, "writer_reconnect_denied": True,
                      "migration_replayed": False, "reopened": False,
                      "https_attested": False}
            (directory / "supervisor.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"Coordinator killed after {boundary}; same cluster, logical state and writer denial verified; no replay.")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            for role, name in serving.items():
                if run("docker", "container", "inspect", "--format", "{{.Id}}", name, check=False):
                    item = identity("container", name, token)
                    if role in resources and resources[role]["Id"] != item["Id"]:
                        raise RuntimeError("Unexpected HTTPS container during cleanup")
                    run("docker", "rm", "-f", item["Id"])
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


def recover_verified_source(directory, token, network, pooler, image, checkout):
    """Reattest the same fenced source and restore one verified admission path."""
    from postgres_full_schema_fixture import (
        SIMULATED_BACKUP_TASKS, attest_fenced_origin, run_simulated_backup_task,
    )
    from postgres_scoped_grants_fixture import prove_scoped_grants_and_fence
    from scripts.selfhost_backup import exclusive_lock

    marker = directory / "tasks-suspended.json"
    marker_contents = marker.read_text(encoding="utf-8")
    lock = directory / ".operations.lock"

    def client(role, database, host, port):
        password = f"synthetic-{role}" if role.startswith("j2m_schema_") else "synthetic-only"
        return ["docker", "exec", "-i", token, "env", f"PGPASSWORD={password}", "psql",
                "-XqAt", "-v", "ON_ERROR_STOP=1", "-h", host, "-p", str(port), "-U", role, "-d", database]

    def sql(statement, role="postgres", database="full_schema"):
        return subprocess.run([*client(role, database, "127.0.0.1", 5432), "-c", statement],
                              capture_output=True, timeout=15)

    def connection(role, database, *, direct=False):
        return subprocess.Popen(client(role, database, "127.0.0.1" if direct else pooler, 5432 if direct else 6432),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def pool_control(statement):
        return subprocess.run([*client("j2m_pool_admin", "pgbouncer", pooler, 6432), "-c", statement],
                              capture_output=True, timeout=15)

    def service_flow(role, source):
        filename = "WEB.env" if role == "j2m_schema_web" else "WORKER.env"
        run("docker", "run", "--rm", "--network", network, "--env-file", str(directory / filename),
            "--mount", f"type=bind,src={directory / 'ca.crt'},dst=/run/j2m/supabase-ca.pem,readonly",
            image, "python", "-c", source)

    def assert_fenced():
        for role in ("j2m_schema_web", "j2m_schema_worker"):
            result = sql("SELECT 1", role)
            if not result.returncode or b"permission denied for database" not in result.stderr:
                raise RuntimeError("Service was admitted during grant preparation")
            with connection(role, "full_schema") as peer:
                _, stderr = peer.communicate(b"SELECT 1;\n", timeout=8)
                if not peer.returncode or b"disabled" not in stderr.lower():
                    raise RuntimeError("Pool was admitted during grant preparation")

    started = time.monotonic()
    try:
        with exclusive_lock(lock):
            assert_fenced()
            attest_fenced_origin(network, directory / "ca.crt", image, checkout, sql, nominal=True)
            prove_scoped_grants_and_fence(sql, "full_schema", service_flow, connection, pool_control,
                                         nominal=True, before_open=assert_fenced, recovery_only=True)
            attest_fenced_origin(network, directory / "ca.crt", image, checkout, sql, reopened=True)
        marker.unlink()
        for task in SIMULATED_BACKUP_TASKS:
            run_simulated_backup_task(lock, marker, task).check_returncode()
    except BaseException:
        if not marker.exists():
            marker.write_text(marker_contents, encoding="utf-8")
        pool_control("DISABLE full_schema").check_returncode()
        sql("REVOKE CONNECT ON DATABASE full_schema FROM PUBLIC,j2m_schema_web,j2m_schema_worker; "
            "SELECT pg_terminate_backend(pid, 3000) FROM pg_stat_activity "
            "WHERE datname=current_database() AND usename IN ('j2m_schema_web','j2m_schema_worker')").check_returncode()
        assert_fenced()
        raise
    return round(time.monotonic() - started, 2)


def supervise_migrated_role_source(directory, boundary="migrate"):
    """Kill the actual restricted-backup coordinator and attest the live fenced source."""
    from postgres_full_schema_fixture import SIMULATED_BACKUP_TASKS, run_simulated_backup_task

    directory.mkdir(mode=0o700, exist_ok=False)
    token = "j2m-roles-" + uuid4().hex[:8]
    network, pooler = token + "-internal", token + "-pooler"
    checkpoint = directory / "origin-checkpoint.json"
    checkout = run("git", "rev-parse", "HEAD")
    candidate = "j2m-interactive:transition-" + checkout
    image = run("docker", "image", "inspect", "--format", "{{.Id}}", candidate)
    before = production_ids()
    env = dict(os.environ, J2M_ROLE_SUPERVISION=str(checkpoint), J2M_ROLE_SYNTHETIC_TOKEN=token,
               J2M_ROLE_FIXTURE_IMAGE=candidate, J2M_NOMINAL_TRANSITION="1", J2M_ROLE_BOUNDARY=boundary)
    resources = (("container", token), ("container", pooler), ("network", network))
    auxiliary_ids = {}
    expected_head = INITIAL_HEAD if boundary in {"fence", "restore", "restore-partial"} else FINAL_HEAD
    with (directory / "origin-coordinator.log").open("wb") as log:
        child = subprocess.Popen([sys.executable, "-u", "tests/postgres_role_separation_ci.py"],
                                 cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while not checkpoint.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.2)
            if not checkpoint.exists():
                raise RuntimeError(f"Restricted-backup coordinator did not reach {boundary}")
            evidence = json.loads(checkpoint.read_text(encoding="utf-8"))
            observed = {name: identity(kind, name, token) for kind, name in resources}
            docker_network = observed[network]
            auxiliary = evidence["auxiliary"]
            expected_auxiliary = ({token + "-attest-" + role for role in ("web", "worker", "proxy")}
                                  if boundary == "https" else set())
            if boundary == "restore-partial":
                if len(auxiliary) != 1 or not all(re.fullmatch(r"j2m-restore-[0-9a-f]{12}", name) for name in auxiliary):
                    raise RuntimeError("Unverified partial restore container")
                expected_auxiliary = set(auxiliary)
            if auxiliary.keys() != expected_auxiliary:
                raise RuntimeError("Unexpected auxiliary resources")
            for name, identifier in auxiliary.items():
                resource = json.loads(run("docker", "inspect", name))[0]
                expected_network = "none" if boundary == "restore-partial" else network
                if (resource["Id"] != identifier or resource["HostConfig"]["PortBindings"]
                        or set(resource["NetworkSettings"]["Networks"]) != {expected_network}):
                    raise RuntimeError("Unverified auxiliary identity")
                auxiliary_ids[name] = identifier
            expected_network_ids = {observed[token]["Id"], observed[pooler]["Id"]}
            if boundary == "https":
                expected_network_ids.update(auxiliary.values())
            if (not docker_network["Internal"] or set(docker_network["Containers"]) != expected_network_ids
                    or any(observed[name]["HostConfig"]["PortBindings"] for name in (token, pooler))
                    or evidence["checkout"] != checkout
                    or (evidence["phase"], evidence["status"], evidence["database"],
                        evidence["network"], evidence["image"]) != (boundary, "pending", "full_schema", network, image)):
                raise RuntimeError("Unverified source checkpoint identity")
            child.kill()
            child.wait(timeout=10)
            for kind, name in resources:
                current = identity(kind, name, token)
                if (current["Id"] != observed[name]["Id"] or
                        (kind == "container" and current["Mounts"] != observed[name]["Mounts"])):
                    raise RuntimeError("Source resource changed after coordinator death")

            def query(statement):
                return run("docker", "exec", token, "psql", "-XAt", "-v", "ON_ERROR_STOP=1",
                           "-U", "postgres", "-d", "full_schema", "-c", statement)

            if (query("SELECT system_identifier, (SELECT oid FROM pg_database "
                      "WHERE datname=current_database()) FROM pg_control_system()")
                    != f"{evidence['cluster']}|{evidence['database_oid']}"
                    or query("SELECT version_num FROM alembic_version") != expected_head
                    or logical_digest(token, "full_schema", user="postgres") != evidence["digest"]):
                raise RuntimeError("Source cluster, revision or digest changed")
            verify_transition_accounting(query)
            if json.loads(checkpoint.read_text(encoding="utf-8")) != evidence:
                raise RuntimeError("Pending journal changed after coordinator death")
            indexes = list((directory / "restricted-backups").glob("j2m-set-*.j2mbackup"))
            if boundary in {"fence", "restore-partial"}:
                if evidence["index"] is not None or indexes:
                    raise RuntimeError("A checkpoint without a completed backup exposed an index")
                if boundary == "restore-partial":
                    restored = next(iter(auxiliary_ids.values()))
                    if run("docker", "exec", restored, "psql", "-XAt", "-U", "j2m_restore", "-d", "j2m_restore",
                           "-c", "SELECT version_num FROM alembic_version") != INITIAL_HEAD:
                        raise RuntimeError("Incomplete restore did not preserve the expected legacy revision")
            else:
                index = directory / "restricted-backups" / Path(evidence["index"]).name
                with patch.dict(os.environ, {"J2M_SYNTHETIC_BACKUP_TEST": "1"}), patch(
                    "scripts.selfhost_backup.load_dpapi_key", return_value=bytes(range(32))
                ):
                    if indexes != [index] or _hash_file(index) != evidence["index_sha256"] or not verify_integral_set(
                        index, directory / "synthetic-key"
                    ):
                        raise RuntimeError("Authenticated backup index changed")
            for role in ("j2m_schema_web", "j2m_schema_worker"):
                for host, port, message in (("127.0.0.1", "5432", b"permission denied for database"),
                                            (pooler, "6432", b"disabled")):
                    check = subprocess.run(
                        ["docker", "exec", token, "env", f"PGPASSWORD=synthetic-{role}", "psql", "-XAt",
                         "-h", host, "-p", port, "-U", role, "-d", "full_schema", "-c", "SELECT 1"],
                        capture_output=True, timeout=10,
                    )
                    if not check.returncode or message not in check.stderr.lower():
                        raise RuntimeError("Expected writer denial after coordinator death")
            for task in SIMULATED_BACKUP_TASKS:
                result = run_simulated_backup_task(directory / ".operations.lock",
                                                   directory / "tasks-suspended.json", task)
                if result.returncode != 75:
                    raise RuntimeError("A synthetic task escaped durable suspension")
            replay_probe = """
import os
from run import app
from app.extensions import db
from scripts.run_certified_migration import rehearse_upgrade

with app.app_context():
    try:
        rehearse_upgrade(db.engine, app.config, environment=os.environ)
    except RuntimeError as error:
        if str(error) != "Source revision no longer eligible":
            raise
    else:
        raise AssertionError("Migration replay accepted")
"""
            if expected_head == FINAL_HEAD:
                replay = subprocess.run(
                    ["docker", "run", "--rm", "--network", network, "--env-file", str(directory / "MIGRATOR.env"),
                     "-e", "J2M_PROCESS_TYPE=migrator", "--mount",
                     f"type=bind,src={directory / 'ca.crt'},dst=/run/j2m/supabase-ca.pem,readonly",
                     image, "python", "-c", replay_probe], capture_output=True, timeout=45,
                )
                if replay.returncode or logical_digest(token, "full_schema", user="postgres") != evidence["digest"]:
                    raise RuntimeError("Migration replay was accepted or changed the source")
            recovery_seconds = (recover_verified_source(directory, token, network, pooler, image, checkout)
                                if boundary == "verify" else None)
            report = {**evidence, "container": observed[token]["Id"], "pooler": observed[pooler]["Id"],
                      "network_id": docker_network["Id"], "postgres_image": observed[token]["Image"],
                      "pooler_image": observed[pooler]["Image"],
                      "volumes": [mount["Name"] for mount in observed[token]["Mounts"] if mount["Type"] == "volume"],
                      "coordinator_terminated": True, "tasks_suspended": True,
                      "actual_head": expected_head, "writer_reconnect_denied": True,
                      "source_accounting_verified": True, "migration_replayed": False,
                      "reopened": recovery_seconds is not None, "recovery_seconds": recovery_seconds}
            (directory / "supervisor.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"Killed the restricted-backup coordinator at {boundary}; same source, digest and writer denial verified.")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            for name, identifier in auxiliary_ids.items():
                if run("docker", "inspect", "--format", "{{.Id}}", name, check=False) == identifier:
                    run("docker", "rm", "-f", identifier)
            for kind, name in resources:
                if run("docker", kind, "inspect", "--format", "{{.Id}}", name, check=False):
                    resource = identity(kind, name, token)
                    run("docker", kind, "rm", *(["-f"] if kind == "container" else []), resource["Id"])
            if production_ids() != before:
                raise RuntimeError("Operational container IDs changed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconstructed-legacy", action="store_true")
    parser.add_argument("--migrated-role-source", action="store_true")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--boundary", choices=("fence", "restore", "restore-partial", "migrate", "verify", "https", "reopen"), default="migrate")
    options = parser.parse_args()
    if options.migrated_role_source:
        if options.evidence_dir is None:
            parser.error("--migrated-role-source requires --evidence-dir")
        supervise_migrated_role_source(options.evidence_dir.resolve(), options.boundary)
    elif options.boundary == "reopen":
        parser.error("reopen requires --migrated-role-source")
    elif options.evidence_dir:
        exercise(options.evidence_dir, reconstructed=options.reconstructed_legacy, boundary=options.boundary)
    else:
        with tempfile.TemporaryDirectory() as folder:
            exercise(Path(folder) / "supervisor", reconstructed=options.reconstructed_legacy, boundary=options.boundary)
