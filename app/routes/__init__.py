from flask import Blueprint, jsonify
from app.routes.auth import auth_bp
from app.routes.business import business_bp
from app.routes.customer import customer_bp
from app.routes.scanner import scanner_bp
from app.routes.admin import admin_bp
from app.routes.manager import manager_bp
from app.routes.apple_webservice import apple_ws_bp

# Health Check Blueprint or root API routes
main_bp = Blueprint("main", __name__)


@main_bp.route("/api/health", methods=["GET"])
def health_check():
    """Health check endpoint to verify backend service status."""
    return (
        jsonify({"status": "ok", "app": "J2M Interactive Loyalty SaaS"}),
        200,
    )


def register_routes(app):
    """Register all blueprints with the Flask application."""
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(business_bp)
    app.register_blueprint(customer_bp)
    app.register_blueprint(scanner_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(manager_bp)
    app.register_blueprint(apple_ws_bp)
