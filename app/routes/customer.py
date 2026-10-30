import logging
from flask import Blueprint, jsonify, redirect, request, send_file
from app.extensions import db
from app.models import Customer
from app.services import GoogleWalletService, AppleWalletService
from app.services.google_wallet import GoogleWalletAPIError, GoogleWalletConfigurationError
from app.wallet_access import validate_wallet_token
from app.services.referral_participant_service import ensure_customer_invitation_persisted

logger = logging.getLogger(__name__)

customer_bp = Blueprint("customer", __name__, url_prefix="/api/customers")


@customer_bp.after_request
def protect_wallet_responses(response):
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@customer_bp.route("/ping", methods=["GET"])
def ping():
    return jsonify({"message": "Customer module ready"}), 200


@customer_bp.route("/<int:customer_id>/wallet/google", methods=["GET"])
def get_google_wallet_pass(customer_id: int):
    """
    Generates a Google Wallet Pass (signed JWT & save URL) for a customer.
    
    Path params:
        customer_id (int): ID of the customer
    Returns:
        JSON with the Google Wallet Save URL and JWT token.
    """
    if not request.args.get("token"):
        return jsonify({"status": "error", "message": "Token de Wallet requerido."}), 403
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return (
            jsonify({
                "status": "error",
                "message": f"Cliente con ID {customer_id} no encontrado."
            }),
            404,
        )

    if not validate_wallet_token(request.args.get("token", ""), customer):
        return jsonify({"status": "error", "message": "Token de Wallet inválido o expirado."}), 403

    if not customer.business:
        return (
            jsonify({
                "status": "error",
                "message": f"El cliente {customer_id} no tiene un negocio asociado."
            }),
            400,
        )

    try:
        ensure_customer_invitation_persisted(customer)
        wallet_service = GoogleWalletService()
        _jwt_token, save_url = wallet_service.create_jwt_pass(customer)

        if request.args.get("format") == "redirect":
            return redirect(save_url, code=302)

        response_data = {
            "status": "mock" if wallet_service.mode == "mock" else "success",
            "customer_id": customer.id,
            "business_name": customer.business.name,
            "save_url": save_url,
            "mode": wallet_service.mode,
            "message": ("Simulación de Google Wallet generada para desarrollo."
                        if wallet_service.mode == "mock" else "Enlace de Google Wallet preparado."),
        }
        return jsonify(response_data), 200
    except GoogleWalletConfigurationError:
        logger.warning("google_wallet_issuance_unavailable customer_id=%s", customer_id)
        return jsonify({"status": "unavailable", "message": "Google Wallet no está disponible temporalmente."}), 503
    except GoogleWalletAPIError as exc:
        logger.warning("google_wallet_issuance_failed customer_id=%s status_code=%s", customer_id, exc.status_code)
        return jsonify({"status": "error", "message": "No fue posible preparar Google Wallet."}), 502
    except Exception:
        logger.exception("Error generating Google Wallet pass for customer %s", customer_id)
        return (
            jsonify({
                "status": "error",
                "message": "No fue posible generar el pase de Google Wallet."
            }),
            500,
        )


@customer_bp.route("/<int:customer_id>/wallet/apple", methods=["GET"])
def get_apple_wallet_pass(customer_id: int):
    """
    Generates and returns an Apple Wallet .pkpass file for a customer.
    
    Path params:
        customer_id (int): ID of the customer
    Returns:
        Binary stream (.pkpass) with mimetype 'application/vnd.apple.pkpass'.
    """
    if not request.args.get("token"):
        return jsonify({"status": "error", "message": "Token de Wallet requerido."}), 403
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return (
            jsonify({
                "status": "error",
                "message": f"Cliente con ID {customer_id} no encontrado."
            }),
            404,
        )

    if not validate_wallet_token(request.args.get("token", ""), customer):
        return jsonify({"status": "error", "message": "Token de Wallet inválido o expirado."}), 403

    if not customer.business:
        return (
            jsonify({
                "status": "error",
                "message": f"El cliente {customer_id} no tiene un negocio asociado."
            }),
            400,
        )

    try:
        ensure_customer_invitation_persisted(customer)
        apple_service = AppleWalletService()
        pkpass_stream = apple_service.generate_pkpass(customer)
        filename = f"{customer.business.slug}-pass.pkpass"

        return send_file(
            pkpass_stream,
            mimetype="application/vnd.apple.pkpass",
            as_attachment=True,
            download_name=filename,
        )

    except Exception as e:
        logger.exception("Error generating Apple Wallet pass for customer %s", customer_id)
        return (
            jsonify({
                "status": "error",
                "message": "No fue posible generar el pase de Apple Wallet."
            }),
            500,
        )
