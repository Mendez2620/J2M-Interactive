import logging
from app.extensions import db
from app.services.apple_wallet_update_service import enqueue_apple_pass_update

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
            queued = enqueue_apple_pass_update(customer, reason="reward")
            if queued:
                db.session.commit()
                logger.info("Apple Wallet update queued for customer %s", customer.id)
                notified = True
        except Exception as e:
            logger.exception("Failed to send Apple Wallet push: %s", e)

        return notified
