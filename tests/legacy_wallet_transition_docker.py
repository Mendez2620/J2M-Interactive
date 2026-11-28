"""Laptop-only synthetic transition using the original historical container image."""
import argparse
import re
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from legacy_transition_digest import logical_digest as snapshot_digest
from postgres_full_schema_fixture import BACKUP_GRANTS
from nginx_recovery_ci import NGINX_IMAGE, nonce_https_request, production_ids, proxy_config, run, write_certificates

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.isolated_transition import Phase, rehearse
from scripts.historical_transition_evidence import (Backend, FINAL_HEAD, INITIAL_HEAD, LEGACY_SHA,
                                                    TransitionJournal, prove_https, verify_record)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--journal", type=Path)
parser.add_argument("--preflight-only", action="store_true")
parser.add_argument("--supervised-run")
parser.add_argument("--interrupt-at", choices=("fence", "restore", "restore-partial", "migrate", "verify", "https"), default="migrate")
parser.add_argument("--reconstructed-legacy", action="store_true")
args = parser.parse_args()
if not args.preflight_only and args.journal is None:
    parser.error("--journal is required for the synthetic rehearsal")
sha = run("git", "-C", str(ROOT), "rev-parse", "HEAD")
if run("git", "-C", str(ROOT), "status", "--porcelain"):
    raise RuntimeError("Rehearsal requires a clean source tree")
legacy_tag = "j2m-interactive:" + LEGACY_SHA
if args.reconstructed_legacy:
    legacy_tag = "j2m-interactive:reconstructed-" + LEGACY_SHA
target_tag = "j2m-interactive:transition-" + sha


def image(tag):
    return run("docker", "image", "inspect", "--format", "{{.Id}}", tag)


img, new = image(legacy_tag), image(target_tag)
if args.reconstructed_legacy:
    reconstructed_revision = run("docker", "image", "inspect", "--format",
                                 '{{index .Config.Labels "org.opencontainers.image.revision"}}', img)
    if reconstructed_revision != LEGACY_SHA:
        raise RuntimeError("Reconstructed legacy image lacks the expected revision label")
label = run("docker", "image", "inspect", "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}', new)
if label != sha or img == new:
    raise RuntimeError("Expected the original image and an independent build of this source")
if shutil.disk_usage(tempfile.gettempdir()).free < 2 * 1024**3:
    raise RuntimeError("Insufficient free disk space for isolated recovery")
if args.preflight_only:
    print("Read-only preflight passed. No Docker resources changed.")
    raise SystemExit(0)

r = run
before = production_ids()
if args.supervised_run and not re.fullmatch("[0-9a-f]{32}", args.supervised_run):
    parser.error("Invalid synthetic run identifier")
p = "j2m-legacy-tls-" + (args.supervised_run or uuid.uuid4().hex)
volume = p + "-pgdata"
label = f"j2m.synthetic.transition={p}"
n = {role: p + "-" + role for role in ("db", "web", "worker", "proxy")}
root = ROOT
SOURCE = "j2m_test"
RESTORED = "restore_test"
source_digest = None
journal = TransitionJournal(args.journal, legacy_image=img, target_image=new, target_sha=sha,
                            environment="synthetic")
probe=(root/"tests/legacy_wallet_fixture.py").read_text(encoding="utf-8")
def config(database, role="j2m_test"):
    if database not in {SOURCE, RESTORED}:
        raise RuntimeError("Unexpected synthetic database")
    return ["--network", p, "--env-file", str(root / ".env.wallet-demo.example"),
            "-e", f"DATABASE_URL=postgresql://{role}:synthetic@db/{database}",
            "-e", "GOOGLE_WALLET_MODE=disabled", "-e", "APPLE_WALLET_MODE=disabled"]


def exercise(stage, selected=img):
    database = SOURCE if stage == "before" else RESTORED
    subprocess.run(["docker", "run", "--rm", "-i", *config(database), selected,
                    "python", "-", stage], input=probe, text=True, check=True, timeout=60)


def start(role, selected=img):
    cmd=() if role=="web" else ("flask","--app","run.py","run-google-wallet-sync-worker")
    process="web" if role=="web" else "google-wallet-worker"
    r("docker","run","-d","--name",n[role],"--network-alias",role,"--label",label,"--label",f"com.docker.compose.project={p}","--label",f"com.docker.compose.service={role}",*config(RESTORED, "j2m_" + role),"-e",f"J2M_PROCESS_TYPE={process}","-e",f"J2M_RELEASE_IMAGE=j2m-interactive:{sha if selected == new else ''}",selected,*cmd)
def attest(folder, selected=img):
    def snapshot():
        ids = {role: json.loads(r("docker", "inspect", name))[0] for role, name in n.items()}
        network = json.loads(r("docker", "network", "inspect", p))[0]
        if not network["Internal"] or set(network["Containers"]) != {obj["Id"] for obj in ids.values()}:
            raise RuntimeError("Unexpected isolated network membership")
        for role in ("web", "worker"):
            obj = ids[role]
            labels = obj["Config"]["Labels"]
            details = obj["NetworkSettings"]["Networks"]
            if (obj["Image"] != selected or not obj["State"]["Running"] or set(details) != {p}
                    or labels["com.docker.compose.project"] != p or labels["com.docker.compose.service"] != role
                    or f"DATABASE_URL=postgresql://j2m_{role}:synthetic@db/{RESTORED}" not in obj["Config"]["Env"]):
                raise RuntimeError("Unexpected Docker backend identity")
        web = ids["web"]
        return Backend(web["Image"], web["Id"], web["NetworkSettings"]["Networks"][p]["IPAddress"])

    for role in ("web", "worker"):
        r("docker", "exec", n[role], "python", "scripts/container_healthcheck.py")

    def request(route, nonce):
        if route == "ready":
            interrupt_boundary("https")
        return nonce_https_request(p, folder, new, route, nonce)

    return prove_https(snapshot, request, lambda: r("docker", "logs", n["proxy"]),
                       revision=sha if selected == new else None)


def sql(database, statement):
    return r("docker", "exec", n["db"], "psql", "-XAt", "-v", "ON_ERROR_STOP=1",
             "-U", "j2m_test", "-d", database, "-c", statement)


def logical_digest(database):
    return snapshot_digest(n["db"], database)


def restore_fixture():
    global source_digest
    db_args = ["docker", "exec", n["db"]]
    source_digest = logical_digest(SOURCE)
    denied = subprocess.run(["docker", "exec", "-e", "PGPASSWORD=synthetic", n["db"],
                             "psql", "-h", "127.0.0.1", "-XAt", "-v", "ON_ERROR_STOP=1",
                             "-U", "j2m_backup", "-d", SOURCE, "-c",
                             "UPDATE public.customers SET current_stamps=99"],
                            capture_output=True, timeout=15)
    if not denied.returncode or b"permission denied" not in denied.stderr:
        raise RuntimeError("Synthetic backup role is not read-only")
    archive = subprocess.check_output(
        ["docker", "exec", "-e", "PGPASSWORD=synthetic", n["db"], "pg_dump", "-h", "127.0.0.1",
         "-U", "j2m_backup", "--enable-row-security", "--no-owner", "--no-acl", "-Fc", SOURCE],
        timeout=25,
    )
    if not archive.startswith(b"PGDMP") or len(archive) > 8 * 1024 * 1024:
        raise RuntimeError("Invalid synthetic snapshot")
    r(*db_args, "createdb", "-U", "j2m_test", "-O", "j2m_transition_owner", RESTORED)
    command = ["docker", "exec", "-i", n["db"], "pg_restore", "--exit-on-error",
               "--no-owner", "--no-acl", "--role=j2m_transition_owner", "-U", "j2m_test", "-d", RESTORED]
    if args.supervised_run and args.interrupt_at == "restore-partial":
        # Commit only the DDL section; do not load rows, sequences or constraints.
        subprocess.run([*command, "--section=pre-data"], input=archive, check=True, timeout=25)
        interrupt_boundary("restore-partial")
    subprocess.run(command, input=archive, check=True, timeout=25)
    if logical_digest(RESTORED) != source_digest or logical_digest(SOURCE) != source_digest:
        raise RuntimeError("Synthetic logical restore did not match its source")
    journal.advance("restored", sha256=hashlib.sha256(archive).hexdigest(),
                    logical_sha256=source_digest, revision=INITIAL_HEAD,
                    source=database_identity(SOURCE), restored=database_identity(RESTORED))


def database_identity(database):
    identity = sql(database, "SELECT current_database(), oid FROM pg_database WHERE datname=current_database()")
    if not identity.startswith(database + "|"):
        raise RuntimeError("Unexpected synthetic connection target")
    return identity


def guard_images():
    if image(legacy_tag) != img or image(target_tag) != new:
        raise RuntimeError("A transition image tag changed")


def provision():
    journal.advance("preflight")
    r("docker", "network", "create", "--internal", "--label", label, p)
    r("docker", "volume", "create", "--label", label, volume)
    r("docker","run","-d","--name",n["db"],"--network",p,"--network-alias","db","--label",label,"--mount",f"type=volume,src={volume},dst=/var/lib/postgresql/data,volume-nocopy","-e","POSTGRES_USER=j2m_test","-e","POSTGRES_DB=j2m_test","-e","POSTGRES_PASSWORD=synthetic","postgres:17-alpine")
    time.sleep(4)
    r("docker","run","--rm",*config(SOURCE),new,"flask","--app","run.py","db","upgrade",INITIAL_HEAD)
    exercise("before")
    sql(SOURCE, """
        CREATE ROLE j2m_transition_owner NOLOGIN;
        CREATE ROLE j2m_migrator LOGIN NOINHERIT PASSWORD 'synthetic';
        GRANT j2m_transition_owner TO j2m_migrator;
        CREATE ROLE j2m_web NOLOGIN PASSWORD 'synthetic';
        CREATE ROLE j2m_worker NOLOGIN PASSWORD 'synthetic';
        CREATE ROLE j2m_backup LOGIN PASSWORD 'synthetic';
        GRANT CONNECT ON DATABASE j2m_test TO j2m_backup;
        REVOKE CONNECT ON DATABASE j2m_test FROM PUBLIC;
    """)
    sql(SOURCE, BACKUP_GRANTS)


def assert_drained():
    guard_images()
    # The seed container has already exited; only PostgreSQL may be attached.
    network = json.loads(r("docker", "network", "inspect", p))[0]
    if {value["Name"] for value in network["Containers"].values()} != {n["db"]}:
        raise RuntimeError("Synthetic writers not drained")


def restore():
    restore_fixture()
    journal.advance("maintenance")


def migrate():
    r("docker", "run", "--rm", *config(RESTORED, "j2m_migrator"),
      "-e", "PGOPTIONS=-c role=j2m_transition_owner", new,
      "flask", "--app", "run.py", "db", "upgrade", FINAL_HEAD)


def interrupt_boundary(phase):
    if not args.supervised_run or phase != args.interrupt_at:
        return
    has_copy = phase != Phase.FENCE.value
    evidence = {
        "phase": phase,
        "container": r("docker", "inspect", "--format", "{{.Id}}", n["db"]),
        "network": r("docker", "network", "inspect", "--format", "{{.Id}}", p),
        "postgres_image": r("docker", "inspect", "--format", "{{.Image}}", n["db"]),
        "cluster": sql(SOURCE, "SELECT system_identifier FROM pg_control_system()"),
        "source": database_identity(SOURCE),
        "restored": database_identity(RESTORED) if has_copy else None,
        "source_digest": logical_digest(SOURCE),
        "restored_digest": logical_digest(RESTORED) if has_copy else None,
        "checkout": sha,
    }
    if phase == "https":
        evidence["serving"] = {
            role: r("docker", "inspect", "--format", "{{.Id}}|{{.Image}}", n[role])
            for role in ("web", "worker", "proxy")
        }
    if source_digest is not None and evidence["source_digest"] != source_digest:
        raise RuntimeError("Protected source changed before interruption")
    print("J2M_TRANSITION_BOUNDARY " + json.dumps(evidence), flush=True)
    while True:
        time.sleep(10)


def verify_migration():
    schema_check="from run import app; from app.extensions import db; from scripts.check_wallet_referral_schema import validate_schema; a=app.app_context(); a.push(); raise SystemExit(bool(validate_schema(db.session.connection())));"
    r("docker","run","--rm",*config(RESTORED),new,"python","-c",schema_check)
    reserve = """
INSERT INTO google_wallet_write_reservations
(operation_id, owner_token, issuer, kind, resource_id, business_id, customer_id, state, active_slot, updated_at)
VALUES (repeat('a',32), repeat('b',32), 'synthetic', 'loyaltyObject', 'synthetic.object', 1, 1, 'uncertain', 1, now());
"""
    r("docker","exec",n["db"],"psql","-U","j2m_test","-d",RESTORED,"-v","ON_ERROR_STOP=1","-c",reserve)
    journal.advance("migrated", revision=FINAL_HEAD)
    exercise("after")
    persisted = r("docker","exec",n["db"],"psql","-U","j2m_test","-d",RESTORED,"-Atc",
                  "SELECT count(*) FROM google_wallet_write_reservations "
                  "WHERE operation_id=repeat('a',32) AND state='uncertain' AND active_slot=1")
    if persisted != "1":
        raise RuntimeError("Wallet reservation was not preserved")


def verify_https_and_recover():
    guard_images()
    # The serving smoke test only needs health/readiness; privileged data
    # verification remains a separate short-lived synthetic fixture.
    sql(RESTORED, "GRANT SELECT ON alembic_version TO j2m_web, j2m_worker")
    sql(RESTORED, "ALTER ROLE j2m_web LOGIN; ALTER ROLE j2m_worker LOGIN")

    start("web", new)
    start("worker", new)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        folder.chmod(0o755)
        write_certificates(folder)
        (folder/"nginx.conf").write_text(proxy_config().replace("access_log off;", "log_format proof '$http_x_j2m_probe $uri $upstream_addr $upstream_status'; access_log /dev/stdout proof;"),encoding="utf-8")
        r("docker","run","-d","--name",n["proxy"],"--network",p,"--network-alias","proxy","--label",label,"--mount",f"type=bind,src={folder},dst=/etc/nginx/tls,readonly",NGINX_IMAGE,"nginx","-c","/etc/nginx/tls/nginx.conf","-g","daemon off;")
        time.sleep(5)
        target = attest(folder, new)
        exercise("verify", new)
        journal.advance("target_verified", container=target.container)
        r("docker", "rm", "-f", n["web"])
        try:
            attest(folder, new)
        except RuntimeError:
            journal.advance("failure_injected")
        else:
            raise RuntimeError("Missing target was incorrectly accepted")
        r("docker", "rm", "-f", n["worker"])
        guard_images()
        start("web")
        start("worker")
        r("docker", "exec", n["proxy"], "nginx", "-c", "/etc/nginx/tls/nginx.conf", "-s", "reload")
        time.sleep(5)
        recovered = attest(folder)
        if recovered.container == target.container:
            raise RuntimeError("Expected a separate recovered backend")
        head = r("docker", "exec", n["db"], "psql", "-U", "j2m_test", "-d", RESTORED, "-Atc",
                 "SELECT version_num FROM alembic_version")
        if head != FINAL_HEAD:
            raise RuntimeError("Recovery changed the database revision")
        exercise("verify")
        journal.advance("legacy_recovered", container=recovered.container)
        r("docker", "rm", "-f", n["proxy"])
    print("Target and recovered legacy HTTPS bound to exact Docker upstream; one worker per stage.")


class DockerRehearsal:
    actions = {
        Phase.PREFLIGHT: provision,
        Phase.MAINTENANCE: assert_drained,
        Phase.FENCE: assert_drained,
        Phase.RESTORE: restore,
        Phase.MIGRATE: migrate,
        Phase.VERIFY: verify_migration,
        Phase.HTTPS: verify_https_and_recover,
    }

    def __init__(self):
        self.database_id = None

    def verify_isolation(self):
        guard_images()
        if self.database_id is not None:
            db = json.loads(r("docker", "inspect", self.database_id))[0]
            network = json.loads(r("docker", "network", "inspect", p))[0]
            if (db["Name"] != "/" + n["db"] or not network["Internal"]
                    or set(db["NetworkSettings"]["Networks"]) != {p}
                    or db["HostConfig"]["PortBindings"]):
                raise RuntimeError("Unexpected synthetic database isolation")

    def check_prerequisites(self, phase):
        if phase != Phase.PREFLIGHT:
            assert_drained()
        if phase == Phase.MIGRATE:
            self.check_revision(INITIAL_HEAD, RESTORED)

    def execute(self, phase):
        self.actions[phase]()
        interrupt_boundary(phase.value)

    def check_result(self, phase):
        if phase == Phase.PREFLIGHT:
            self.database_id = r("docker", "inspect", "--format", "{{.Id}}", n["db"])
            self.verify_isolation()
            self.check_revision(INITIAL_HEAD, SOURCE)
        if phase == Phase.RESTORE:
            self.check_revision(INITIAL_HEAD, RESTORED)
        if phase in {Phase.MIGRATE, Phase.VERIFY, Phase.HTTPS}:
            self.check_revision(FINAL_HEAD, RESTORED)
            self.check_revision(INITIAL_HEAD, SOURCE)
            if logical_digest(SOURCE) != source_digest:
                raise RuntimeError("Protected synthetic source changed")

    def check_revision(self, expected, database):
        observed = sql(database, "SELECT version_num FROM alembic_version")
        if observed != expected:
            raise RuntimeError("Unexpected synthetic schema revision")


try:
    rehearse(args.journal.with_suffix(args.journal.suffix + ".state"), DockerRehearsal(),
             identity={"checkout": sha, "legacy_image": img, "target_image": new,
                       "network": p, "database_container": n["db"],
                       "legacy_provenance": "reconstructed" if args.reconstructed_legacy else "local-original",
                       "initial_head": INITIAL_HEAD, "target_head": FINAL_HEAD})
except Exception as exc:
    journal.record("blocked", failure_type=type(exc).__name__)
    raise
finally:
    journal.file.close()
    for name in n.values():r("docker","rm","-f",name,check=False)
    r("docker","network","rm",p)
    if r("docker", "volume", "inspect", "--format",
         '{{index .Labels "j2m.synthetic.transition"}}', volume, check=False) == p:
        r("docker", "volume", "rm", volume)
    if production_ids() != before:
        raise RuntimeError("Operational container IDs changed")
verify_record(args.journal, legacy_image=img, target_image=new, target_sha=sha)
