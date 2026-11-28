"""Synthetic PostgreSQL exercise for split roles and a durable writer fence."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4

from nginx_recovery_ci import write_certificates
from postgres_full_schema_fixture import exercise_application_schema

SETUP = """
CREATE ROLE j2m_owner NOLOGIN;
CREATE ROLE j2m_migrator LOGIN NOINHERIT PASSWORD 'synthetic-j2m_migrator';
GRANT j2m_owner TO j2m_migrator;
CREATE ROLE j2m_web LOGIN PASSWORD 'synthetic-only';
CREATE ROLE j2m_worker LOGIN PASSWORD 'synthetic-only';
CREATE ROLE j2m_backup LOGIN PASSWORD 'synthetic-only';
CREATE SCHEMA rehearsal AUTHORIZATION j2m_owner;
SET ROLE j2m_owner;
CREATE TABLE rehearsal.ledger (id bigint GENERATED ALWAYS AS IDENTITY, tenant int, balance int);
ALTER TABLE rehearsal.ledger ENABLE ROW LEVEL SECURITY;
CREATE FUNCTION rehearsal.write_ledger() RETURNS void LANGUAGE SQL
AS $$ INSERT INTO rehearsal.ledger (tenant,balance) VALUES (1,99) $$;
REVOKE EXECUTE ON FUNCTION rehearsal.write_ledger() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rehearsal.write_ledger() TO j2m_web;
INSERT INTO rehearsal.ledger (tenant, balance) VALUES (1, 2), (2, 3);
GRANT USAGE ON SCHEMA rehearsal TO j2m_web,j2m_worker,j2m_backup;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON rehearsal.ledger TO j2m_web,j2m_worker;
GRANT USAGE ON SEQUENCE rehearsal.ledger_id_seq TO j2m_web,j2m_worker;
GRANT SELECT ON rehearsal.ledger, rehearsal.ledger_id_seq TO j2m_backup;
CREATE POLICY web_rows ON rehearsal.ledger TO j2m_web USING (tenant = 1) WITH CHECK (tenant = 1);
CREATE POLICY worker_rows ON rehearsal.ledger TO j2m_worker USING (tenant = 2) WITH CHECK (tenant = 2);
CREATE POLICY backup_rows ON rehearsal.ledger FOR SELECT TO j2m_backup USING (true);
RESET ROLE;
"""

FENCE = """
BEGIN;
ALTER ROLE j2m_web NOLOGIN;
ALTER ROLE j2m_worker NOLOGIN;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON rehearsal.ledger FROM j2m_web,j2m_worker;
REVOKE USAGE ON SEQUENCE rehearsal.ledger_id_seq FROM j2m_web,j2m_worker;
LOCK TABLE rehearsal.ledger IN ACCESS EXCLUSIVE MODE;
COMMIT;
"""


def main():
    name = "j2m-roles-" + uuid4().hex[:8]
    network = name + "-internal"
    pooler = name + "-pooler"
    subprocess.run(["docker", "network", "create", "--internal", network], check=True, capture_output=True, timeout=30)
    subprocess.run(["docker", "run", "-d", "--name", name, "--network", network, "--network-alias", "db",
                    "-e", "POSTGRES_PASSWORD=synthetic-only", "-e", "POSTGRES_DB=j2m_test",
                    "postgres:17-alpine"], check=True, capture_output=True, timeout=60)

    def run(*args, **kwargs):
        return subprocess.run(["docker", "exec", "-i", name, *args], capture_output=True,
                              timeout=20, **kwargs)

    def password(role):
        if role in {"j2m_migrator", "j2m_schema_web", "j2m_schema_worker"}:
            return f"synthetic-{role}"
        return "synthetic-only"

    def sql(statement, role="postgres", database="j2m_test"):
        return run("env", f"PGPASSWORD={password(role)}", "psql", "-XAt", "-v", "ON_ERROR_STOP=1",
                   "-h", "127.0.0.1", "-U", role, "-d", database, "-c", statement)

    def connection(role, database, *, direct=False):
        endpoint = ["-h", "127.0.0.1"] if direct else ["-h", pooler, "-p", "6432"]
        return subprocess.Popen(
            ["docker", "exec", "-i", name, "env", f"PGPASSWORD={password(role)}",
             "psql", "-XqAt", *endpoint, "-U", role, "-d", database],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    try:
        for _ in range(25):
            if sql("SELECT 1").returncode == 0:
                break
            time.sleep(1)
        sql(SETUP).check_returncode()
        query = "SELECT tenant,balance FROM rehearsal.ledger ORDER BY tenant,id"
        assert sql(query, "j2m_web").stdout.strip() == b"1|2"
        assert sql(query, "j2m_worker").stdout.strip() == b"2|3"
        assert sql("INSERT INTO rehearsal.ledger (tenant,balance) VALUES (2,8)", "j2m_web").returncode
        assert sql("UPDATE rehearsal.ledger SET balance=0", "j2m_backup").returncode
        assert sql("SET ROLE j2m_owner", "j2m_web").returncode
        source = sql(query).stdout.strip()
        dump = run("env", "PGPASSWORD=synthetic-only", "pg_dump", "-h", "127.0.0.1",
                   "-U", "j2m_backup", "--enable-row-security", "--no-owner", "--no-acl",
                   "-Fc", "-n", "rehearsal", "j2m_test", check=True).stdout
        sql("CREATE DATABASE restored").check_returncode()
        run("pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "-U", "postgres",
            "-d", "restored", input=dump, check=True)
        assert sql(query, database="restored").stdout.strip() == source

        # A live connection must lose DML too. Wait until its first SELECT completes.
        peer = subprocess.Popen(["docker", "exec", "-i", name, "env", "PGPASSWORD=synthetic-only",
                                 "psql", "-XqAt", "-h", "127.0.0.1", "-U", "j2m_web", "-d", "j2m_test"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            peer.stdin.write(b"BEGIN; SELECT balance FROM rehearsal.ledger FOR UPDATE;\n")
            peer.stdin.flush()
            assert peer.stdout.readline().strip() == b"2"
            with ThreadPoolExecutor(max_workers=1) as executor:
                fence = executor.submit(sql, FENCE)
                for _ in range(40):
                    blocked = sql("SELECT count(*) FROM pg_locks WHERE NOT granted "
                                  "AND relation='rehearsal.ledger'::regclass").stdout.strip()
                    if blocked not in (b"", b"0"):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Fence did not wait for the active transaction")
                peer.stdin.write(b"ROLLBACK;\n")
                peer.stdin.flush()
                fence.result(timeout=15).check_returncode()
            assert sql("SELECT 1", "j2m_web").returncode
            assert sql("SELECT 1", "j2m_worker").returncode
            out, errors = peer.communicate(b"INSERT INTO rehearsal.ledger (tenant,balance) VALUES (1,9);\n"
                                           b"UPDATE rehearsal.ledger SET balance=9;\nDELETE FROM rehearsal.ledger;\n"
                                           b"TRUNCATE rehearsal.ledger;\nSELECT nextval('rehearsal.ledger_id_seq');\n"
                                           b"SET ROLE j2m_owner;\nSELECT rehearsal.write_ledger();\n",
                                           timeout=8)
            assert errors.count(b"permission denied") >= 7
            sql("SET ROLE j2m_owner; ALTER TABLE rehearsal.ledger ADD COLUMN note text",
                "j2m_migrator").check_returncode()
            assert sql(query).stdout.strip() == source
        finally:
            if peer.poll() is None:
                peer.kill()
                peer.wait(timeout=8)
        start_pooler = """cat > /tmp/j2m-users <<'EOF'
"j2m_schema_web" "synthetic-j2m_schema_web"
"j2m_schema_worker" "synthetic-j2m_schema_worker"
"j2m_pool_admin" "synthetic-only"
EOF
cat > /tmp/j2m-pool.ini <<'EOF'
[databases]
full_restore_1 = host=db port=5432 dbname=full_restore_1
full_schema = host=db port=5432 dbname=full_schema
[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /tmp/j2m-users
admin_users = j2m_pool_admin
pool_mode = session
query_wait_timeout = 5
server_connect_timeout = 5
server_login_retry = 1
EOF
exec pgbouncer /tmp/j2m-pool.ini
"""
        subprocess.run(["docker", "run", "-d", "--name", pooler, "--network", network,
                        "--entrypoint", "sh", "j2m-pgbouncer:synthetic", "-ec", start_pooler],
                       check=True, capture_output=True, timeout=30)

        def pool_control(statement):
            return run("env", "PGPASSWORD=synthetic-only", "psql", "-XAt", "-v", "ON_ERROR_STOP=1",
                       "-h", pooler, "-p", "6432", "-U", "j2m_pool_admin", "-d", "pgbouncer",
                       "-c", statement)

        for _ in range(20):
            if pool_control("SHOW VERSION").returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Synthetic PgBouncer unavailable")
        with tempfile.TemporaryDirectory(prefix="j2m-role-tls-") as folder:
            ca, cert, key = write_certificates(Path(folder), hostnames=("db",))
            for path in (ca, cert, key):
                subprocess.run(["docker", "cp", str(path), f"{name}:/tmp/{path.name}"],
                               check=True, capture_output=True, timeout=20)
            run("sh", "-ec", "chown postgres:postgres /tmp/server.crt /tmp/server.key; "
                "chmod 600 /tmp/server.key; chmod 644 /tmp/server.crt", check=True)
            for statement in ("ALTER SYSTEM SET ssl_cert_file='/tmp/server.crt'",
                              "ALTER SYSTEM SET ssl_key_file='/tmp/server.key'",
                              "ALTER SYSTEM SET ssl='on'"):
                sql(statement).check_returncode()
            sql("SELECT pg_reload_conf()").check_returncode()
            for _ in range(20):
                if sql("SHOW ssl").stdout.strip() == b"on":
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError("Synthetic PostgreSQL did not enable TLS")
            with ExitStack() as operation:
                exercise_application_schema(network, run, sql, connection, pool_control, ca, operation)
        print("Synthetic per-role RLS, read-only dump coverage, durable writer fence and migrator verified.")
    finally:
        subprocess.run(["docker", "rm", "-f", name, pooler], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=30, check=True)


if __name__ == "__main__":
    main()
