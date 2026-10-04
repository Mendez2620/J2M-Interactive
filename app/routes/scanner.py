from flask import Blueprint, jsonify

scanner_bp = Blueprint("scanner", __name__, url_prefix="/api/scanner")


@scanner_bp.route("/ping", methods=["GET"])
def ping():
    return jsonify({"message": "Scanner module ready"}), 200
