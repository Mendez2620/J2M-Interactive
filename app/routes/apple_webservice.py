import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request, send_file
from app.extensions import db
from app.models import Customer, AppleDevice
from app.services import AppleWalletService

logger = logging.getLogger(__name__)

apple_ws_bp = Blueprint("apple_webservice", __name__, url_prefix="/api/apple/v1")


def _get_auth_token_from_header() -> str:
    """Extracts the authentication token from 'Authorization: ApplePass <token>' header."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("ApplePass "):
        return auth_header.replace("ApplePass ", "").strip()
    return auth_header.strip()


def _resolve_customer_by_serial(serial_number: str) -> Customer:
    """Resolves a Customer instance from Apple Wallet pass serial number."""
    if not serial_number:
        return None
    # Serial numbers are formatted as 'customer_{id}_{token_suffix}'
    if serial_number.startswith("customer_"):
        parts = serial_number.split("_")
        if len(parts) >= 2 and parts[1].isdigit():
            customer_id = int(parts[1])
            return db.session.get(Customer, customer_id)
    return Customer.query.filter(Customer.qr_code_token.startswith(serial_number)).first()


@apple_ws_bp.route("/devices/<device_id>/registrations/<pass_type_id>/<serial_number>", methods=["POST"])
def register_device(device_id: str, pass_type_id: str, serial_number: str):
    """
    Registers an Apple device to receive push notifications for a pass.
    """
    auth_token = _get_auth_token_from_header()
    customer = _resolve_customer_by_serial(serial_number)

    if not customer or customer.qr_code_token != auth_token:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    push_token = data.get("pushToken")
    if not push_token:
        return jsonify({"error": "Missing pushToken"}), 400

    device = AppleDevice.query.filter_by(
        device_library_identifier=device_id,
        pass_type_identifier=pass_type_id,
        serial_number=serial_number,
    ).first()

    status_code = 200
    if not device:
        device = AppleDevice(
            customer_id=customer.id,
            device_library_identifier=device_id,
            pass_type_identifier=pass_type_id,
            serial_number=serial_number,
            push_token=push_token,
        )
        db.session.add(device)
        status_code = 201
    else:
        device.push_token = push_token
        device.updated_at = datetime.now(timezone.utc)

    db.session.commit()
    return "", status_code


@apple_ws_bp.route("/devices/<device_id>/registrations/<pass_type_id>/<serial_number>", methods=["DELETE"])
def unregister_device(device_id: str, pass_type_id: str, serial_number: str):
    """
    Unregisters an Apple device when a pass is removed from Apple Wallet.
    """
    auth_token = _get_auth_token_from_header()
    customer = _resolve_customer_by_serial(serial_number)

    if not customer or customer.qr_code_token != auth_token:
        return jsonify({"error": "Unauthorized"}), 401

    device = AppleDevice.query.filter_by(
        device_library_identifier=device_id,
        pass_type_identifier=pass_type_id,
        serial_number=serial_number,
    ).first()

    if device:
        db.session.delete(device)
        db.session.commit()

    return "", 200


@apple_ws_bp.route("/devices/<device_id>/passes/<pass_type_id>", methods=["GET"])
def get_updated_passes(device_id: str, pass_type_id: str):
    """
    Returns serial numbers for passes associated with a device that were updated.
    """
    passes_updated_since = request.args.get("passesUpdatedSince")
    devices_query = AppleDevice.query.filter_by(
        device_library_identifier=device_id,
        pass_type_identifier=pass_type_id,
    )

    if passes_updated_since:
        try:
            since_dt = datetime.fromisoformat(passes_updated_since.replace("Z", "+00:00"))
            devices_query = devices_query.filter(AppleDevice.updated_at > since_dt)
        except Exception:
            pass

    devices = devices_query.all()
    if not devices:
        return "", 204

    serial_numbers = [d.serial_number for d in devices]
    last_updated = datetime.now(timezone.utc).isoformat()

    return jsonify({
        "lastUpdated": last_updated,
        "serialNumbers": serial_numbers,
    }), 200


@apple_ws_bp.route("/passes/<pass_type_id>/<serial_number>", methods=["GET"])
def deliver_pass(pass_type_id: str, serial_number: str):
    """
    Delivers the latest version of a pass (.pkpass file) to the Apple device.
    """
    auth_token = _get_auth_token_from_header()
    customer = _resolve_customer_by_serial(serial_number)

    if not customer or customer.qr_code_token != auth_token:
        return jsonify({"error": "Unauthorized"}), 401

    apple_service = AppleWalletService()
    pkpass_stream = apple_service.generate_pkpass(customer)
    filename = f"{customer.business.slug}-pass.pkpass"

    response = send_file(
        pkpass_stream,
        mimetype="application/vnd.apple.pkpass",
        as_attachment=True,
        download_name=filename,
    )
    response.headers["Last-Modified"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    return response


@apple_ws_bp.route("/log", methods=["POST"])
def log_apple_message():
    """
    Receives diagnostic logs from Apple Wallet client.
    """
    data = request.get_json(silent=True) or {}
    logs = data.get("logs", [])
    for msg in logs:
        logger.info("[Apple Wallet Client Log] %s", msg)
    return "", 200


def notify_apple_wallet_devices(customer: Customer) -> int:
    """
    Finds all registered Apple devices for a customer and sends silent APNs push notifications.
    Returns the count of devices notified.
    """
    devices = AppleDevice.query.filter_by(customer_id=customer.id).all()
    apple_service = AppleWalletService()
    notified_count = 0

    for dev in devices:
        if dev.push_token:
            apple_service.send_push_notification(dev.push_token)
            notified_count += 1

    return notified_count
