import logging
from app.services import AppleWalletService

logger = logging.getLogger(__name__)


class RewardNotificationService:
    """Notify Apple devices; Google balance updates use the persistent outbox."""

    def send_reward_ready(self, customer, business) -> bool:
        """Notify the customer that a reward is ready.

        Args:
            customer: ``Customer`` model instance.
            business: ``Business`` model instance (used for any business‑specific
                configuration, e.g., push payload).
        Returns:
            bool: ``True`` if at least one notification attempt succeeded.
        """
        notified = False
        try:
            # Apple push – we reuse the QR code token as the authentication token.
            # The service method sends a silent APNs notification.
            AppleWalletService().send_push_notification(customer.qr_code_token)
            logger.info("Apple Wallet push notification sent for customer %s", customer.id)
            notified = True
        except Exception as e:
            logger.exception("Failed to send Apple Wallet push: %s", e)

        return notified
