"""Verify dump coverage against both migrated J2M schemas in the synthetic role harness."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextlib import contextmanager
from difflib import unified_diff
from unittest.mock import patch
from time import monotonic, sleep
from pathlib import Path
import os
import re
import subprocess
import sys
import json

from postgres_scoped_grants_fixture import prove_scoped_grants_and_fence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.selfhost_backup import (
    _hash_file, exclusive_lock, integral_backup, restore_integral_isolated, verify_integral_set, verify_transition_accounting,
)
from scripts.historical_transition_evidence import Backend, FINAL_HEAD, LEGACY_SHA, prove_https
from scripts.check_transition_relation_manifest import RELATION_QUERY, verify_relation_manifest
from scripts.check_transition_security_catalog import CATALOG_QUERY, verify_catalog, verify_fixture_security

SEED = """
from run import app
from app.extensions import db
from app.models import Business, Customer, Staff, PlatformAdmin
from app.services.loyalty_transaction_service import process_loyalty_transaction
with app.app_context():
    admin = PlatformAdmin(name="Synthetic platform admin", email="platform@example.test", is_active=True)
    admin.set_password("SyntheticPlatform123")
    db.session.add(admin)
    for i in (1, 2):
        business = Business(name=f'Synthetic {i}', slug=f'synthetic-{i}', loyalty_type='points' if i == 1 else 'stamps',
                            points_per_currency=1, stamps_reward_limit=2, cooldown_minutes=0,
                            web_access_enabled=True)
        db.session.add(business)
        db.session.flush()
        manager = Staff(business_id=business.id, username=f'manager{i}', email=f'manager{i}@example.test',
                        role='manager', web_access_enabled=True)
        manager.set_password("SyntheticPass123")
        cashier = Staff(business_id=business.id, username=f'cashier{i}', role='cashier',
                        web_access_enabled=True, permissions="scanner_operate_web,authorize_customer_identity")
        cashier.set_password("SyntheticPass123")
        db.session.add_all([manager, cashier])
        customer = Customer(business_id=business.id, full_name='Synthetic')
        db.session.add(customer)
        db.session.flush()
        process_loyalty_transaction(business=business, customer_id=customer.id, staff_id=None,
                                    action="earn_points" if i == 1 else "earn_stamp", tx_type="earn",
                                    points_amount=1 if i == 1 else 0, stamps_amount=0 if i == 1 else 1, purchase_amount=0, request_id=f"synthetic-{i}",
                                    fingerprint=f"synthetic-{i}")
    db.session.commit()
"""

BACKUP_GRANTS = """
DO $$
DECLARE item record;
BEGIN
  GRANT USAGE ON SCHEMA public TO j2m_backup;
  FOR item IN SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname='public' LOOP
    EXECUTE format('GRANT SELECT ON public.%I TO j2m_backup', item.tablename);
    IF item.rowsecurity AND NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename=item.tablename
          AND policyname='synthetic_backup_coverage') THEN
      EXECUTE format('CREATE POLICY synthetic_backup_coverage ON public.%I '
                     'FOR SELECT TO j2m_backup USING (true)', item.tablename);
    END IF;
  END LOOP;
  GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO j2m_backup;
END $$;
"""

DATA_QUERIES = """
SELECT format('SELECT %L,coalesce(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text),''[]''::jsonb) FROM public.%I x;',
              tablename,tablename) FROM pg_tables WHERE schemaname='public' ORDER BY tablename;
SELECT format('SELECT %L,last_value,is_called FROM public.%I;',sequencename,sequencename)
FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename;
"""


def prove_alternate_write_barrier(sql, database):
    def read():
        result = sql(CATALOG_QUERY, database=database)
        result.check_returncode()
        return json.loads(result.stdout)

    approved = read()
    # Reconcile against a captured catalog before and after exercising scoped
    # revocation. Reserved provider roles are intentionally absent from this
    # synthetic server; these disposable names model their privilege paths.
    statement = """
BEGIN;
CREATE ROLE j2m_fixture_gateway NOLOGIN;
CREATE ROLE j2m_fixture_writer NOLOGIN;
GRANT j2m_fixture_writer TO j2m_fixture_gateway WITH INHERIT TRUE, SET FALSE;
CREATE SCHEMA fixture_unrelated;
CREATE TABLE fixture_unrelated.data (id integer);
GRANT INSERT ON fixture_unrelated.data TO j2m_fixture_writer;
GRANT UPDATE (name) ON public.businesses TO j2m_fixture_writer;
GRANT UPDATE ON public.businesses TO PUBLIC;
CREATE FUNCTION public.fixture_gateway_write() RETURNS void LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog AS $$ UPDATE public.businesses SET name=name WHERE false $$;
DO $$ BEGIN
    IF NOT pg_has_role('j2m_fixture_gateway','j2m_fixture_writer','USAGE')
       OR NOT has_column_privilege('j2m_fixture_gateway','public.businesses','name','UPDATE')
       OR NOT has_function_privilege('j2m_fixture_gateway','public.fixture_gateway_write()','EXECUTE')
    THEN RAISE EXCEPTION 'Unexpected initial access'; END IF;
END $$;
REVOKE UPDATE ON public.businesses FROM PUBLIC;
REVOKE UPDATE (name) ON public.businesses FROM j2m_fixture_writer;
REVOKE EXECUTE ON FUNCTION public.fixture_gateway_write() FROM PUBLIC;
DO $$ BEGIN
    IF has_column_privilege('j2m_fixture_gateway','public.businesses','name','UPDATE')
       OR has_function_privilege('j2m_fixture_gateway','public.fixture_gateway_write()','EXECUTE')
       OR NOT has_table_privilege('j2m_fixture_gateway','fixture_unrelated.data','INSERT')
    THEN RAISE EXCEPTION 'Scoped barrier mismatch'; END IF;
END $$;
SET LOCAL ROLE j2m_fixture_gateway;
DO $$ BEGIN
    BEGIN
        PERFORM public.fixture_gateway_write();
        RAISE EXCEPTION 'Gateway retained RPC access';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END $$;
ROLLBACK;
"""
    sql(statement, database=database).check_returncode()
    verify_catalog(approved, read())

    sql("GRANT UPDATE (name) ON public.businesses TO j2m_backup", database=database).check_returncode()
    try:
        try:
            verify_catalog(approved, read())
        except ValueError as error:
            assert "changed=1" in str(error)
        else:
            raise AssertionError("Unexpected column grant was accepted")
    finally:
        sql("REVOKE UPDATE (name) ON public.businesses FROM j2m_backup", database=database).check_returncode()
    verify_catalog(approved, read())


@contextmanager
def certify_pre_migration_backup(network, sql, root, ca):
    """Run the real integral backup with an independently exported snapshot."""
    database = "full_schema"
    output = root / "restricted-backups"
    env_file = root / "restricted-backup.env"
    env_file.write_text(f"DATABASE_URL=postgresql://j2m_backup:synthetic-only@db/{database}\n"
                        f"DATABASE_SSL_ROOT_CERT={ca}\n", encoding="utf-8")
    key_file = root / "synthetic-key"
    logo = "businesses/1/logo/" + "a" * 32 + ".png"
    logo_path = root / "media" / logo
    logo_path.parent.mkdir(parents=True)
    from PIL import Image
    Image.new("RGBA", (2, 2), (255, 0, 0, 128)).save(logo_path)
    sql(f"UPDATE public.businesses SET logo_path='{logo}' WHERE id=1", database=database).check_returncode()
    coverage_queries = sql(DATA_QUERIES, database=database)
    coverage_queries.check_returncode()
    queries = coverage_queries.stdout.decode().splitlines()

    def run_backup(expected_head="9d0e1f2a3b4c", expected_error=None):
        start = monotonic()
        name = network.removesuffix("-internal")
        process = subprocess.Popen(
            ["docker", "exec", "-i", name, "psql", "-XqAt", "-v", "ON_ERROR_STOP=1",
             "-U", "postgres", "-d", database],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8",
        )

        def read_snapshot():
            process.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
                                "SELECT pg_export_snapshot();\n" + RELATION_QUERY + ";\n"
                                + "\n".join(queries) + "\n")
            process.stdin.flush()
            return [process.stdout.readline().rstrip("\r\n") for _ in range(len(queries) + 2)]

        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                snapshot_id, relations, *expected = executor.submit(read_snapshot).result(timeout=20)
                if not snapshot_id or len(expected) != len(queries) or not all(expected):
                    raise RuntimeError("Independent source inventory was incomplete")

                def coverage(restored):
                    # Include the exact relation set, otherwise new empty relations
                    # could disappear without affecting the row comparison.
                    destination = json.loads(restored(RELATION_QUERY))
                    return (destination == json.loads(relations)
                            and all(restored(statement) == row
                                    for statement, row in zip(queries, expected, strict=True)))

                with patch.dict(os.environ, {"J2M_SYNTHETIC_BACKUP_TEST": "1"}), patch(
                    "scripts.selfhost_backup.load_dpapi_key", return_value=bytes(range(32))
                ):
                    try:
                        index = integral_backup(
                            env_file, root / "media", output, key_file, retries=1, network=network,
                            timeout=180, restricted_snapshot=snapshot_id, verify_restored_coverage=coverage,
                            certification_head=expected_head, acquire_lock=False,
                        )
                    except RuntimeError as error:
                        if expected_error is None or expected_error not in str(error):
                            raise
                    else:
                        assert expected_error is None, "Unexpected backup certification"
                        assert verify_integral_set(index, key_file)
                        tag = "j2m-interactive:reconstructed-" + LEGACY_SHA
                        image_id = subprocess.check_output(
                            ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                            text=True, timeout=15,
                        ).strip()
                        label = subprocess.check_output(
                            ["docker", "image", "inspect", "--format",
                             '{{index .Config.Labels "org.opencontainers.image.revision"}}', image_id],
                            text=True, timeout=15,
                        ).strip()
                        if label != LEGACY_SHA:
                            raise RuntimeError("Historical reconstruction revision label does not match")
                        digest = _hash_file(index)
                        report = restore_integral_isolated(
                            index, key_file, tag, timeout=180, expected_image_id=image_id,
                            expected_index_sha256=digest,
                        )
                        assert report.alembic_revision == expected_head and report.businesses == 2 and report.customers == 2
                        print(f"Reconstructed legacy={LEGACY_SHA} image={image_id} index_sha256={digest}")
                print(f"Restricted integral backup ({'rejection' if expected_error else 'verified'}): "
                      f"{monotonic() - start:.2f}s")
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                process.stdout.close()

    # The disposable DB normally inherits CONNECT from PUBLIC. During the
    # archive window only the exporter and the restricted reader may connect.
    lock_file = root / ".operations.lock"

    def simulated_backup_task():
        return subprocess.run(
            [sys.executable, "-c",
             "from pathlib import Path; import sys; from scripts.selfhost_backup import exclusive_lock; "
             "context = exclusive_lock(Path(sys.argv[1])); context.__enter__(); context.__exit__(None, None, None)",
             str(lock_file)], cwd=ROOT, capture_output=True, timeout=20,
        )

    started = monotonic()
    with exclusive_lock(lock_file):
        competing_task = simulated_backup_task()
        if competing_task.returncode == 0 or b"Ya existe una" not in competing_task.stderr:
            raise RuntimeError("A simulated backup task bypassed the transition lock")
        sql("REVOKE CONNECT ON DATABASE full_schema FROM PUBLIC; "
            "GRANT CONNECT ON DATABASE full_schema TO j2m_backup",
            database=database).check_returncode()
        try:
            for role in ("j2m_schema_web", "j2m_schema_worker", "j2m_migrator"):
                assert sql("SELECT 1", role, database=database).returncode
            active = sql("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                         "AND pid<>pg_backend_pid() AND backend_type='client backend'", database=database)
            active.check_returncode()
            assert active.stdout.strip() == b"0"
            assert sql("SELECT nextval('public.customers_id_seq')", "j2m_backup",
                       database=database).returncode
            run_backup()
            before = {path.name for path in output.iterdir()}
            run_backup(expected_head="c3d4e5f6a7b8", expected_error="head Alembic esperado")
            assert {path.name for path in output.iterdir()} == before
            sql("ALTER TABLE platform_admins ENABLE ROW LEVEL SECURITY; "
                "CREATE POLICY synthetic_incomplete ON platform_admins FOR SELECT TO j2m_backup USING (false)",
                database=database).check_returncode()
            try:
                run_backup(expected_error="inventario independiente")
                assert {path.name for path in output.iterdir()} == before
            finally:
                sql("DROP POLICY synthetic_incomplete ON platform_admins; "
                    "ALTER TABLE platform_admins DISABLE ROW LEVEL SECURITY", database=database).check_returncode()
            sql("GRANT CONNECT ON DATABASE full_schema TO j2m_migrator",
                database=database).check_returncode()
            yield
        except Exception:
            # Disposable database stays closed until the outer cleanup removes it.
            raise
        else:
            sql("GRANT CONNECT ON DATABASE full_schema TO PUBLIC; "
                "REVOKE CONNECT ON DATABASE full_schema FROM j2m_backup,j2m_migrator",
                database=database).check_returncode()
            for role in ("j2m_schema_web", "j2m_schema_worker"):
                sql("SELECT 1", role, database=database).check_returncode()
    simulated_backup_task().check_returncode()
    print(f"Synthetic admission and task lock restored after {monotonic() - started:.2f}s.")


def attest_fenced_origin(network, ca, image, revision, sql):
    """Attest the migrated source through Nginx while only readers can enter."""
    from nginx_recovery_ci import NGINX_IMAGE, nonce_https_request, proxy_config, run, write_certificates

    if not re.fullmatch(r"j2m-roles-[0-9a-f]{8}-internal", network):
        raise ValueError("Unexpected synthetic HTTPS network")
    prefix = network.removesuffix("-internal")
    names = {role: f"{prefix}-attest-{role}" for role in ("web", "worker", "proxy")}
    db = json.loads(run("docker", "inspect", prefix))[0]["Id"]
    network_spec = json.loads(run("docker", "network", "inspect", network))[0]
    if (not network_spec["Internal"] or {entry["Name"] for entry in network_spec["Containers"].values()}
            != {prefix, f"{prefix}-pooler"}):
        raise RuntimeError("Unexpected initial Docker network membership")
    network_id = network_spec["Id"]
    folder = ca.parent / "https"
    folder.mkdir(mode=0o755)
    write_certificates(folder)
    (folder / "nginx.conf").write_text(proxy_config().replace(
        "access_log off;",
        "log_format proof '$http_x_j2m_probe $uri $upstream_addr $upstream_status'; access_log /dev/stdout proof;",
    ), encoding="utf-8")
    db_url = ("postgresql://j2m_backup:synthetic-only@db/full_schema"
              "?sslmode=verify-full&sslrootcert=/run/j2m/supabase-ca.pem")
    started = monotonic()
    selected = image
    selected_revision = revision

    def query(statement):
        result = sql(statement, database="full_schema")
        result.check_returncode()
        return result.stdout.decode().strip()

    def snapshot():
        observed = json.loads(run("docker", "network", "inspect", network))[0]
        services = {role: json.loads(run("docker", "inspect", name))[0] for role, name in names.items()}
        if (observed["Id"] != network_id or not observed["Internal"]
                or db not in observed["Containers"]
                or query("SELECT version_num FROM alembic_version") != FINAL_HEAD):
            raise RuntimeError("Fenced origin changed during HTTPS attestation")
        for role in ("web", "worker", "proxy"):
            service = services[role]
            if (not service["State"]["Running"] or service["HostConfig"]["PortBindings"]
                    or set(service["NetworkSettings"]["Networks"]) != {network}
                    or service["Config"]["Labels"].get("j2m.synthetic.role") != role
                    or (role != "proxy" and service["Image"] != selected)):
                raise RuntimeError("Unexpected synthetic HTTPS service identity")
        if observed["Containers"].keys() != {db, *(item["Id"] for item in services.values()), *pooler_ids}:
            raise RuntimeError("Unexpected network member during attestation")
        web = services["web"]
        return Backend(web["Image"], web["Id"], web["NetworkSettings"]["Networks"][network]["IPAddress"])

    pooler_ids = set(network_spec["Containers"]) - {db}
    def start_services():
        for role in ("web", "worker"):
            command = () if role == "web" else ("flask", "--app", "run.py", "run-google-wallet-sync-worker")
            process = "web" if role == "web" else "google-wallet-worker"
            run("docker", "run", "-d", "--name", names[role], "--network", network,
                "--network-alias", role, "--label", f"j2m.synthetic.role={role}",
                "--env-file", str(ROOT / ".env.wallet-demo.example"),
                "-e", f"J2M_PROCESS_TYPE={process}", "-e", f"J2M_RELEASE_IMAGE=j2m-interactive:{selected_revision or ''}",
                "-e", f"DATABASE_URL={db_url}", "-e", "DATABASE_SSLMODE=verify-full",
                "-e", "GOOGLE_WALLET_MODE=disabled", "-e", "APPLE_WALLET_MODE=disabled", "--mount",
                f"type=bind,src={ca},dst=/run/j2m/supabase-ca.pem,readonly", selected, *command)
    def attest():
        for role in ("web", "worker"):
            for _ in range(30):
                health = subprocess.run(
                    ["docker", "exec", names[role], "python", "scripts/container_healthcheck.py"],
                    capture_output=True, timeout=10,
                )
                if health.returncode == 0:
                    break
                sleep(.25)
            else:
                raise RuntimeError(f"Synthetic {role} healthcheck did not pass")
        return prove_https(
            snapshot, lambda route, nonce: nonce_https_request(network, folder, image, route, nonce),
            lambda: run("docker", "logs", names["proxy"]), revision=selected_revision,
        )

    try:
        start_services()
        run("docker", "run", "-d", "--name", names["proxy"], "--network", network, "--network-alias", "proxy",
            "--label", "j2m.synthetic.role=proxy", "--mount", f"type=bind,src={folder},dst=/etc/nginx/tls,readonly",
            NGINX_IMAGE, "nginx", "-c", "/etc/nginx/tls/nginx.conf", "-g", "daemon off;")
        backend = attest()
        run("docker", "rm", "-f", names["web"], names["worker"])
        try:
            snapshot()
        except RuntimeError:
            pass
        except subprocess.CalledProcessError:
            pass
        else:
            raise RuntimeError("Missing fenced backend was accepted")
        for role in ("j2m_schema_web", "j2m_schema_worker"):
            assert sql("SELECT 1", role, database="full_schema").returncode
        legacy_tag = "j2m-interactive:reconstructed-" + LEGACY_SHA
        legacy = json.loads(run("docker", "image", "inspect", legacy_tag))[0]
        if legacy["Config"]["Labels"].get("org.opencontainers.image.revision") != LEGACY_SHA:
            raise RuntimeError("Recovery image does not match the historical revision")
        selected, selected_revision = legacy["Id"], None
        start_services()
        run("docker", "exec", names["proxy"], "nginx", "-c", "/etc/nginx/tls/nginx.conf", "-s", "reload")
        recovered = attest()
        if recovered.container == backend.container:
            raise RuntimeError("Expected a different recovered backend")
        verify_transition_accounting(query)
        print(f"Same-origin fenced HTTPS and backend recovery: {monotonic() - started:.2f}s, "
              f"target={backend.container}, recovered={recovered.container}")
    finally:
        for name in names.values():
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def exercise_application_schema(network, run, sql, connection, pool_control, ca, operation):
    sql("CREATE DATABASE full_schema OWNER j2m_owner").check_returncode()

    root = ca.parent
    media = root / "media"
    media.mkdir()
    layer = root / "synthetic-compose.json"
    layer.write_text(json.dumps({
        "networks": {"j2m-private": {"external": True, "name": network, "driver": None}},
        "services": {"web": {"volumes": [{"type": "bind", "source": str(media),
                                           "target": "/app/instance/media", "read_only": True}]}},
    }), encoding="utf-8")
    candidate = os.environ.get("J2M_ROLE_FIXTURE_IMAGE", "j2m-interactive:wallet-demo-synthetic")
    metadata = json.loads(subprocess.check_output(["docker", "image", "inspect", candidate], timeout=15))[0]
    if candidate.startswith("j2m-interactive:transition-"):
        checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=15).strip()
        label = (metadata["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
        if label != checkout or candidate != f"j2m-interactive:transition-{checkout}":
            raise RuntimeError("Candidate image revision differs from the checkout")
        print(f"Candidate checkout={checkout} image={metadata['Id']}")
    env = {
        **os.environ,
        "J2M_IMAGE": metadata["Id"],
        "J2M_ENV_FILE": str(ROOT / ".env.wallet-demo.example"),
        "J2M_CLOUDFLARED_ENV_FILE": str(ROOT / ".env.cloudflared.example"),
        "J2M_DATABASE_CA_FILE": str(ca),
    }
    project = network.removesuffix("-internal")
    services = {"j2m_migrator": ("migrator", "MIGRATOR"),
                "j2m_schema_web": ("web", "WEB"),
                "j2m_schema_worker": ("google-wallet-worker", "WORKER")}
    base_env = (ROOT / ".env.wallet-demo.example").read_text(encoding="utf-8").splitlines()
    base_env = [line for line in base_env if not line.startswith(
        ("DATABASE_URL=", "DATABASE_SSLMODE=", "GOOGLE_WALLET_MODE=", "APPLE_WALLET_MODE="))]
    for _, suffix in services.values():
        env[f"J2M_{suffix}_ENV_FILE"] = str(root / f"{suffix}.env")

    def app(*args, role="j2m_migrator", database="full_schema", ca_override=None, dedicated=False):
        service, _ = services[role]
        for login, (_, suffix) in services.items():
            path = root / f"{suffix}.env"
            path.write_text("\n".join([
                *base_env, "GOOGLE_WALLET_MODE=disabled", "APPLE_WALLET_MODE=disabled",
                "DATABASE_SSLMODE=verify-full", "DATABASE_POOL_TIMEOUT=1",
                f"DATABASE_URL=postgresql://{login}:synthetic-{login}@db/{database}"
                "?sslmode=verify-full&sslrootcert=/run/j2m/supabase-ca.pem",
                "",
            ]), encoding="utf-8")
        # Bootstrap only the disposable source from an empty catalog.
        # The transition itself uses the standalone guarded migrator.
        dedicated = dedicated or args[:3] == ("python", "-m", "scripts.run_certified_migration")
        options = (["-e", "PGOPTIONS=-c role=j2m_owner"]
                   if role == "j2m_migrator" and not dedicated else [])
        compose = ["docker", "compose", "-p", project, "-f", "compose.selfhost.yml",
                   "-f", "compose.selfhost.identities.yml", "-f", str(layer), "--profile", "migrate"]
        process_env = {**env, "J2M_DATABASE_CA_FILE": str(ca_override or ca)}
        return subprocess.run([*compose, "run", "--rm", "--no-deps", "-T", *options, service, *args],
                              cwd=ROOT, env=process_env, check=ca_override is None,
                              capture_output=ca_override is not None, timeout=90)

    def snapshot(database):
        commands = sql(DATA_QUERIES, database=database)
        commands.check_returncode()
        rows = run("psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database,
                   input=commands.stdout, check=True).stdout
        schema = run("pg_dump", "-U", "postgres", "-d", database, "--schema-only",
                     "--no-owner", "--no-acl", "-n", "public", check=True).stdout
        # PostgreSQL deparses a text[] cast differently after a dump/parse cycle.
        lines = []
        for line in schema.splitlines():
            if line.startswith((b"\\restrict ", b"\\unrestrict ")):
                continue
            line = re.sub(rb"\(\(ARRAY\[([^\]]*)\]\)::text\[\]\)",
                          lambda match: b"(ARRAY[" + match[1].replace(b"::character varying", b"::text") + b"])",
                          line)
            line = re.sub(rb"\(('(?:[^']|'')*')::character varying\)::text", rb"\1::text", line)
            lines.append(line)
        return rows, lines

    def check_tls(role, database):
        expected_role = "j2m_owner" if role == "j2m_migrator" else role
        probe = (
            "from run import app; from app.extensions import db; from sqlalchemy import text; "
            "ctx=app.app_context(); ctx.push(); conn=db.session.connection(); "
            "assert conn.connection.driver_connection.info.ssl_in_use; "
            'observed=tuple(conn.execute(text("SELECT current_user,session_user,current_database()")).one()); '
            f"assert observed == {(expected_role, role, database)!r}; "
            "import os; "
            "assert db.engine.pool.size() == 2; "
            f"assert int(os.environ['DATABASE_MAX_OVERFLOW']) == {int(role != 'j2m_migrator')}; "
            "assert os.environ['J2M_PROCESS_TYPE'] != 'web' or os.environ['WEB_CONCURRENCY'] == '2'"
        )
        app("python", "-c", probe, role=role, database=database)

    def check_effective_pool(role, database):
        if role == "j2m_schema_web":
            gunicorn_config = """
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError

def post_worker_init(worker):
    from app.extensions import db
    from run import app
    with app.app_context():
        worker.held_connections = [db.engine.connect() for _ in range(3)]
        pids = [connection.scalar(text("SELECT pg_backend_pid()"))
                for connection in worker.held_connections]
        try:
            with db.engine.connect():
                raise AssertionError("Gunicorn pool overflow exceeded")
        except TimeoutError:
            pass
    ready = Path(__file__).with_name(f"{worker.pid}.pending")
    ready.write_text(",".join(map(str, pids)))
    ready.rename(ready.with_suffix(".ready"))
"""
            controller = """
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.request import urlopen
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import TimeoutError

assert os.environ["WEB_CONCURRENCY"] == "2"
with TemporaryDirectory() as directory:
    root = Path(directory)
    config = root / "probe.py"
    config.write_text(sys.argv[1])
    process = subprocess.Popen(["gunicorn", "--config", str(config), "--bind",
                                "127.0.0.1:18742", "--workers",
                                os.environ["WEB_CONCURRENCY"], "run:app"])
    try:
        for _ in range(80):
            assert process.poll() is None, "Gunicorn exited before saturation"
            ready = list(root.glob("*.ready"))
            if len(ready) == 2:
                break
            time.sleep(.1)
        else:
            raise AssertionError("Two Gunicorn workers did not saturate concurrently")
        backend_pids = {int(pid) for file in ready for pid in file.read_text().split(",")}
        assert len(backend_pids) == 6
        base = make_url(os.environ["DATABASE_URL"]).set(drivername="postgresql+psycopg2")
        with ExitStack() as budget:
            for role, password, size, overflow in (
                ("j2m_schema_worker", "synthetic-j2m_schema_worker", 2, 1),
                ("j2m_migrator", "synthetic-j2m_migrator", 2, 0),
                ("j2m_backup", "synthetic-only", 1, 0),
                ("postgres", "synthetic-only", 2, 0),
            ):
                pool = create_engine(
                    base.set(username=role, password=password), pool_size=size,
                    max_overflow=overflow, pool_timeout=1,
                )
                budget.callback(pool.dispose)
                for _ in range(size + overflow):
                    held = budget.enter_context(pool.connect())
                    backend_pids.add(held.scalar(text("SELECT pg_backend_pid()")))
                try:
                    with pool.connect():
                        raise AssertionError("Auxiliary connection budget exceeded")
                except TimeoutError:
                    pass
            live = {row[0] for row in held.execute(text(
                "SELECT pid FROM pg_stat_activity WHERE datname=current_database() "
                "AND backend_type='client backend'"
            ))}
            assert live == backend_pids and len(live) == 14, "Incomplete connection budget"
            with urlopen("http://127.0.0.1:18742/api/health", timeout=3) as result:
                assert result.status == 200
        print("Fourteen simultaneous synthetic sessions verified, including six held by Gunicorn.")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
"""
            app("python", "-c", controller, gunicorn_config, dedicated=True, role=role, database=database)
            return
        probe = """
from contextlib import ExitStack
import os
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError
from app.extensions import db
from run import app

expected = 2 if os.environ["J2M_PROCESS_TYPE"] == "migrator" else 3
with app.app_context(), ExitStack() as stack:
    for _ in range(expected):
        connection = stack.enter_context(db.engine.connect())
        assert connection.scalar(text("SELECT 1")) == 1
    try:
        with db.engine.connect():
            raise AssertionError("Connection limit was not enforced")
    except TimeoutError:
        pass
"""
        app("python", "-c", probe, dedicated=True, role=role, database=database)

    migration_probe = """
from unittest.mock import patch
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from app.extensions import db
from run import app
from scripts import run_certified_migration as migration
import os
import time

with app.app_context():
    peer_engine = create_engine(db.engine.url, poolclass=NullPool)
    with peer_engine.connect() as peer:
        lock = text("SELECT pg_try_advisory_lock(:key)")
        unlock = text("SELECT pg_advisory_unlock(:key)")
        params = {"key": migration.LOCK_KEY}
        assert peer.scalar(lock, params)
        peer.commit()
        try:
            migration.rehearse_upgrade(db.engine, app.config, environment=os.environ)
        except RuntimeError as error:
            assert str(error) == "Migration lock already held"
        else:
            raise AssertionError("A second migrator acquired an occupied bigint lock")
        assert peer.scalar(unlock, params)
        peer.commit()

        original_upgrade = migration.command.upgrade
        observed_pids = []

        def check_lock_and_identity(config, target):
            connection = config.attributes["j2m_migration_connection"]
            for step in (lambda: None, lambda: original_upgrade(config, target)):
                step()
                connection.commit()
                login, role, database, pid = migration._identity(connection)
                assert (login, role, database) == ("j2m_migrator", "j2m_owner", "full_schema")
                observed_pids.append(pid)
                assert not peer.scalar(lock, params)
                peer.commit()
            assert observed_pids[0] == observed_pids[1]

        with patch.object(migration.command, "upgrade", side_effect=check_lock_and_identity):
            migration.rehearse_upgrade(db.engine, app.config, environment=os.environ)
        assert peer.scalar(lock, params)
        assert peer.scalar(unlock, params)
        for _ in range(20):
            alive = peer.scalar(text("SELECT EXISTS (SELECT FROM pg_stat_activity WHERE pid = :pid)"),
                                {"pid": observed_pids[0]})
            peer.commit()
            if not alive:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("The migration backend remained connected")

        try:
            migration.rehearse_upgrade(db.engine, app.config, environment=os.environ)
        except RuntimeError as error:
            assert str(error) == "Source revision no longer eligible"
        else:
            raise AssertionError("The migration was replayed at the target revision")
        assert peer.scalar(lock, params)
        assert peer.scalar(unlock, params)
    peer_engine.dispose()
print("Dedicated migrator bigint lock, commit boundaries, role cleanup and replay denial verified.")
"""

    for i, head in enumerate(("9d0e1f2a3b4c", "c3d4e5f6a7b8")):
        if i == 0:
            app("flask", "--app", "run.py", "db", "upgrade", head)
            check_tls("j2m_migrator", "full_schema")
            check_effective_pool("j2m_migrator", "full_schema")
            from nginx_recovery_ci import write_certificates

            wrong = root / "wrong-ca"
            wrong.mkdir()
            wrong_ca, _, _ = write_certificates(wrong, hostnames=("db",), authority_name="Untrusted synthetic CI CA")
            denied = app("python", "-c", "from run import app; from app.extensions import db; "
                         "from sqlalchemy import text; app.app_context().push(); "
                         "db.session.execute(text('SELECT 1'))", ca_override=wrong_ca)
            if not denied.returncode or b"certificate verify failed" not in denied.stderr.lower():
                raise RuntimeError("Connection accepted an unrelated CA or failed for the wrong reason")
            app("python", "-c", SEED)
        else:
            started = monotonic()
            app("python", "-c", migration_probe, dedicated=True)
            print(f"Dedicated migration and fencing checks: {monotonic() - started:.2f}s")
        sql(BACKUP_GRANTS, database="full_schema").check_returncode()
        fixture_catalog = sql(CATALOG_QUERY, database="full_schema")
        fixture_catalog.check_returncode()
        catalog = json.loads(fixture_catalog.stdout)
        verify_fixture_security(head, catalog)
        for change in ("owner", "owner_acl", "acl", "object", "membership", "policy", "role", "routine", "trigger"):
            drift = deepcopy(catalog)
            if change == "owner":
                drift["relation:customers"][1] = "postgres"
            elif change == "owner_acl":
                drift["relation:customers"][-1].remove("j2m_owner=arwdDxtm/j2m_owner")
            elif change == "policy":
                drift["policy:referral_participants.synthetic_backup_coverage"][3] = "false"
            elif change == "role":
                drift["role:j2m_backup"][-2] = True
            elif change == "trigger":
                drift["trigger:customer_auth_audits.trg_customer_auth_audits_append_only"][0] = "D"
            elif change == "routine":
                drift["routine:prevent_customer_auth_audit_mutation()"][2] = ["=X/j2m_owner"]
            elif change == "acl":
                drift["relation:customers"][-1].append("=w/j2m_owner")
            elif change == "object":
                drift["relation:unexpected"] = drift["relation:customers"]
            else:
                drift["effective-membership:j2m_backup:j2m_owner"] = [True, True, True]
            try:
                verify_fixture_security(head, drift)
            except ValueError:
                pass
            else:
                raise AssertionError(f"Fixture accepted {change} drift at {head}")
        print(f"Declared relation ownership, ACL, RLS and membership verified for {head}.")
        if i == 0:
            operation.enter_context(certify_pre_migration_backup(network, sql, root, ca))
        assert sql("SELECT version_num FROM alembic_version",
                   database="full_schema").stdout.strip() == head.encode()
        observed_relations = sql(RELATION_QUERY, database="full_schema")
        observed_relations.check_returncode()
        verify_relation_manifest(head, json.loads(observed_relations.stdout))
        assert sql("SELECT count(*) FROM customers", database="full_schema").stdout.strip() == b"2"
        assert sql("UPDATE public.customers SET current_stamps=1", "j2m_backup", database="full_schema").returncode
        assert sql("SET ROLE j2m_owner", "j2m_backup", database="full_schema").returncode
        backup_tls = ("env", "PGPASSWORD=synthetic-only", "PGSSLMODE=verify-full",
                      "PGSSLROOTCERT=/tmp/ca.crt")
        backup_identity = run(*backup_tls, "psql", "-XAt", "-v", "ON_ERROR_STOP=1",
                              "-h", "db", "-U", "j2m_backup", "-d", "full_schema", "-c",
                              "SELECT current_user,session_user,ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()",
                              check=True)
        assert backup_identity.stdout.strip() == b"j2m_backup|j2m_backup|t"
        dump = run(*backup_tls, "pg_dump", "-h", "db",
                   "-U", "j2m_backup", "--enable-row-security", "--no-owner", "--no-acl",
                   "-Fc", "-n", "public", "full_schema", check=True).stdout
        destination = f"full_restore_{i}"
        sql(f"CREATE DATABASE {destination}").check_returncode()
        sql("DROP SCHEMA public", database=destination).check_returncode()
        restore = run("pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "-U", "postgres",
                      "-d", destination, input=dump)
        if restore.returncode:
            raise RuntimeError("Synthetic schema restore failed: " + restore.stderr.decode()[:600])
        origin, restored = snapshot("full_schema"), snapshot(destination)
        if origin[0] != restored[0] or origin[1] != restored[1]:
            diff = list(unified_diff([line.decode() for line in origin[1]], [line.decode() for line in restored[1]], n=0))
            raise RuntimeError(f"Restored J2M fixture differs (data matched={origin[0] == restored[0]}): " + "\n".join(diff[:20]))
        def query(statement):
            result = sql(statement, database=destination)
            result.check_returncode()
            return result.stdout.decode().strip()

        verify_transition_accounting(query)
        if i == 1:
            prove_alternate_write_barrier(sql, "full_schema")
            for role in ("j2m_schema_web", "j2m_schema_worker"):
                assert sql("SELECT 1", role, database="full_schema").returncode
            print("Legacy backup exclusion and writer admission gate held through migration.")
            if candidate.startswith("j2m-interactive:transition-"):
                attest_fenced_origin(network, ca, metadata["Id"], checkout, sql)


            sql(BACKUP_GRANTS, database=destination).check_returncode()
            flow = (ROOT / "tests/postgres_referral_worker_flow.py").read_text()
            checked = set()

            def run_service(role, source=flow):
                if role not in checked:
                    check_tls(role, destination)
                    check_effective_pool(role, destination)
                    checked.add(role)
                app("python", "-c", source, role=role, database=destination)

            prove_scoped_grants_and_fence(
                sql, destination, run_service,
                connection, pool_control,
            )
            verify_transition_accounting(query)
            # The lazy baseline is valid for an unused zero-balance account,
            # but must never certify an unexplained positive or negative balance.
            customer_id = query("""
                INSERT INTO customers (business_id,full_name,marketing_consent,
                    current_points,current_stamps,qr_code_token,created_at)
                VALUES (1,'Synthetic unused',false,0,0,'synthetic-unused',now())
                RETURNING id
            """).splitlines()[0]
            assert customer_id.isdigit()
            verify_transition_accounting(query)
            for points, stamps in ((1, 0), (0, 1), (-1, 0)):
                query(f"UPDATE customers SET current_points={points}, current_stamps={stamps} WHERE id={customer_id}")
                try:
                    verify_transition_accounting(query)
                except RuntimeError as error:
                    if "cadena contable" not in str(error):
                        raise
                else:
                    raise RuntimeError("An unexplained customer balance was accepted")
            query(f"DELETE FROM customers WHERE id={customer_id}")
            verify_transition_accounting(query)
            query("SELECT setval('public.customers_id_seq', 1, true)")
            try:
                verify_transition_accounting(query)
            except RuntimeError as error:
                if "Secuencia" not in str(error):
                    raise
            else:
                raise RuntimeError("A regressed customer sequence was accepted")
            query("SELECT setval('public.customers_id_seq', 2, true)")
            customer_flow = (ROOT / "tests/postgres_customer_http_flow.py").read_text()
            app("python", "-c", customer_flow, role="j2m_schema_web", database=destination)
            verify_transition_accounting(query)
            query("INSERT INTO transactions (business_id,customer_id,type,points_amount,"
                  "stamps_amount,purchase_amount,timestamp) VALUES (1,1,'earn',0,1,0,now())")
            try:
                verify_transition_accounting(query)
            except RuntimeError as error:
                if "cadena contable" not in str(error):
                    raise
            else:
                raise RuntimeError("A movement without a ledger entry was accepted")

        print(f"Full J2M {head}: public schema, data and sequences matched after restore.")
