import datetime
from app.extensions import db
from app.models import Business, Customer

class BirthdayService:
    """Service to handle automatic birthday campaigns.

    It finds customers whose ``birthdate`` matches the current day (in UTC) and
    triggers a simulated notification (e.g., email or push) using the business
    configuration.
    """

    @staticmethod
    def get_todays_birthdays():
        """Return a list of (business, customer) tuples whose birthday is today.

        The comparison ignores the year component – only month and day are
        considered. All dates are interpreted as UTC.
        """
        today = datetime.datetime.utcnow().date()
        # Filter customers with a non‑null birthdate that matches month/day
        customers = (
            Customer.query.filter(Customer.birthdate.isnot(None))
            .filter(db.extract('month', Customer.birthdate) == today.month)
            .filter(db.extract('day', Customer.birthdate) == today.day)
            .all()
        )
        # Group by business for potential per‑business customization
        result = []
        for cust in customers:
            if cust.business:
                result.append((cust.business, cust))
        return result

    @staticmethod
    def send_greeting(business: Business, customer: Customer):
        """Simulate sending a birthday greeting.

        In a real deployment this would integrate with an email provider or a
        push‑notification service. For the purpose of this project we simply log
        the action so that unit‑tests can assert that the method was called.
        """
        # Example payload – could be extended with business‑specific templates
        message = (
            f"Feliz cumpleaños, {customer.name or 'Cliente'}! 🎉\n"
            f"De parte de {business.name} hemos preparado una oferta especial "
            "para ti. ¡Disfruta de tu día!"
        )
        # Here we just print; a logger could be used instead.
        print(message)
        return True

    @classmethod
    def run_daily_campaign(cls):
        """Entry point used by the CLI command.

        Returns the number of greetings sent.
        """
        sent = 0
        for business, customer in cls.get_todays_birthdays():
            if cls.send_greeting(business, customer):
                sent += 1
        return sent
