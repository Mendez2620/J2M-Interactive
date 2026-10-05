import os
from flask import Flask
from app.config import config_by_name
from app.extensions import db, migrate, cors
from app.routes import register_routes


def create_app(config_name=None):
    """Application factory for J2M Interactive Loyalty SaaS."""
    if config_name is None:
        config_name = os.getenv("FLASK_ENV", "development")

    app = Flask(__name__)
    config_class = config_by_name.get(config_name, config_by_name["default"])
    app.config.from_object(config_class)

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    cors.init_app(app, resources={r"/api/*": {"origins": app.config.get("CORS_ORIGINS", "*")}})

    # Import models so SQLAlchemy / Alembic are aware of them
    from app import models  # noqa: F401

    # Register routes / blueprints
    register_routes(app)

    # Register CLI commands
    @app.cli.command('check-birthdays')
    def check_birthdays():
        """Execute the daily birthday campaign."""
        from app.services.birthday_service import BirthdayService
        sent = BirthdayService.run_daily_campaign()
        print(f'Sent {sent} birthday greetings')
    return app
