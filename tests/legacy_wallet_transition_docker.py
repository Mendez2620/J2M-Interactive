"""Laptop-only synthetic transition using the original historical container image."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from nginx_recovery_ci import NGINX_IMAGE, production_ids, proxy_config, run, write_certificates

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.historical_transition_evidence import (Backend, FINAL_HEAD, INITIAL_HEAD, LEGACY_SHA,
                                                    TransitionJournal, prove_https, verify_record)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--journal", type=Path)
parser.add_argument("--preflight-only", action="store_true")
args = parser.parse_args()
if not args.preflight_only and args.journal is None:
    parser.error("--journal is required for the synthetic rehearsal")
sha = run("git", "-C", str(ROOT), "rev-parse", "HEAD")
if run("git", "-C", str(ROOT), "status", "--porcelain"):
    raise RuntimeError("Rehearsal requires a clean source tree")
legacy_tag = "j2m-interactive:" + LEGACY_SHA
target_tag = "j2m-interactive:transition-" + sha


def image(tag):
    return run("docker", "image", "inspect", "--format", "{{.Id}}", tag)


img, new = image(legacy_tag), image(target_tag)
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
p = "j2m-legacy-tls-" + uuid.uuid4().hex[:8]
n = {role: p + "-" + role for role in ("db", "web", "worker", "proxy")}
root = ROOT
journal = TransitionJournal(args.journal, legacy_image=img, target_image=new, target_sha=sha,
                            environment="synthetic")
probe=(root/"tests/legacy_wallet_fixture.py").read_text(encoding="utf-8")
def exercise(stage, selected=img):
    subprocess.run(["docker","run","--rm","-i",*cfg,selected,"python","-",stage],input=probe,text=True,check=True,timeout=60)
cfg=["--network",p,"--env-file",str(root/".env.wallet-demo.example"),"-e","DATABASE_URL=postgresql://j2m_test:synthetic@db/j2m_test","-e","GOOGLE_WALLET_MODE=disabled","-e","APPLE_WALLET_MODE=disabled"]
def start(role, selected=img):
    cmd=() if role=="web" else ("flask","--app","run.py","run-google-wallet-sync-worker")
    process="web" if role=="web" else "google-wallet-worker"
    r("docker","run","-d","--name",n[role],"--network-alias",role,"--label",f"com.docker.compose.project={p}","--label",f"com.docker.compose.service={role}",*cfg,"-e",f"J2M_PROCESS_TYPE={process}","-e",f"J2M_RELEASE_IMAGE=j2m-interactive:{sha if selected == new else ''}",selected,*cmd)
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
                    or labels["com.docker.compose.project"] != p or labels["com.docker.compose.service"] != role):
                raise RuntimeError("Unexpected Docker backend identity")
        web = ids["web"]
        return Backend(web["Image"], web["Id"], web["NetworkSettings"]["Networks"][p]["IPAddress"])

    for role in ("web", "worker"):
        r("docker", "exec", n[role], "python", "scripts/container_healthcheck.py")

    def request(route, nonce):
        http_input = (f"GET /api/{route} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
                      f"X-J2M-Probe: {nonce}\r\n\r\n").encode()
        client = ("import socket,ssl,json; c=ssl.create_default_context(cafile='/proof/ca.crt'); "
                  "s=c.wrap_socket(socket.create_connection(('proxy',8443),3),server_hostname='localhost'); "
                  f"s.sendall({http_input!r}); b=s.makefile('rb'); status=int(b.readline().split()[1]); "
                  "headers={k.lower():v.strip() for k,v in (line.decode().split(':',1) "
                  "for line in iter(b.readline,b'\\r\\n'))}; "
                  "print(json.dumps([status,headers.get('x-j2m-revision')]))")
        return json.loads(r("docker", "run", "--rm", "--network", p, "--mount",
                            f"type=bind,src={folder},dst=/proof,readonly", new, "python", "-c", client))

    return prove_https(snapshot, request, lambda: r("docker", "logs", n["proxy"]),
                       revision=sha if selected == new else None)


def restore_fixture():
    db_args = ["docker", "exec", n["db"]]
    archive = subprocess.check_output([*db_args, "pg_dump", "-U", "j2m_test", "-Fc", "j2m_test"], timeout=25)
    if not archive.startswith(b"PGDMP") or len(archive) > 8 * 1024 * 1024:
        raise RuntimeError("Invalid synthetic snapshot")
    r(*db_args, "createdb", "-U", "j2m_test", "restore_test")
    subprocess.run(["docker", "exec", "-i", n["db"], "pg_restore", "--exit-on-error",
                    "-U", "j2m_test", "-d", "restore_test"], input=archive, check=True, timeout=25)
    rows = r(*db_args, "psql", "-U", "j2m_test", "-d", "restore_test", "-Atc",
             "SELECT version_num, (SELECT count(*) FROM transactions), "
             "(SELECT count(*) FROM google_wallet_sync_jobs) FROM alembic_version")
    if rows != INITIAL_HEAD + "|1|1":
        raise RuntimeError("Synthetic restore did not preserve revision and movements")
    journal.advance("restored", sha256=hashlib.sha256(archive).hexdigest(), revision=INITIAL_HEAD)


def guard_images():
    if image(legacy_tag) != img or image(target_tag) != new:
        raise RuntimeError("A transition image tag changed")


try:
    journal.advance("preflight")
    r("docker","network","create","--internal",p)
    r("docker","run","-d","--name",n["db"],"--network",p,"--network-alias","db","--tmpfs","/var/lib/postgresql/data","-e","POSTGRES_USER=j2m_test","-e","POSTGRES_DB=j2m_test","-e","POSTGRES_PASSWORD=synthetic","postgres:16-alpine")
    time.sleep(4)
    r("docker","run","--rm",*cfg,new,"flask","--app","run.py","db","upgrade","9d0e1f2a3b4c")
    exercise("before")
    restore_fixture()
    guard_images()
    # The seed container has already exited; only PostgreSQL may be attached.
    network = json.loads(r("docker", "network", "inspect", p))[0]
    if {value["Name"] for value in network["Containers"].values()} != {n["db"]}:
        raise RuntimeError("Synthetic writers not drained")
    journal.advance("maintenance")
    r("docker","run","--rm",*cfg,new,"flask","--app","run.py","db","upgrade","c3d4e5f6a7b8")
    schema_check="from run import app; from app.extensions import db; from scripts.check_wallet_referral_schema import validate_schema; a=app.app_context(); a.push(); raise SystemExit(bool(validate_schema(db.session.connection())));"
    r("docker","run","--rm",*cfg,new,"python","-c",schema_check)
    reserve = """
INSERT INTO google_wallet_write_reservations
(operation_id, owner_token, issuer, kind, resource_id, business_id, customer_id, state, active_slot, updated_at)
VALUES (repeat('a',32), repeat('b',32), 'synthetic', 'loyaltyObject', 'synthetic.object', 1, 1, 'uncertain', 1, now());
"""
    r("docker","exec",n["db"],"psql","-U","j2m_test","-d","j2m_test","-v","ON_ERROR_STOP=1","-c",reserve)
    journal.advance("migrated", revision=FINAL_HEAD)
    exercise("after")
    persisted = r("docker","exec",n["db"],"psql","-U","j2m_test","-d","j2m_test","-Atc",
                  "SELECT count(*) FROM google_wallet_write_reservations "
                  "WHERE operation_id=repeat('a',32) AND state='uncertain' AND active_slot=1")
    if persisted != "1":
        raise RuntimeError("Wallet reservation was not preserved")
    guard_images()
    start("web", new)
    start("worker", new)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        folder.chmod(0o755)
        write_certificates(folder)
        (folder/"nginx.conf").write_text(proxy_config().replace("access_log off;", "log_format proof '$http_x_j2m_probe $uri $upstream_addr $upstream_status'; access_log /dev/stdout proof;"),encoding="utf-8")
        r("docker","run","-d","--name",n["proxy"],"--network",p,"--network-alias","proxy","--mount",f"type=bind,src={folder},dst=/etc/nginx/tls,readonly",NGINX_IMAGE,"nginx","-c","/etc/nginx/tls/nginx.conf","-g","daemon off;")
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
        head = r("docker", "exec", n["db"], "psql", "-U", "j2m_test", "-Atc",
                 "SELECT version_num FROM alembic_version")
        if head != FINAL_HEAD:
            raise RuntimeError("Recovery changed the database revision")
        exercise("verify")
        journal.advance("legacy_recovered", container=recovered.container)
        r("docker", "rm", "-f", n["proxy"])
    print("Target and recovered legacy HTTPS bound to exact Docker upstream; one worker per stage.")
except Exception as exc:
    journal.record("blocked", failure_type=type(exc).__name__)
    raise
finally:
    journal.file.close()
    for name in n.values():r("docker","rm","-f",name,check=False)
    r("docker","network","rm",p)
    if production_ids() != before:
        raise RuntimeError("Operational container IDs changed")
verify_record(args.journal, legacy_image=img, target_image=new, target_sha=sha)
