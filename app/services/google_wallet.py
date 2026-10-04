import os
import time
import logging
import jwt
import requests
from typing import Dict, Any, Tuple, Optional
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger(__name__)


class GoogleWalletService:
    """Service to create and update Google Wallet Loyalty Classes, Objects, signed JWT save URLs, and REST API patches."""

    SAVE_URL_PREFIX = "https://pay.google.com/gp/v/save/"
    API_BASE_URL = "https://walletobjects.googleapis.com/walletobjects/v1"

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
        """Defines the Google Wallet LoyaltyClass dictionary structure with geofencing support."""
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

        # Geofencing / Location support for Google Wallet notifications
        if business.latitude is not None and business.longitude is not None:
            loyalty_class["locations"] = [
                {
                    "kind": "walletobjects#latLongPoint",
                    "latitude": float(business.latitude),
                    "longitude": float(business.longitude),
                }
            ]

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

        # Geofencing on object level
        if business.latitude is not None and business.longitude is not None:
            loyalty_object["locations"] = [
                {
                    "kind": "walletobjects#latLongPoint",
                    "latitude": float(business.latitude),
                    "longitude": float(business.longitude),
                }
            ]

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

    def _get_oauth2_access_token(self) -> Optional[str]:
        """
        Generates an OAuth2 access token for Google Wallet REST API calls using Google Service Account assertion.
        """
        if self.is_mock_key:
            return None

        now = int(time.time())
        claims = {
            "iss": self.sa_email,
            "scope": "https://www.googleapis.com/auth/wallet_object.issuer",
            "aud": "https://oauth2.googleapis.com/token",
            "exp": now + 3600,
            "iat": now,
        }

        assertion = jwt.encode(claims, self.sa_private_key, algorithm="RS256")
        token_response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=10,
        )

        if token_response.status_code == 200:
            return token_response.json().get("access_token")
        
        logger.error("Failed to acquire Google OAuth2 access token: %s", token_response.text)
        return None

    def update_loyalty_object(self, customer) -> Dict[str, Any]:
        """
        Updates the customer's LoyaltyObject in Google Wallet via REST API PATCH.
        In development / mock mode, logs the patch and returns simulated success.
        """
        object_id = self.get_object_id(customer)
        business = customer.business

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

        patch_body = {
            "loyaltyPoints": loyalty_points,
        }

        # In dev/mock mode
        if self.is_mock_key:
            logger.info("Dev Mode: Simulated Google Wallet REST PATCH for object %s: %s", object_id, patch_body)
            return {
                "status": "mock_success",
                "object_id": object_id,
                "patch_body": patch_body,
                "message": "Actualización de Google Wallet simulada en desarrollo.",
            }

        access_token = self._get_oauth2_access_token()
        if not access_token:
            return {
                "status": "error",
                "message": "No se pudo obtener el token OAuth2 de Google.",
            }

        url = f"{self.API_BASE_URL}/loyaltyObject/{object_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.patch(url, headers=headers, json=patch_body, timeout=10)
            if resp.status_code in (200, 204):
                return {
                    "status": "success",
                    "object_id": object_id,
                    "data": resp.json() if resp.content else {},
                }
            return {
                "status": "api_error",
                "status_code": resp.status_code,
                "error": resp.text,
            }
        except Exception as e:
            logger.exception("Error calling Google Wallet PATCH API: %s", e)
            return {"status": "exception", "error": str(e)}
