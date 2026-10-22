import os
import click
from app import create_app
from app.extensions import db

app = create_app(os.getenv("FLASK_ENV", "development"))

@app.cli.command("init-db")
def init_db():
    """Development-only helper; existing databases must use `flask db upgrade`."""
    if app.config.get("APP_ENV") == "production":
        raise click.ClickException("init-db está bloqueado en producción; use migraciones Alembic controladas.")
    with app.app_context():
        db.create_all()
        print("Database tables created successfully!")


@app.cli.command("seed-db")
@click.option("--yes-i-understand-data-will-be-deleted", is_flag=True)
def seed_db(yes_i_understand_data_will_be_deleted):
    """CLI Command to drop, recreate, and seed the database."""
    if app.config.get("APP_ENV") == "production":
        raise click.ClickException("seed-db está bloqueado en producción.")
    if not yes_i_understand_data_will_be_deleted:
        raise click.ClickException("Este comando elimina datos. Use la confirmación explícita solo en desarrollo aislado.")
    from seed import reset_and_seed_database
    reset_and_seed_database()


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", 5000))
    debug = False if os.getenv("FLASK_ENV", "development") == "production" else os.getenv("FLASK_DEBUG", "True").lower() in ("true", "1", "t")

    print(f"Starting J2M Interactive Loyalty SaaS on http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)
