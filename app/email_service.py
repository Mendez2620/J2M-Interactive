import smtplib
import ssl
from email.message import EmailMessage


class MailDeliveryError(RuntimeError):
    pass


class MemoryRecoveryMailer:
    """Testing-only transport. Its outbox is never exposed through an HTTP route."""

    def __init__(self):
        self.outbox = []

    def send_recovery_code(self, recipient, code, business_name):
        self.outbox.append({"to": recipient, "code": code, "business_name": business_name})

    def send_registration_code(self, recipient, code, business_name):
        self.outbox.append({"to": recipient, "code": code, "business_name": business_name, "purpose": "registration"})

class SMTPRecoveryMailer:
    def __init__(self, config):
        self.host = config.get("SMTP_HOST")
        self.port = config.get("SMTP_PORT", 587)
        self.username = config.get("SMTP_USERNAME")
        self.password = config.get("SMTP_PASSWORD")
        self.sender = config.get("SMTP_FROM")
        self.use_tls = config.get("SMTP_USE_TLS", True)

    def send_recovery_code(self, recipient, code, business_name):
        if not self.host or not self.sender:
            raise MailDeliveryError("SMTP no configurado")
        message = EmailMessage()
        message["Subject"] = f"Código para recuperar tu tarjeta de {business_name}"
        message["From"] = self.sender
        message["To"] = recipient
        message.set_content(
            f"Tu código de recuperación es {code}. Caduca en 10 minutos. "
            "Si no lo solicitaste, ignora este mensaje."
        )
        try:
            with smtplib.SMTP(self.host, self.port, timeout=10) as client:
                if self.use_tls:
                    client.starttls(context=ssl.create_default_context())
                if self.username:
                    client.login(self.username, self.password or "")
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise MailDeliveryError("No fue posible entregar el correo") from exc

    def send_registration_code(self, recipient, code, business_name):
        if not self.host or not self.sender:
            raise MailDeliveryError("SMTP no configurado")
        message = EmailMessage()
        message["Subject"] = f"Confirma tu registro en {business_name}"
        message["From"] = self.sender
        message["To"] = recipient
        message.set_content(
            f"Tu código de verificación es {code}. Caduca en 10 minutos. "
            "Si no solicitaste este registro, ignora este mensaje."
        )
        try:
            with smtplib.SMTP(self.host, self.port, timeout=10) as client:
                if self.use_tls:
                    client.starttls(context=ssl.create_default_context())
                if self.username:
                    client.login(self.username, self.password or "")
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise MailDeliveryError("No fue posible entregar el correo") from exc

def build_recovery_mailer(config):
    if config.get("RECOVERY_MAIL_BACKEND") == "memory":
        return MemoryRecoveryMailer()
    return SMTPRecoveryMailer(config)
