from flask import Blueprint, jsonify

customer_bp = Blueprint("customer", __name__, url_prefix="/api/customers")


@customer_bp.route("/ping", methods=["GET"])
def ping():
    return jsonify({"message": "Customer module ready"}), 200
