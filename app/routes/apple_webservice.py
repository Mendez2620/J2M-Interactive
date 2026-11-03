import logging
import re
from datetime import datetime, timezone
from email.utils import format_datetime

from flask import Blueprint, current_app, jsonify, request, send_file
from itsdangerous import BadData, URLSafeSerializer

from app.extensions import db
from app.models import AppleDevice, ApplePassCredential, Customer
from app.services import AppleWalletService
from app.services.referral_participant_service import prepare_customer_wallet_invitation


logger = logging.getLogger(__name__)
apple_ws_bp = Blueprint("apple_webservice", __name__, url_prefix="/api/apple/v1")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{8,160}$")


def _auth_token():
    header = request.headers.get("Authorization", "")
    return header[10:].strip() if header.startswith("ApplePass ") else ""


def _marker_serializer():
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="apple-passkit-update-marker-v1")


def _encode_marker(value):
    return _marker_serializer().dumps({"v": int(value)})


def _decode_marker(value):
    if not value:
        return 0
    try:
        payload = _marker_serializer().loads(value)
        marker = payload.get("v")
        return marker if isinstance(marker, int) and marker >= 0 else None
    except (BadData, AttributeError):
        return None


def _credential(pass_type_id, serial_number):
    if not SAFE_IDENTIFIER.fullmatch(pass_type_id or "") or not SAFE_IDENTIFIER.fullmatch(serial_number or ""):
        return None
    return ApplePassCredential.query.filter_by(
        pass_type_identifier=pass_type_id, serial_number=serial_number, revoked_at=None,
    ).one_or_none()


def _authorized_credential(pass_type_id, serial_number):
    credential = _credential(pass_type_id, serial_number)
    if not credential:
        return None
    customer = db.session.get(Customer, credential.customer_id)
    if (not customer or customer.business_id != credential.business_id or
            not AppleWalletService(pass_type_id=pass_type_id).authenticate(customer, _auth_token(), credential)):
        return None
    return credential


@apple_ws_bp.post("/devices/<device_id>/registrations/<pass_type_id>/<serial_number>")
def register_device(device_id, pass_type_id, serial_number):
    credential = _authorized_credential(pass_type_id, serial_number)
    if not credential or not SAFE_IDENTIFIER.fullmatch(device_id or ""):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True)
    push_token = data.get("pushToken") if isinstance(data, dict) else None
    if not isinstance(push_token, str) or not SAFE_IDENTIFIER.fullmatch(push_token):
        return jsonify({"error": "Invalid request"}), 400
    device = AppleDevice.query.filter_by(
        device_library_identifier=device_id, pass_type_identifier=pass_type_id,
        serial_number=serial_number,
    ).one_or_none()
    created = device is None
    if created:
        device = AppleDevice(
            customer_id=credential.customer_id, device_library_identifier=device_id,
            pass_type_identifier=pass_type_id, serial_number=serial_number, push_token=push_token,
        )
        db.session.add(device)
    else:
        if device.customer_id != credential.customer_id:
            return jsonify({"error": "Unauthorized"}), 401
        device.push_token = push_token
        device.is_active = True
        device.invalidated_at = None
        device.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return "", 201 if created else 200


@apple_ws_bp.delete("/devices/<device_id>/registrations/<pass_type_id>/<serial_number>")
def unregister_device(device_id, pass_type_id, serial_number):
    credential = _authorized_credential(pass_type_id, serial_number)
    if not credential:
        return jsonify({"error": "Unauthorized"}), 401
    device = AppleDevice.query.filter_by(
        device_library_identifier=device_id, pass_type_identifier=pass_type_id,
        serial_number=serial_number, customer_id=credential.customer_id,
    ).one_or_none()
    if device:
        device.is_active = False
        device.invalidated_at = datetime.now(timezone.utc)
        db.session.commit()
    return "", 200


@apple_ws_bp.get("/devices/<device_id>/registrations/<pass_type_id>")
@apple_ws_bp.get("/devices/<device_id>/passes/<pass_type_id>")
def get_updated_passes(device_id, pass_type_id):
    if not SAFE_IDENTIFIER.fullmatch(device_id or "") or not SAFE_IDENTIFIER.fullmatch(pass_type_id or ""):
        return "", 204
    since = _decode_marker(request.args.get("passesUpdatedSince"))
    if since is None:
        return jsonify({"error": "Invalid update marker"}), 400
    rows = db.session.query(AppleDevice, ApplePassCredential).join(
        ApplePassCredential,
        (ApplePassCredential.customer_id == AppleDevice.customer_id) &
        (ApplePassCredential.pass_type_identifier == AppleDevice.pass_type_identifier) &
        (ApplePassCredential.serial_number == AppleDevice.serial_number),
    ).filter(
        AppleDevice.device_library_identifier == device_id,
        AppleDevice.pass_type_identifier == pass_type_id,
        AppleDevice.is_active.is_(True), ApplePassCredential.revoked_at.is_(None),
        ApplePassCredential.update_tag > since,
    ).all()
    if not rows:
        return "", 204
    newest = max(credential.update_tag for _, credential in rows)
    return jsonify({"lastUpdated": _encode_marker(newest),
                    "serialNumbers": sorted({device.serial_number for device, _ in rows})}), 200


@apple_ws_bp.get("/passes/<pass_type_id>/<serial_number>")
def deliver_pass(pass_type_id, serial_number):
    credential = _authorized_credential(pass_type_id, serial_number)
    if not credential:
        return jsonify({"error": "Unauthorized"}), 401
    customer = db.session.get(Customer, credential.customer_id)
    if not customer or not customer.business or not customer.business.is_active:
        return jsonify({"error": "Unauthorized"}), 401
    prepare_customer_wallet_invitation(customer)
    service = AppleWalletService(pass_type_id=pass_type_id)
    if not service.pass_generation_allowed():
        return jsonify({"error": "Apple Wallet is unavailable"}), 503
    modified_at = credential.updated_at
    if modified_at.tzinfo is None:
        modified_at = modified_at.replace(tzinfo=timezone.utc)
    modified_at = modified_at.astimezone(timezone.utc).replace(microsecond=0)
    last_modified = format_datetime(modified_at, usegmt=True)
    if request.if_modified_since:
        requested_at = request.if_modified_since
        if requested_at.tzinfo is None:
            requested_at = requested_at.replace(tzinfo=timezone.utc)
        if requested_at.astimezone(timezone.utc) >= modified_at:
            response = current_app.make_response(("", 304))
            response.headers["Last-Modified"] = last_modified
            response.headers["Cache-Control"] = "no-store, private"
            return response
    stream = service.generate_pkpass(customer)
    # PassKit fetches a pass independently of a particular registered device.
    # Record only that this version was served; never claim every device installed it.
    credential.last_served_at = datetime.now(timezone.utc)
    credential.last_served_update_tag = credential.update_tag
    credential.updated_at = modified_at
    db.session.commit()
    response = send_file(stream, mimetype="application/vnd.apple.pkpass", as_attachment=True,
                         download_name=f"{customer.business.slug}-pass.pkpass")
    response.headers["Last-Modified"] = last_modified
    response.headers["Cache-Control"] = "no-store, private"
    return response


@apple_ws_bp.post("/log")
def log_apple_message():
    data = request.get_json(silent=True)
    logs = data.get("logs") if isinstance(data, dict) else None
    if not isinstance(logs, list):
        return jsonify({"error": "Invalid logs"}), 400
    logger.info("Apple Wallet client reported %d diagnostic message(s)", min(len(logs), 50))
    return "", 200


def notify_apple_wallet_devices(customer):
    from app.services.apple_wallet_update_service import enqueue_apple_pass_update
    count = enqueue_apple_pass_update(customer, reason="balance")
    if count:
        db.session.commit()
    return count
