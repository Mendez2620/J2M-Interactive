import os
import time
import logging
import jwt
from typing import Dict, Any, Tuple, Optional
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger(__name__)


class GoogleWalletService:
    """Service to create Google Wallet Loyalty Classes, Objects, and signed JWT save URLs."""

    SAVE_URL_PREFIX = "https://pay.google.com/gp/v/save/"

    def __init__(
        self,
        issuer_id: Optional[str] = None,
        sa_email: Optional[str] = None,
        sa_private_key: Optional[str] = None,
    ):
        self.issuer_id = (
            issuer_id
            or os.getenv("GOOGLE_ISSUER_ID")
            or "3388000000000000000"
        )
        self.sa_email = (
            sa_email
            or os.getenv("GOOGLE_SA_EMAIL")
            or "service-account@j2m-loyalty.iam.gserviceaccount.com"
        )
        
        raw_key = (
            sa_private_key
            or os.getenv("GOOGLE_SA_PRIVATE_KEY")
        )

        self.is_mock_key = False
        if raw_key and "-----BEGIN" in raw_key:
            # Handle escaped newlines from environment variables
            self.sa_private_key = raw_key.replace("\\n", "\n")
        else:
            # Generate a transient RSA private key for local development / testing fallback
            logger.info("No valid GOOGLE_SA_PRIVATE_KEY provided. Generating transient development RSA key.")
            self.sa_private_key = self._generate_dev_private_key()
            self.is_mock_key = True

    @staticmethod
    def _generate_dev_private_key() -> str:
        """Generates a transient 2048-bit RSA private key in PEM format for dev/testing."""
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return pem.decode("utf-8")

    def get_class_id(self, business) -> str:
        """Generates a unique Class ID for the given business."""
        sanitized_slug = business.slug.replace("-", "_")
        return f"{self.issuer_id}.business_{business.id}_{sanitized_slug}"

    def get_object_id(self, customer) -> str:
        """Generates a unique Object ID for the given customer."""
        token_suffix = customer.qr_code_token[:8] if customer.qr_code_token else "00000000"
        return f"{self.issuer_id}.customer_{customer.id}_{token_suffix}"

    def create_or_update_class(self, business) -> Dict[str, Any]:
        """Defines the Google Wallet LoyaltyClass dictionary structure."""
        class_id = self.get_class_id(business)

        loyalty_class = {
            "id": class_id,
            "issuerName": business.name,
            "programName": f"Club {business.name}",
            "reviewStatus": "UNDER_REVIEW",
            "hexBackgroundColor": business.primary_color or "#1E3A8A",
        }

        # Optional logo URI if available
        if business.logo_url:
            loyalty_class["programLogo"] = {
                "sourceUri": {
                    "uri": business.logo_url
                },
                "contentDescription": {
                    "defaultValue": {
                        "language": "es-419",
                        "value": f"Logo {business.name}",
                    }
                },
            }

        return loyalty_class

    def create_loyalty_object(self, customer) -> Dict[str, Any]:
        """Defines the Google Wallet LoyaltyObject dictionary structure for a customer."""
        business = customer.business
        object_id = self.get_object_id(customer)
        class_id = self.get_class_id(business)

        # Loyalty balance representation based on business loyalty type
        if business.loyalty_type == "stamps":
            loyalty_points = {
                "label": "Sellos",
                "balance": {
                    "string": f"{customer.current_stamps} / {business.stamps_reward_limit}"
                },
            }
        else:
            loyalty_points = {
                "label": "Puntos",
                "balance": {
                    "string": f"{customer.current_points:.0f} pts"
                },
            }

        loyalty_object = {
            "id": object_id,
            "classId": class_id,
            "state": "ACTIVE",
            "accountId": str(customer.id),
            "accountName": customer.full_name,
            "barcode": {
                "type": "QR_CODE",
                "value": customer.qr_code_token,
                "alternateText": customer.qr_code_token,
            },
            "loyaltyPoints": loyalty_points,
            "textModulesData": [
                {
                    "header": "Cliente",
                    "body": customer.full_name,
                },
                {
                    "header": "Programa",
                    "body": "Sellos de Recompensa" if business.loyalty_type == "stamps" else "Puntos Acumulables",
                },
            ],
        }

        if customer.email:
            loyalty_object["textModulesData"].append({
                "header": "Email",
                "body": customer.email,
            })

        if customer.phone:
            loyalty_object["textModulesData"].append({
                "header": "Teléfono",
                "body": customer.phone,
            })

        return loyalty_object

    def create_jwt_pass(self, customer, origins=None) -> Tuple[str, str]:
        """
        Generates a signed JWT with RS256 algorithm and builds the Google Wallet Save URL.
        
        Returns:
            Tuple[jwt_token, save_url]
        """
        business = customer.business
        loyalty_class = self.create_or_update_class(business)
        loyalty_object = self.create_loyalty_object(customer)

        if origins is None:
            origins = ["http://localhost:5000", "https://pay.google.com"]

        # JWT payload according to Google Wallet API specifications
        payload = {
            "iss": self.sa_email,
            "aud": "google",
            "typ": "savetowallet",
            "iat": int(time.time()),
            "origins": origins,
            "payload": {
                "loyaltyClasses": [loyalty_class],
                "loyaltyObjects": [loyalty_object],
            },
        }

        # Sign JWT with RS256
        token = jwt.encode(payload, self.sa_private_key, algorithm="RS256")
        save_url = f"{self.SAVE_URL_PREFIX}{token}"

        return token, save_url
