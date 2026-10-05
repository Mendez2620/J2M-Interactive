import logging
from app.services import GoogleWalletService, AppleWalletService

logger = logging.getLogger(__name__)


class RewardNotificationService:
    """Thin wrapper to notify both Google and Apple wallets about a ready reward.

    The service is deliberately simple: it attempts to update the customer's
    loyalty object in Google Wallet and sends a silent push notification to
    Apple Wallet devices. Any exception is caught and logged – the caller does
    not need to handle failures because reward delivery is best‑effort.
    """

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
            # Update Google Wallet loyalty object – this method returns a dict
            # with a ``status`` key. In mock mode it always succeeds.
            google_result = GoogleWalletService().update_loyalty_object(customer)
            logger.info("Google Wallet reward sync result: %s", google_result)
            notified = True
        except Exception as e:
            logger.exception("Failed to sync reward to Google Wallet: %s", e)

        try:
            # Apple push – we reuse the QR code token as the authentication token.
            # The service method sends a silent APNs notification.
            AppleWalletService().send_push_notification(customer.qr_code_token)
            logger.info("Apple Wallet push notification sent for customer %s", customer.id)
            notified = True
        except Exception as e:
            logger.exception("Failed to send Apple Wallet push: %s", e)

        return notified
