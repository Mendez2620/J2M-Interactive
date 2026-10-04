from flask import Blueprint, jsonify

business_bp = Blueprint("business", __name__, url_prefix="/api/business")


@business_bp.route("/ping", methods=["GET"])
def ping():
    return jsonify({"message": "Business module ready"}), 200
