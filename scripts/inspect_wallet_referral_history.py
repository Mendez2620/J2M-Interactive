"""Read-only, bounded inspection of local referral history."""
import argparse
import json
import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select, text

from app.config import normalize_database_url
from app.models import Business, CustomerReferralActivation, GoogleWalletReferralJob, ReferralParticipant
from app.models.google_wallet_write_reservation import table as reservations


def inspect_history(session, business_id, *, after=0, capability_after=0, limit=25):
    if not 1 <= limit <= 100 or business_id < 1 or min(after, capability_after) < 0:
        raise ValueError("Invalid tenant or pagination")
    business = session.execute(select(Business.is_active, Business.referrals_enabled)
                               .where(Business.id == business_id)).one_or_none()
    if business is None:
        raise ValueError("Business unavailable")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    p = ReferralParticipant
    c = CustomerReferralActivation
    j = GoogleWalletReferralJob
    rows = session.execute(select(p.id, p.is_enabled, p.revoked_at, p.consented_at,
                                  p.consent_version.is_not(None), p.consent_evidence_hash.is_not(None))
                           .where(p.business_id == business_id, p.participant_type == "customer_ambassador",
                                  p.id > after).order_by(p.id).limit(limit + 1)).all()
    capabilities = session.execute(select(c.id, c.participant_id, c.expires_at, c.consumed_at)
                                   .where(c.business_id == business_id, c.id > capability_after)
                                   .order_by(c.id).limit(limit + 1)).all()
    participants = []
    for item, enabled, revoked, consented, version, evidence in rows[:limit]:
        status = ("revoked" if revoked is not None else "no_consent" if not (consented and version and evidence)
                  else "active" if enabled else "disabled")
        participants.append({"id": item, "state": status})
    jobs = session.execute(select(j.status, func.count()).where(
        j.business_id == business_id, j.status.in_(("pending", "processing"))).group_by(j.status)).all()
    active = session.execute(select(reservations.c.state, func.count()).where(
        reservations.c.business_id == business_id, reservations.c.active_slot == 1)
        .group_by(reservations.c.state)).all()
    return {
        "business_id": business_id, "business_active": business.is_active,
        "program_enabled": business.referrals_enabled,
        "participants": participants, "next_after": rows[limit - 1].id if len(rows) > limit else None,
        "capabilities": [
            {"id": item, "participant_id": participant,
             "state": "consumed" if consumed is not None else "expired" if expires <= now else "current",
             "expires_at": expires.isoformat()}
            for item, participant, expires, consumed in capabilities[:limit]
        ],
        "next_capability_after": capabilities[limit - 1].id if len(capabilities) > limit else None,
        "jobs": dict(jobs), "reservations": dict(active),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", type=int, required=True)
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--capability-after", type=int, default=0)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    engine = None
    try:
        engine = create_engine(normalize_database_url(os.environ["DATABASE_URL"]),
                               connect_args={"connect_timeout": 5, "sslmode": "verify-full"})
        with engine.begin() as connection:
            connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout = '5s'"))
            print(json.dumps(inspect_history(connection, args.business_id, after=args.after,
                                             capability_after=args.capability_after, limit=args.limit)))
    except Exception:
        print("Referral history could not be verified. Connection details suppressed.")
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
