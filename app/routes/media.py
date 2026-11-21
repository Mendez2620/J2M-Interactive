from io import BytesIO

from flask import Blueprint, abort, make_response, send_file

from app.media_storage import MediaStorageError, get_media_storage, parse_managed_media_key
from app.models import Business


media_bp = Blueprint("media", __name__, url_prefix="/media")
MIME_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


@media_bp.get("/<path:asset_key>")
def public_media(asset_key):
    try:
        parts = parse_managed_media_key(asset_key)
        business_id = int(parts["business_id"])
    except (MediaStorageError, ValueError):
        abort(404)
    business = Business.query.filter_by(id=business_id).first()
    if not business or asset_key not in {business.logo_path, business.cover_path}:
        abort(404)
    try:
        content = get_media_storage().read_bytes(asset_key, expected_business_id=business_id)
    except (MediaStorageError, OSError):
        abort(404)
    response = make_response(send_file(
        BytesIO(content), mimetype=MIME_TYPES[parts["extension"]],
        download_name=asset_key.rsplit("/", 1)[-1], conditional=True, max_age=31536000,
    ))
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
