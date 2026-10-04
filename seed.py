import os
from datetime import date, datetime, timezone
from app import create_app
from app.extensions import db
from app.models import Business, Customer, Staff, Transaction, AppleDevice

app = create_app(os.getenv("FLASK_ENV", "development"))


def reset_and_seed_database():
    """Drops all tables, recreates them with current schema, and inserts initial seed data."""
    with app.app_context():
        print("Eliminando tablas antiguas si existen...")
        db.drop_all()

        print("Creando nuevas tablas con el esquema actualizado...")
        db.create_all()

        print("Insertando datos de prueba...")

        # 1. Negocio 1: Programa de Sellos
        cafe = Business(
            name="Café Central",
            slug="cafe-central",
            logo_url="https://images.unsplash.com/photo-1501339847302-ac426a4a7cbb?w=200&auto=format&fit=crop&q=80",
            primary_color="#78350F",
            secondary_color="#FEF3C7",
            loyalty_type="stamps",
            stamps_reward_limit=8,
            latitude=19.4326,
            longitude=-99.1332,
        )

        # 2. Negocio 2: Programa de Puntos
        boutique = Business(
            name="Moda Elegance",
            slug="moda-elegance",
            logo_url="https://images.unsplash.com/photo-1441986300917-64674bd600d8?w=200&auto=format&fit=crop&q=80",
            primary_color="#4F46E5",
            secondary_color="#EEF2FF",
            loyalty_type="points",
            points_per_currency=2.0,
            latitude=19.4200,
            longitude=-99.1600,
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
        admin_user.set_password("Admin1234!")

        cashier_cafe = Staff(
            business_id=cafe.id,
            name="Cajero Roberto",
            username="cajero1",
            email="roberto@cafecentral.mx",
            phone="+525522334455",
            role="cashier",
            is_active=True,
        )
        cashier_cafe.set_password("Cajero1234!")

        manager_boutique = Staff(
            business_id=boutique.id,
            name="Gerente Sofía",
            username="sofia_manager",
            email="sofia@modaelegance.com",
            phone="+525533445566",
            role="manager",
            is_active=True,
        )
        manager_boutique.set_password("Manager1234!")

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
    reset_and_seed_database()
