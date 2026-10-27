import os
import argparse
from datetime import date, datetime, timezone
from pathlib import Path
from sqlalchemy.engine import make_url
from app import create_app
from app.config import BASE_DIR
from app.extensions import db
from app.models import Business, BusinessCategory, Customer, Staff, Transaction, AppleDevice

app = create_app(os.getenv("FLASK_ENV", "development"))


def _assert_safe_seed_target(*, confirmed=False):
    """Fail closed unless this is an explicitly confirmed local SQLite database."""
    environment = str(app.config.get("APP_ENV") or os.getenv("FLASK_ENV") or "").strip().lower()
    if environment != "development":
        raise RuntimeError("El reinicio de datos solo está permitido en desarrollo local.")
    if not confirmed:
        raise RuntimeError("El reinicio requiere confirmación explícita en desarrollo local.")

    try:
        database_url = make_url(str(app.config.get("SQLALCHEMY_DATABASE_URI") or ""))
    except Exception as exc:
        raise RuntimeError("No se pudo validar de forma segura la base de desarrollo.") from exc
    if database_url.get_backend_name() != "sqlite":
        raise RuntimeError("El reinicio solo está permitido sobre SQLite local aislado.")
    if database_url.database not in {None, "", ":memory:"}:
        database_path = Path(database_url.database)
        if not database_path.is_absolute():
            database_path = BASE_DIR / database_path
        try:
            database_path.resolve().relative_to(BASE_DIR.resolve())
        except ValueError as exc:
            raise RuntimeError("La base SQLite debe estar dentro del proyecto local.") from exc


def _development_seed_password():
    password = os.getenv("J2M_DEMO_SEED_PASSWORD", "")
    if len(password) < 12:
        raise RuntimeError("Configure J2M_DEMO_SEED_PASSWORD con al menos 12 caracteres para el seed local.")
    return password


def reset_and_seed_database(*, confirmed=False):
    """Drops all tables, recreates them with current schema, and inserts initial seed data."""
    _assert_safe_seed_target(confirmed=confirmed)
    demo_password = _development_seed_password()
    with app.app_context():
        print("Eliminando tablas antiguas si existen...")
        db.drop_all()

        print("Creando nuevas tablas con el esquema actualizado...")
        db.create_all()

        print("Creando categorías de negocio...")
        cat_rest = BusinessCategory(name="Restaurante", slug="restaurante")
        cat_bar = BusinessCategory(name="Bar", slug="bar")
        cat_cafe = BusinessCategory(name="Cafetería", slug="cafeteria")
        cat_restbar = BusinessCategory(name="Restaurant-Bar", slug="restaurant-bar")
        cat_boutique = BusinessCategory(name="Boutique / Moda", slug="boutique-moda")

        db.session.add_all([cat_rest, cat_bar, cat_cafe, cat_restbar, cat_boutique])
        db.session.commit()

        print("Insertando datos de prueba...")

        # 1. Negocio 1: Programa de Sellos
        cafe = Business(
            name="Café Central",
            slug="cafe-central",
            category_id=cat_cafe.id,
            logo_url="https://images.unsplash.com/photo-1501339847302-ac426a4a7cbb?w=200&auto=format&fit=crop&q=80",
            primary_color="#78350F",
            secondary_color="#FEF3C7",
            loyalty_type="stamps",
            stamps_reward_limit=8,
            latitude=19.4326,
            longitude=-99.1332,
            is_active=True,
        )

        # 2. Negocio 2: Programa de Puntos
        boutique = Business(
            name="Moda Elegance",
            slug="moda-elegance",
            category_id=cat_boutique.id,
            logo_url="https://images.unsplash.com/photo-1441986300917-64674bd600d8?w=200&auto=format&fit=crop&q=80",
            primary_color="#4F46E5",
            secondary_color="#EEF2FF",
            loyalty_type="points",
            points_per_currency=2.0,
            latitude=19.4200,
            longitude=-99.1600,
            is_active=True,
        )

        db.session.add_all([cafe, boutique])
        db.session.commit()

        # 3. Usuarios de Staff (Admin, Manager, Cashier)
        admin_user = Staff(
            business_id=cafe.id,
            name="Administrador Café",
            username="admin_cafe",
            email="admin@cafecentral.mx",
            phone="+525511223344",
            role="admin",
            is_active=True,
        )
        admin_user.set_password(demo_password)

        cashier_cafe = Staff(
            business_id=cafe.id,
            name="Cajero Roberto",
            username="cajero1",
            email="roberto@cafecentral.mx",
            phone="+525522334455",
            role="cashier",
            is_active=True,
        )
        cashier_cafe.set_password(demo_password)

        manager_boutique = Staff(
            business_id=boutique.id,
            name="Gerente Sofía",
            username="sofia_manager",
            email="sofia@modaelegance.com",
            phone="+525533445566",
            role="manager",
            is_active=True,
        )
        manager_boutique.set_password(demo_password)

        db.session.add_all([admin_user, cashier_cafe, manager_boutique])
        db.session.commit()

        # 4. Clientes de prueba con fecha de nacimiento (birthdate)
        customer1 = Customer(
            business_id=cafe.id,
            full_name="Carlos Mendoza",
            email="carlos.mendoza@ejemplo.com",
            phone="+525598765432",
            birthdate=date(1995, 4, 18),
            current_stamps=6,
            current_points=0.0,
        )

        customer2 = Customer(
            business_id=cafe.id,
            full_name="Valeria Gómez",
            email="valeria.gomez@ejemplo.com",
            phone="+525587654321",
            birthdate=date(2001, 10, 15),
            current_stamps=8,
            current_points=0.0,
        )

        customer3 = Customer(
            business_id=boutique.id,
            full_name="Ana Sofía Morales",
            email="ana.morales@ejemplo.com",
            phone="+525576543210",
            birthdate=date(1990, 8, 24),
            current_stamps=0,
            current_points=350.0,
        )

        customer4 = Customer(
            business_id=boutique.id,
            full_name="Diego Rivera",
            email="diego.rivera@ejemplo.com",
            phone="+525565432109",
            birthdate=date(1982, 12, 8),
            current_stamps=0,
            current_points=1250.0,
        )

        db.session.add_all([customer1, customer2, customer3, customer4])
        db.session.commit()

        # 5. Transacciones iniciales de historial
        tx1 = Transaction(
            business_id=cafe.id,
            customer_id=customer1.id,
            staff_id=cashier_cafe.id,
            type="earn",
            stamps_amount=1,
            purchase_amount=85.00,
        )
        tx2 = Transaction(
            business_id=boutique.id,
            customer_id=customer3.id,
            staff_id=manager_boutique.id,
            type="earn",
            points_amount=100.0,
            purchase_amount=50.00,
        )
        tx3 = Transaction(
            business_id=boutique.id,
            customer_id=customer4.id,
            staff_id=manager_boutique.id,
            type="earn",
            points_amount=500.0,
            purchase_amount=250.00,
        )

        db.session.add_all([tx1, tx2, tx3])
        db.session.commit()

        print("¡Base de datos regenerada y poblada exitosamente!")
        print(f"- Negocios creados: {Business.query.count()}")
        print(f"- Clientes creados: {Customer.query.count()}")
        print(f"- Usuarios de Staff: {Staff.query.count()}")
        print(f"- Transacciones registradas: {Transaction.query.count()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reinicia únicamente una base SQLite local de desarrollo.")
    parser.add_argument("--yes-i-understand-data-will-be-deleted", action="store_true")
    arguments = parser.parse_args()
    reset_and_seed_database(confirmed=arguments.yes_i_understand_data_will_be_deleted)
