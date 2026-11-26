"""Verify a session lock across commits on a dedicated synthetic database."""

import os
from secrets import randbelow

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


def probe(engine):
    key = randbelow(2**31)
    params = {"key": key}
    with engine.connect() as owner, engine.connect() as peer:
        pid = owner.scalar(text("SELECT pg_backend_pid()"))
        peer_pid = peer.scalar(text("SELECT pg_backend_pid()"))
        if pid == peer_pid:
            raise RuntimeError("Expected independent PostgreSQL sessions")
        if not owner.scalar(text("SELECT pg_try_advisory_lock(:key)"), params):
            raise RuntimeError("Synthetic advisory key unexpectedly occupied")
        owner.commit()
        try:
            if owner.scalar(text("SELECT pg_backend_pid()")) != pid:
                raise RuntimeError("Session changed across commit")
            if peer.scalar(text("SELECT pg_try_advisory_lock(:key)"), params):
                raise RuntimeError("Peer acquired a lock held by the original session")
            peer.commit()
        finally:
            if not owner.scalar(text("SELECT pg_advisory_unlock(:key)"), params):
                raise RuntimeError("Original session could not release its lock")
            owner.commit()
        if not peer.scalar(text("SELECT pg_try_advisory_lock(:key)"), params):
            raise RuntimeError("Lock remained occupied after release")
        if not peer.scalar(text("SELECT pg_advisory_unlock(:key)"), params):
            raise RuntimeError("Peer could not release the acquired lock")


def main():
    try:
        url = make_url(os.environ["TEST_POSTGRES_URL"])
        if (url.get_backend_name() != "postgresql" or url.host not in {"localhost", "127.0.0.1", "::1"}
                or url.database != "j2m_test" or url.query):
            raise ValueError("Expected dedicated synthetic PostgreSQL")
        engine = create_engine(url.set(drivername="postgresql+psycopg2"), poolclass=NullPool,
                               connect_args={"connect_timeout": 5})
        try:
            probe(engine)
        finally:
            engine.dispose()
    except Exception as exc:
        print(f"PostgreSQL session affinity not verified ({type(exc).__name__}).")
        return 1
    print("PostgreSQL synthetic session affinity verified across commit and release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
