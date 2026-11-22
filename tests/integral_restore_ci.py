"""Docker/PostgreSQL integration probe for the authenticated DB+media restore path."""

import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
import tempfile
from unittest.mock import patch


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    source_url = os.environ["TEST_POSTGRES_URL"]
    os.environ.update(DATABASE_URL=source_url, DATABASE_SSLMODE="disable",
                      J2M_SYNTHETIC_BACKUP_TEST="1")
    from app import create_app
    from app.extensions import db
    from app.models import Business
    from scripts.selfhost_backup import integral_backup, restore_integral_isolated

    app = create_app("development")
    with tempfile.TemporaryDirectory() as temporary, app.app_context():
        root = Path(temporary)
        media = root / "media"
        output = root / "backups"
        business = Business.query.filter_by(slug="integral-restore-ci").first()
        if business is None:
            business = Business(name="Integral Restore CI", slug="integral-restore-ci")
            db.session.add(business)
            db.session.commit()
        current_key = f"businesses/{business.id}/logo/" + "a" * 32 + ".png"
        old_key = f"businesses/{business.id}/logo/" + "b" * 32 + ".png"
        for key, content in ((current_key, b"current"), (old_key, b"historical")):
            path = media / Path(*key.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        marker = media / Path(*old_key.split("/"))
        marker.with_name(f".{marker.name}.retained.json").write_text(json.dumps({
            "schema": 1, "storage_key": old_key, "business_id": business.id,
            "superseded_by_design_version": 2,
            "retired_at": "2026-11-21T00:00:00+00:00",
        }), encoding="utf-8")
        business.logo_path = current_key
        db.session.commit()
        env_file = root / "synthetic.env"
        source_port = urlsplit(source_url).port or 5432
        env_file.write_text(
            f"DATABASE_URL=postgresql://postgres:postgres@j2m-backup-source:{source_port}/j2m_test?sslmode=disable\n",
            encoding="utf-8",
        )
        key_file = root / "synthetic.dpapi"
        synthetic_key = bytes(range(32))
        with patch("scripts.selfhost_backup.load_dpapi_key", return_value=synthetic_key):
            index = integral_backup(env_file, media, output, key_file, retries=1,
                                    allow_insecure_synthetic=True, timeout=180)
            report = restore_integral_isolated(
                index, key_file, "j2m-interactive:" + "0" * 39 + "1", timeout=180,
            )
        if report.businesses < 1:
            raise RuntimeError("La restauración integral no recuperó el negocio sintético.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
