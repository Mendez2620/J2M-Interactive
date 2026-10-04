import logging
from flask import Blueprint, jsonify, request
from app.extensions import db
from app.models import Customer
from app.services import GoogleWalletService

logger = logging.getLogger(__name__)

customer_bp = Blueprint("customer", __name__, url_prefix="/api/customers")


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
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return (
            jsonify({
                "status": "error",
                "message": f"Cliente con ID {customer_id} no encontrado."
            }),
            404,
        )

    if not customer.business:
        return (
            jsonify({
                "status": "error",
                "message": f"El cliente {customer_id} no tiene un negocio asociado."
            }),
            400,
        )

    try:
        wallet_service = GoogleWalletService()
        jwt_token, save_url = wallet_service.create_jwt_pass(customer)

        response_data = {
            "status": "success",
            "customer_id": customer.id,
            "business_name": customer.business.name,
            "save_url": save_url,
            "jwt": jwt_token,
            "is_dev_mock_key": wallet_service.is_mock_key,
            "message": "Enlace de pase de Google Wallet generado correctamente.",
        }

        if wallet_service.is_mock_key:
            response_data["notice"] = (
                "Se utilizó una clave RSA temporal para desarrollo. "
                "Para producción, configura GOOGLE_ISSUER_ID, GOOGLE_SA_EMAIL "
                "y GOOGLE_SA_PRIVATE_KEY en las variables de entorno."
            )

        return jsonify(response_data), 200

    except Exception as e:
        logger.exception("Error generating Google Wallet pass for customer %s", customer_id)
        return (
            jsonify({
                "status": "error",
                "message": f"Error al generar el pase de Google Wallet: {str(e)}"
            }),
            500,
        )
