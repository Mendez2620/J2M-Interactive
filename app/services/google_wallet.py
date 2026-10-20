import json
import logging
import os
import re
import time
from typing import Optional, Tuple
from urllib.parse import quote, urlparse

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import current_app, has_app_context
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

logger = logging.getLogger(__name__)


class GoogleWalletConfigurationError(RuntimeError):
    pass


class GoogleWalletAPIError(RuntimeError):
    def __init__(self, status_code=None, retryable=False):
        super().__init__("Google Wallet API no disponible")
        self.status_code = status_code
        self.retryable = retryable


class GoogleWalletService:
    """Google Wallet Loyalty integration with explicit disabled/mock/real modes."""

    SAVE_URL_PREFIX = "https://pay.google.com/gp/v/save/"
    API_BASE_URL = "https://walletobjects.googleapis.com/walletobjects/v1"
    OAUTH_SCOPE = "https://www.googleapis.com/auth/wallet_object.issuer"
    VALID_MODES = {"disabled", "mock", "real"}

    def __init__(self, *, mode=None, credentials_info=None, http_session=None):
        self.mode = str(mode or self._config("GOOGLE_WALLET_MODE", "mock")).strip().lower()
        if self.mode not in self.VALID_MODES:
            raise GoogleWalletConfigurationError("GOOGLE_WALLET_MODE inválido.")
        self.issuer_id = str(self._config("GOOGLE_ISSUER_ID", "") or "").strip()
        self.sa_email = None
        self.sa_private_key = None
        self.credentials = None
        self.http = http_session
        self.is_mock_key = self.mode == "mock"
        if self.mode == "disabled":
            return
        if not re.fullmatch(r"\d{6,20}", self.issuer_id):
            raise GoogleWalletConfigurationError("GOOGLE_ISSUER_ID ausente o inválido.")
        if self.mode == "mock":
            self.sa_email = "mock-wallet@example.invalid"
            self.sa_private_key = self._generate_dev_private_key()
            return
        info = credentials_info or self._credentials_from_environment()
        self._validate_credentials_info(info)
        try:
            self.credentials = service_account.Credentials.from_service_account_info(
                info, scopes=[self.OAUTH_SCOPE]
            )
            serialization.load_pem_private_key(info["private_key"].encode(), password=None)
        except Exception:
            raise GoogleWalletConfigurationError("Credencial de Google Wallet inválida.") from None
        self.sa_email = info["client_email"]
        self.sa_private_key = info["private_key"]
        if self.http is None:
            self.http = AuthorizedSession(self.credentials)

    @staticmethod
    def _config(name, default=None):
        environment_value = os.getenv(name)
        if environment_value is not None:
            return environment_value
        return current_app.config.get(name, default) if has_app_context() else default

    @staticmethod
    def _generate_dev_private_key():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()

    @classmethod
    def _credentials_from_environment(cls):
        raw = cls._config("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not raw:
            raise GoogleWalletConfigurationError("Faltan credenciales de Google Wallet.")
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise GoogleWalletConfigurationError("Credencial de Google Wallet inválida.") from None

    @staticmethod
    def _validate_credentials_info(info):
        required = ("type", "project_id", "client_email", "private_key", "token_uri")
        if not isinstance(info, dict) or info.get("type") != "service_account":
            raise GoogleWalletConfigurationError("Se requiere una credencial service_account.")
        if any(not isinstance(info.get(field), str) or not info[field].strip() for field in required):
            raise GoogleWalletConfigurationError("Credencial de Google Wallet incompleta.")
        if not info["client_email"].endswith(".iam.gserviceaccount.com"):
            raise GoogleWalletConfigurationError("Correo de cuenta de servicio inválido.")

    def _require_enabled(self):
        if self.mode == "disabled":
            raise GoogleWalletConfigurationError("Google Wallet está deshabilitado.")

    def get_class_id(self, business):
        return f"{self.issuer_id}.business_{business.id}"

    def get_object_id(self, customer):
        return f"{self.issuer_id}.business_{customer.business_id}_customer_{customer.id}"

    def _logo_uri(self, business):
        uri = business.logo_url
        if not uri and business.logo_path and has_app_context():
            base = current_app.config.get("PUBLIC_BASE_URL", "")
            if base.startswith("https://"):
                uri = f"{base.rstrip('/')}/static/{business.logo_path.lstrip('/')}"
        parsed = urlparse(uri or "")
        return uri if parsed.scheme == "https" and parsed.netloc else None

    def create_or_update_class(self, business):
        self._require_enabled()
        result = {
            "id": self.get_class_id(business), "issuerName": business.name[:20],
            "programName": f"Club {business.name}"[:20], "reviewStatus": "UNDER_REVIEW",
            "hexBackgroundColor": business.primary_color or "#1E3A8A",
            "accountNameLabel": "Miembro", "accountIdLabel": "ID",
        }
        logo_uri = self._logo_uri(business)
        if logo_uri:
            result["programLogo"] = {"sourceUri": {"uri": logo_uri}, "contentDescription": {
                "defaultValue": {"language": "es-419", "value": f"Logo {business.name}"}}}
        return result

    def _loyalty_points(self, customer):
        if customer.business.loyalty_type == "stamps":
            return {"label": "Sellos", "balance": {"string":
                    f"{customer.current_stamps} / {customer.business.stamps_reward_limit}"}}
        return {"label": "Puntos", "balance": {"string": f"{customer.current_points:.0f} pts"}}

    def create_loyalty_object(self, customer):
        self._require_enabled()
        business = customer.business
        result = {
            "id": self.get_object_id(customer), "classId": self.get_class_id(business), "state": "ACTIVE",
            "accountId": f"member-{customer.id}", "accountName": "Miembro",
            "barcode": {"type": "QR_CODE", "value": customer.qr_code_token},
            "loyaltyPoints": self._loyalty_points(customer),
            "textModulesData": [{"id": "program", "header": "Programa", "body":
                "Sellos de recompensa" if business.loyalty_type == "stamps" else "Puntos acumulables"}],
        }
        if business.wallet_terms:
            result["textModulesData"].append({"id": "terms", "header": "Términos", "body": business.wallet_terms})
        return result

    def _request(self, method, resource, *, body=None):
        try:
            response = self.http.request(method, f"{self.API_BASE_URL}/{resource}", json=body, timeout=10)
        except RefreshError:
            logger.warning("google_wallet_api_error event=authentication_failure")
            raise GoogleWalletAPIError(status_code=401, retryable=False) from None
        except (TransportError, requests.RequestException):
            logger.warning("google_wallet_api_error event=network_failure")
            raise GoogleWalletAPIError(retryable=True) from None
        if response.status_code in (200, 201, 204):
            return response.json() if getattr(response, "content", b"") else {}
        retryable = response.status_code in {408, 429, 500, 502, 503, 504}
        logger.warning("google_wallet_api_error status_code=%s retryable=%s", response.status_code, retryable)
        raise GoogleWalletAPIError(response.status_code, retryable)

    def _upsert(self, kind, resource_id, body):
        resource = f"{kind}/{quote(resource_id, safe='')}"
        try:
            self._request("GET", resource)
        except GoogleWalletAPIError as exc:
            if exc.status_code != 404:
                raise
            try:
                return self._request("POST", kind, body=body)
            except GoogleWalletAPIError as create_error:
                if create_error.status_code != 409:
                    raise
        return self._request("PATCH", resource, body=body)

    def ensure_resources(self, customer):
        if self.mode != "real":
            return
        loyalty_class = self.create_or_update_class(customer.business)
        if "programLogo" not in loyalty_class:
            raise GoogleWalletConfigurationError("Google Wallet real requiere un logotipo HTTPS público.")
        self._upsert("loyaltyClass", loyalty_class["id"], loyalty_class)
        loyalty_object = self.create_loyalty_object(customer)
        self._upsert("loyaltyObject", loyalty_object["id"], loyalty_object)

    def create_jwt_pass(self, customer, origins=None) -> Tuple[str, str]:
        self._require_enabled()
        if origins is None:
            base = self._config("PUBLIC_BASE_URL", "")
            if self.mode == "real" and (not base or not base.startswith("https://")):
                raise GoogleWalletConfigurationError("PUBLIC_BASE_URL HTTPS es obligatoria para Google Wallet real.")
            origins = [base] if base else ["http://localhost:5000"]
        if not isinstance(origins, list) or not origins:
            raise GoogleWalletConfigurationError("origins de Google Wallet inválido.")
        if self.mode == "real":
            for origin in origins:
                parsed = urlparse(origin) if isinstance(origin, str) else None
                if (not parsed or parsed.scheme != "https" or not parsed.netloc or
                        parsed.username or parsed.password or parsed.path not in ("", "/") or
                        parsed.params or parsed.query or parsed.fragment):
                    raise GoogleWalletConfigurationError("origins de Google Wallet inválido.")
        if self.mode == "real":
            self.ensure_resources(customer)
        payload_data = ({"loyaltyObjects": [{
            "id": self.get_object_id(customer),
            "classId": self.get_class_id(customer.business),
        }]} if self.mode == "real" else {
            "loyaltyClasses": [self.create_or_update_class(customer.business)],
            "loyaltyObjects": [self.create_loyalty_object(customer)]})
        claims = {"iss": self.sa_email, "aud": "google", "typ": "savetowallet", "iat": int(time.time()),
                  "origins": origins, "payload": payload_data}
        token = jwt.encode(claims, self.sa_private_key, algorithm="RS256")
        return token, f"{self.SAVE_URL_PREFIX}{token}"

    def update_loyalty_object(self, customer):
        if self.mode == "disabled":
            return {"status": "disabled"}
        patch_body = {"loyaltyPoints": self._loyalty_points(customer)}
        if self.mode == "mock":
            return {"status": "mock", "object_id": self.get_object_id(customer), "patch_body": patch_body}
        try:
            object_id = self.get_object_id(customer)
            result = self._request("PATCH", f"loyaltyObject/{quote(object_id, safe='')}", body=patch_body)
            return {"status": "success", "object_id": object_id, "data": result}
        except GoogleWalletAPIError as exc:
            if exc.status_code == 404:
                try:
                    self.ensure_resources(customer)
                    return {"status": "success", "object_id": self.get_object_id(customer), "data": {}}
                except GoogleWalletAPIError as recovery_error:
                    exc = recovery_error
                except GoogleWalletConfigurationError:
                    return {"status": "error", "status_code": None}
            return {"status": "retry" if exc.retryable else "error", "status_code": exc.status_code}
