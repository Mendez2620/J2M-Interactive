from flask import Blueprint, current_app, jsonify, make_response, render_template, send_from_directory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from app.extensions import db
from app.routes.auth import auth_bp
from app.routes.business import business_bp
from app.routes.customer import customer_bp
from app.routes.scanner import scanner_bp
from app.routes.staff_portal import staff_portal_bp
from app.routes.admin import admin_bp
from app.routes.manager import manager_bp
from app.routes.apple_webservice import apple_ws_bp
from app.routes.customer_recovery import recovery_bp
from app.routes.promoter import promoter_bp

# Health Check Blueprint or root API routes
main_bp = Blueprint("main", __name__)


@main_bp.route("/staff-access", methods=["GET"])
def staff_access():
    return render_template("staff/access.html")


@main_bp.route("/api/health", methods=["GET"])
def health_check():
    """Health check endpoint to verify backend service status."""
    return (
        jsonify({"status": "ok", "app": "J2M Interactive Loyalty SaaS"}),
        200,
    )


@main_bp.route("/api/ready", methods=["GET"])
def readiness_check():
    """Read-only readiness probe for the critical database dependency."""
    try:
        db.session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        db.session.rollback()
        return jsonify({"status": "unavailable", "database": "unavailable"}), 503
    return jsonify({"status": "ready", "database": "available"}), 200


@main_bp.route("/service-worker.js", methods=["GET"])
def service_worker():
    response = make_response(send_from_directory(current_app.static_folder, "service-worker.js"))
    response.headers["Content-Type"] = "application/javascript; charset=utf-8"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


def register_routes(app):
    """Register all blueprints with the Flask application."""
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(business_bp)
    app.register_blueprint(customer_bp)
    app.register_blueprint(scanner_bp)
    app.register_blueprint(staff_portal_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(manager_bp)
    app.register_blueprint(apple_ws_bp)
    app.register_blueprint(recovery_bp)
    app.register_blueprint(promoter_bp)
