"""Explicitly authorized, separate session-pooler diagnostic; never run by preflight.

No table writes. One random negative bigint session advisory key, two TLS
connections. A non-affine backend may retain the diagnostic lock until its
session ends; never terminate a foreign session or unlock unrelated keys.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
from secrets import randbelow
from urllib.parse import parse_qsl, unquote, urlsplit

import psycopg2

from scripts.selfhost_transition_preflight import read_config


def connection_parameters(config, expected_host, expected_project_ref):
    url = urlsplit(config["DATABASE_URL"])
    if (not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.pooler\.supabase\.com", expected_host)
            or not re.fullmatch(r"[a-z0-9]{20}", expected_project_ref)
            or url.scheme not in {"postgres", "postgresql"}
            or url.hostname != expected_host or url.port not in {None, 5432}
            or url.path != "/postgres" or url.fragment
            or unquote(url.username or "") != "postgres." + expected_project_ref
            or not url.password):
        raise ValueError("Expected exact Supabase session-pooler destination")
    pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
    if len({k for k, _ in pairs}) != len(pairs) or any(
            k not in {"sslmode", "sslrootcert"} for k, _ in pairs):
        raise ValueError("Unexpected database URL parameters")
    ca = Path(config["DATABASE_SSL_ROOT_CERT"])
    if ca.is_symlink() or not ca.is_file():
        raise ValueError("Expected local CA file")
    if any(name.upper().startswith("PG") for name in os.environ):
        raise ValueError("Unexpected inherited PostgreSQL environment")
    return {"host": expected_host, "port": 5432, "dbname": "postgres",
            "user": unquote(url.username), "password": unquote(url.password),
            "sslmode": "verify-full", "sslrootcert": str(ca.resolve()),
            "connect_timeout": 5, "application_name": "j2m-affinity-diagnostic",
            "tcp_user_timeout": 10000, "keepalives_idle": 5,
            "keepalives_interval": 2, "keepalives_count": 3}


def scalar(conn, sql, parameters=()):
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '5s'")
        cur.execute(sql, parameters)
        return cur.fetchone()[0]


def check_session_affinity(connect, parameters):
    """Check and release only this probe's key; no session termination."""
    key = -(2**62 + randbelow(2**60))
    owner = peer = None
    owner_pid = peer_pid = None
    owner_locked = peer_locked = False
    # Mark attempted acquisitions before network I/O: an ambiguous response
    # requires explicit cleanup, not an assumption that no lock was taken.
    owner_attempted = peer_attempted = False
    cleanup_uncertain = False
    passed = False
    try:
        owner = connect(**parameters)
        peer = connect(**parameters)
        owner.set_session(readonly=True, autocommit=False)
        peer.set_session(readonly=True, autocommit=False)
        owner_pid = scalar(owner, "SELECT pg_backend_pid()")
        peer_pid = scalar(peer, "SELECT pg_backend_pid()")
        if owner_pid == peer_pid:
            raise RuntimeError("Expected separate sessions")
        owner_attempted = True
        owner_locked = scalar(owner, "SELECT pg_try_advisory_lock(%s)", (key,))
        owner_attempted = owner_locked
        if not owner_locked:
            raise RuntimeError("Diagnostic key occupied")
        owner.commit()
        if scalar(owner, "SELECT pg_backend_pid()") != owner_pid:
            raise RuntimeError("Owner session affinity not demonstrated")
        peer_attempted = True
        peer_locked = scalar(peer, "SELECT pg_try_advisory_lock(%s)", (key,))
        peer_attempted = peer_locked
        if peer_locked:
            raise RuntimeError("Peer unexpectedly acquired key")
        peer.commit()
        if scalar(peer, "SELECT pg_backend_pid()") != peer_pid:
            raise RuntimeError("Peer session affinity not demonstrated")
        if not scalar(owner, "SELECT pg_advisory_unlock(%s)", (key,)):
            raise RuntimeError("Owner could not release diagnostic key")
        owner_attempted = owner_locked = False
        owner.commit()
        peer_attempted = True
        peer_locked = scalar(peer, "SELECT pg_try_advisory_lock(%s)", (key,))
        peer_attempted = peer_locked
        if not peer_locked:
            raise RuntimeError("Diagnostic key unavailable after release")
        if not scalar(peer, "SELECT pg_advisory_unlock(%s)", (key,)):
            raise RuntimeError("Peer could not release diagnostic key")
        peer_attempted = peer_locked = False
        peer.commit()
        passed = True
    except Exception:
        pass
    finally:
        for conn, expected_pid, attempted in ((owner, owner_pid, owner_attempted),
                                               (peer, peer_pid, peer_attempted)):
            if conn is None:
                continue
            try:
                if attempted:
                    conn.rollback()
                    if scalar(conn, "SELECT pg_backend_pid()") != expected_pid:
                        cleanup_uncertain = True
                    elif not scalar(conn, "SELECT pg_advisory_unlock(%s)", (key,)):
                        cleanup_uncertain = True
                    conn.commit()
            except Exception:
                cleanup_uncertain = True
            finally:
                try:
                    conn.close()
                except Exception:
                    cleanup_uncertain = True
    return {"session_affinity_verified": passed and not cleanup_uncertain,
            "diagnostic_cleanup_uncertain": cleanup_uncertain}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env.selfhost"))
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--expected-project-ref", required=True)
    parser.add_argument("--authorize-session-lock-probe", action="store_true")
    args = parser.parse_args()
    if not args.authorize_session_lock_probe:
        print("Blocked: separate owner authorization required.")
        return 2
    try:
        parameters = connection_parameters(read_config(args.env_file), args.expected_host,
                                           args.expected_project_ref)
        result = check_session_affinity(psycopg2.connect, parameters)
    except Exception:
        print("Pooler diagnostic blocked; private details suppressed.")
        return 1
    if result["diagnostic_cleanup_uncertain"]:
        print("NO-GO: diagnostic lock cleanup unconfirmed; do not terminate sessions.")
        return 1
    if not result["session_affinity_verified"]:
        print("NO-GO: session affinity not verified.")
        return 1
    print("Session affinity and diagnostic key release verified; not deployment authorization.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
