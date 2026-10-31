from app.extensions import db
from app.models.business_category import BusinessCategory
from app.models.business import Business
from app.models.customer import Customer
from app.models.staff import Staff
from app.models.transaction import Transaction
from app.models.apple_device import AppleDevice
from app.models.platform_admin import PlatformAdmin
from app.models.staff_session import StaffSession
from app.models.access_audit import AccessAudit
from app.models.customer_recovery import (
    CustomerRecoveryAuthorization, CustomerRecoveryChallenge,
    CustomerRecoveryRateLimit,
)
from app.models.loyalty_ledger import LoyaltyLedgerEntry
from app.models.auth_rate_limit import AuthRateLimitSlot
from app.models.google_wallet_sync_job import GoogleWalletSyncJob
from app.models.customer_registration import (
    CustomerRegistrationChallenge, CustomerRegistrationIdentityClaim,
)
from app.models.registration_email_job import RegistrationEmailJob
from app.models.promoter import (
    Promoter, PromoterSession, ReferralAttribution, ReferralEligibility,
    ReferralEligibilityDecision,
)
from app.models.referral_incentive import (
    ReferralIncentiveRecord, ReferralIncentiveRule, ReferralIncentiveRuleAudit,
)
from app.models.referral_participant import (
    CustomerReferralActivation, ReferralParticipant, ReferralParticipantAudit,
)
from app.models.referral_program import ReferralProgramAudit
from app.models.customer_auth import (
    CustomerAuthAudit, CustomerAuthChallenge, CustomerCredential,
    CustomerLinkingEnrollment, CustomerSession,
)

__all__ = [
    "db",
    "BusinessCategory",
    "Business",
    "Customer",
    "Staff",
    "Transaction",
    "AppleDevice",
    "PlatformAdmin",
    "StaffSession",
    "AccessAudit",
    "CustomerRecoveryAuthorization",
    "CustomerRecoveryChallenge",
    "CustomerRecoveryRateLimit",
    "LoyaltyLedgerEntry",
    "AuthRateLimitSlot",
    "GoogleWalletSyncJob",
    "CustomerRegistrationChallenge",
    "CustomerRegistrationIdentityClaim",
    "RegistrationEmailJob",
    "Promoter", "PromoterSession", "ReferralAttribution", "ReferralEligibility",
    "ReferralEligibilityDecision",
    "ReferralIncentiveRule", "ReferralIncentiveRuleAudit", "ReferralIncentiveRecord",
    "ReferralParticipant", "ReferralParticipantAudit", "CustomerReferralActivation",
    "ReferralProgramAudit",
    "CustomerAuthAudit", "CustomerAuthChallenge", "CustomerCredential",
    "CustomerLinkingEnrollment", "CustomerSession",
]
