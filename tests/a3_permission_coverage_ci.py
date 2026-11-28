"""Run the exact A3 increment on both disposable Alembic heads."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from time import monotonic, sleep
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("j2m_owner", "j2m_migrator", "j2m_web", "j2m_worker", "j2m_backup", "j2m_recovery")
EXPECTED_OIDS = "ARRAY[18919,18921,18923,18925,18927,18929]::oid[]"


def run(*args, sql=None, check=True, timeout=120):
    result = subprocess.run(["docker", *args], input=sql, text=True, encoding="utf-8",
                            capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-4500:])
    return result


def exercise(image, legacy_image, stage):
    name = "j2m-a3-" + uuid4().hex[:12]
    network = container = None

    def sql(command, *, check=True):
        return run("exec", "-i", container, "psql", "-XqAt", "-U", "postgres", "-d", "postgres",
                   "-v", "ON_ERROR_STOP=1", sql=command, check=check)

    def app(*args, role="postgres", runtime=image):
        password = "synthetic-only"
        return run("run", "--rm", "--network", network,
                   "--env-file", str(ROOT / ".env.selfhost.test.example"),
                   "-e", f"DATABASE_URL=postgresql://{role}:{password}@db:5432/postgres",
                   "-e", "DATABASE_SSLMODE=disable", "-e", "GOOGLE_WALLET_MODE=disabled",
                   "-e", "APPLE_WALLET_MODE=disabled", runtime, *args)

    def catalog(statement):
        return sql(statement).stdout.strip()

    def assert_denied(statement, expected):
        denied = sql(statement, check=False)
        if denied.returncode == 0 or expected not in denied.stderr:
            raise AssertionError(denied.stderr or "Unexpected privilege")

    try:
        network = run("network", "create", "--internal", "--label", f"j2m.synthetic={name}", name).stdout.strip()
        container = run("run", "-d", "--name", name, "--network", network, "--network-alias", "db",
                        "--tmpfs", "/var/lib/postgresql/data", "-e", "POSTGRES_PASSWORD=synthetic-only",
                        "postgres:17-alpine").stdout.strip()
        assert json.loads(run("network", "inspect", network).stdout)[0]["Internal"]
        assert not json.loads(run("container", "inspect", container).stdout)[0]["HostConfig"]["PortBindings"]
        deadline = monotonic() + 30
        while run("exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres",
                  check=False).returncode:
            if monotonic() >= deadline:
                raise RuntimeError("Synthetic PostgreSQL startup timed out")
            sleep(.25)
        app("flask", "--app", "run.py", "db", "upgrade", "9d0e1f2a3b4c")
        for role, limit in zip(ROLES, (0, 2, 6, 3, 1, 1)):
            sql(f"CREATE ROLE {role} NOLOGIN NOINHERIT CONNECTION LIMIT {limit}")
        ids = catalog("SELECT string_agg(oid::text,',' ORDER BY oid) FROM pg_roles "
                      "WHERE rolname IN ('" + "','".join(ROLES) + "')")
        assert len(ids.split(",")) == len(ROLES)

        def bundle(stage, action):
            path = ROOT / "scripts" / "a3_permissions" / f"{stage}_{action}.sql"
            raw = path.read_bytes()
            checksums = dict((name, digest) for digest, name in (
                line.split() for line in path.with_name("SHA256SUMS").read_text().splitlines()
            ))
            assert hashlib.sha256(raw).hexdigest() == checksums[path.name]
            return raw.decode("utf8").replace(EXPECTED_OIDS, f"ARRAY[{ids}]::oid[]")

        if stage == "target":
            sql((ROOT / "tests/a3_legacy_baseline.sql").read_text())
        if stage == "target":
            app("flask", "--app", "run.py", "db", "upgrade", "c3d4e5f6a7b8")
            manifest = json.loads((ROOT / "scripts/j2m_transition_relation_manifest.json").read_text())
            sql("GRANT CREATE ON SCHEMA public TO j2m_owner;\n" + "\n".join(
                f"ALTER TABLE public.{entry[2:]} OWNER TO j2m_owner;"
                for entry in manifest["legacy"] + manifest["additions"] if entry.startswith("r:")
            ))
        prefix = "SET ROLE j2m_owner;\n" if stage == "target" else ""
        sql(prefix + (ROOT / "tests" / f"a3_{stage}_baseline.sql").read_text())
        applied = prefix + bundle(stage, "apply")
        revert = prefix + bundle(stage, "revert")
        sql(prefix + "ALTER POLICY j2m_a2_backup_read ON public.referral_eligibilities USING (false);")
        assert_denied(applied, "Unexpected A2/A3 ACL or policy set")
        sql(prefix + "ALTER POLICY j2m_a2_backup_read ON public.referral_eligibilities USING (true);")
        sql(applied)
        assert_denied(applied, "Unexpected A2/A3 ACL or policy set")
        assert_denied("SET ROLE j2m_worker; UPDATE public.customers SET current_points=0;",
                      "permission denied")
        assert_denied("SET ROLE j2m_web; UPDATE public.staff SET password_hash='synthetic' WHERE false;",
                      "permission denied")
        assert catalog("SET ROLE j2m_worker; SELECT count(id) FROM public.google_wallet_publication_jobs "
                       "WHERE status='processing' AND locked_at IS NOT NULL;") == "0"
        assert_denied("SET ROLE j2m_worker; SELECT customer_id FROM public.google_wallet_publication_jobs;",
                      "permission denied")
        flow = (ROOT / "tests/a3_web_http_flow.py").read_text(encoding="utf-8-sig")
        runtime = legacy_image if stage == "legacy" else image
        app("python", "-c", flow, "seed", runtime=runtime)
        sql("ALTER ROLE j2m_web LOGIN PASSWORD 'synthetic-only';")
        app("python", "-c", flow, "http", role="j2m_web", runtime=runtime)
        sql("ALTER ROLE j2m_web NOLOGIN PASSWORD NULL;")
        assert_denied("SET ROLE j2m_web; UPDATE public.referral_participants SET updated_at=now();",
                      "row-level security")
        sql(revert)
        assert_denied("SET ROLE j2m_worker; SELECT id FROM public.google_wallet_publication_jobs;",
                      "permission denied")
        assert_denied("SET ROLE j2m_web; INSERT INTO public.business_categories (name,slug) VALUES ('a','a');",
                      "permission denied")
        assert catalog("SELECT count(*) FROM pg_roles WHERE rolname = ANY(ARRAY['" +
                       "','".join(ROLES) + "']) AND NOT rolcanlogin") == "6"
        print(f"{stage}: restricted ACL increment, replay denial, negative grants and exact rollback passed.",
              flush=True)
    finally:
        if container:
            run("rm", "-f", container)
        if network:
            run("network", "rm", network)


if __name__ == "__main__":
    for phase in ("legacy", "target"):
        exercise(os.environ["J2M_A3_FIXTURE_IMAGE"], os.environ["J2M_A3_LEGACY_IMAGE"], phase)
