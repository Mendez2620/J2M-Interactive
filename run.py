import os
from app import create_app
from app.extensions import db

app = create_app(os.getenv("FLASK_ENV", "development"))

# Auto-create tables in development mode if using SQLite
with app.app_context():
    db.create_all()


@app.cli.command("init-db")
def init_db():
    """CLI Command to initialize and create database tables."""
    with app.app_context():
        db.create_all()
        print("Database tables created successfully!")


@app.cli.command("seed-db")
def seed_db():
    """CLI Command to drop, recreate, and seed the database."""
    from seed import reset_and_seed_database
    reset_and_seed_database()


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "True").lower() in ("true", "1", "t")

    print(f"Starting J2M Interactive Loyalty SaaS on http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)
