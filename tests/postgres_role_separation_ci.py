"""Synthetic PostgreSQL exercise for split roles and a durable writer fence."""

import subprocess
import time
from uuid import uuid4

SETUP = """
CREATE ROLE j2m_owner NOLOGIN;
CREATE ROLE j2m_migrator LOGIN NOINHERIT PASSWORD 'synthetic-only';
GRANT j2m_owner TO j2m_migrator;
CREATE ROLE j2m_web LOGIN PASSWORD 'synthetic-only';
CREATE ROLE j2m_worker LOGIN PASSWORD 'synthetic-only';
CREATE ROLE j2m_backup LOGIN PASSWORD 'synthetic-only';
CREATE SCHEMA rehearsal AUTHORIZATION j2m_owner;
SET ROLE j2m_owner;
CREATE TABLE rehearsal.ledger (id bigint GENERATED ALWAYS AS IDENTITY, tenant int, balance int);
ALTER TABLE rehearsal.ledger ENABLE ROW LEVEL SECURITY;
INSERT INTO rehearsal.ledger (tenant, balance) VALUES (1, 2), (2, 3);
GRANT USAGE ON SCHEMA rehearsal TO j2m_web,j2m_worker,j2m_backup;
GRANT SELECT, INSERT ON rehearsal.ledger TO j2m_web,j2m_worker;
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
REVOKE INSERT ON rehearsal.ledger FROM j2m_web,j2m_worker;
REVOKE USAGE ON SEQUENCE rehearsal.ledger_id_seq FROM j2m_web,j2m_worker;
LOCK TABLE rehearsal.ledger IN ACCESS EXCLUSIVE MODE;
COMMIT;
"""


def main():
    name = "j2m-roles-" + uuid4().hex[:8]
    subprocess.run(["docker", "run", "-d", "--name", name, "--network", "none",
                    "-e", "POSTGRES_PASSWORD=synthetic-only", "-e", "POSTGRES_DB=j2m_test",
                    "postgres:17-alpine"], check=True, capture_output=True, timeout=60)

    def run(*args, **kwargs):
        return subprocess.run(["docker", "exec", "-i", name, *args], capture_output=True,
                              timeout=20, **kwargs)

    def sql(statement, role="postgres", database="j2m_test"):
        return run("env", "PGPASSWORD=synthetic-only", "psql", "-XAt", "-v", "ON_ERROR_STOP=1",
                   "-h", "127.0.0.1", "-U", role, "-d", database, "-c", statement)

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
                                 "psql", "-XAt", "-h", "127.0.0.1", "-U", "j2m_web", "-d", "j2m_test"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            peer.stdin.write(b"SELECT 91;\n")
            peer.stdin.flush()
            assert peer.stdout.readline().strip() == b"91"
            sql(FENCE).check_returncode()
            assert sql("SELECT 1", "j2m_web").returncode
            assert sql("SELECT 1", "j2m_worker").returncode
            out, errors = peer.communicate(b"INSERT INTO rehearsal.ledger (tenant,balance) VALUES (1,9);\n",
                                           timeout=8)
            assert b"permission denied" in errors
            sql("SET ROLE j2m_owner; ALTER TABLE rehearsal.ledger ADD COLUMN note text",
                "j2m_migrator").check_returncode()
            assert sql(query).stdout.strip() == source
        finally:
            if peer.poll() is None:
                peer.kill()
                peer.wait(timeout=8)
        print("Synthetic per-role RLS, read-only dump coverage, durable writer fence and migrator verified.")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30, check=True)


if __name__ == "__main__":
    main()
