import copy
import json
import logging
import os
import re
import time
from contextlib import ExitStack, contextmanager
from functools import wraps
from typing import Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import current_app, has_app_context
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import inspect
from app.extensions import db
from app.services.google_wallet_write_reservation import WalletWriteBlocked, authorized_write, writing
from app.media_storage import absolute_branding_url
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


def _coordinate(*, include_class=False, class_only=False, raises=False):
    def decorate(method):
        @wraps(method)
        def guarded(self, subject, *args, **kwargs):
            if self.mode != "real":
                return method(self, subject, *args, **kwargs)
            try:
                with self.write_scope(subject, include_class=include_class, class_only=class_only):
                    return method(self, subject, *args, **kwargs)
            except (WalletWriteBlocked, SQLAlchemyError, ValueError):
                if raises:
                    raise GoogleWalletAPIError(retryable=True) from None
                return {"status": "retry", "reason": "write_reservation_unavailable"}
        return guarded
    return decorate


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
            self.http = AuthorizedSession(self.credentials, max_refresh_attempts=0)

    @contextmanager
    def write_scope(self, subject, *, include_class=False, class_only=False):
        business_id = subject.id if class_only else subject.business_id
        with ExitStack() as stack:
            if self.mode == "real":
                if include_class or class_only:
                    stack.enter_context(writing(
                        db.engine, f"loyaltyClass/{self.issuer_id}.business_{business_id}",
                        kind="loyaltyClass", issuer=self.issuer_id, business_id=business_id,
                    ))
                if not class_only:
                    stack.enter_context(writing(
                        db.engine, f"loyaltyObject/{self.get_object_id(subject)}",
                        kind="loyaltyObject", issuer=self.issuer_id,
                        business_id=business_id, customer_id=subject.id,
                    ))
            yield

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

    def _image_uri(self, business, *, cover=False):
        path = business.cover_path if cover else business.logo_path
        uri = None if cover else business.logo_url
        if not uri and path and has_app_context():
            base = current_app.config.get("PUBLIC_BASE_URL", "")
            if base.startswith("https://"):
                uri = absolute_branding_url(path, base)
        parsed = urlparse(uri or "")
        return uri if parsed.scheme == "https" and parsed.netloc else None

    def _logo_uri(self, business):
        return self._image_uri(business)

    def create_or_update_class(self, business):
        self._require_enabled()
        result = {
            "id": self.get_class_id(business), "issuerName": business.name[:20],
            "programName": (business.wallet_program_name or f"Club {business.name}")[:20], "reviewStatus": "UNDER_REVIEW",
            "hexBackgroundColor": business.primary_color or "#1E3A8A",
            "accountNameLabel": "Miembro", "accountIdLabel": "ID",
        }
        logo_uri = self._logo_uri(business)
        if logo_uri:
            result["programLogo"] = {"sourceUri": {"uri": logo_uri}, "contentDescription": {
                "defaultValue": {"language": "es-419", "value": f"Logo {business.name}"}}}
        hero_uri = self._image_uri(business, cover=True)
        if hero_uri:
            result["heroImage"] = {"sourceUri": {"uri": hero_uri}, "contentDescription": {
                "defaultValue": {"language": "es-419", "value": f"Imagen de {business.name}"}}}
        return result

    def _loyalty_points(self, customer):
        if customer.business.loyalty_type == "stamps":
            return {"label": customer.business.wallet_balance_label or "Sellos", "balance": {"string":
                    f"{customer.current_stamps} / {customer.business.stamps_reward_limit}"}}
        return {"label": customer.business.wallet_balance_label or "Puntos", "balance": {"string": f"{customer.current_points:.0f} pts"}}

    def _invitation_url(self, customer, *, existing_activation=None):
        from app.models import ReferralParticipant
        from app.services.referral_participant_service import resolve_customer_activation
        from app.services.platform_admin_session import now

        if not customer.business.is_active or not customer.business.referrals_enabled:
            return None
        base = str(self._config("PUBLIC_BASE_URL", "") or "").rstrip("/")
        parsed = urlparse(base)
        if parsed.scheme != "https" or not parsed.netloc:
            return None
        participant = ReferralParticipant.query.filter_by(
            business_id=customer.business_id,
            customer_id=customer.id,
            participant_type="customer_ambassador",
        ).populate_existing().first()
        if not participant or participant.revoked_at is not None:
            return None
        if participant.currently_eligible():
            return f"{base}/invite/{participant.invitation_code}"
        # Only the issuance flow can supply a private activation capability.
        # Recheck its authoritative record instead of trusting the cached URL.
        activation_url = existing_activation or getattr(customer, "_referral_wallet_invitation_url", None)
        if not self._is_owned_referral_uri(activation_url) or not urlparse(activation_url).path.startswith("/invite/activate/"):
            return None
        activation = resolve_customer_activation(urlparse(activation_url).path.rsplit("/", 1)[-1])
        if (activation is not None and activation.business_id == customer.business_id
                and activation.participant_id == participant.id and activation.consumed_at is None
                and activation.expires_at > now() and participant.consented_at is None):
            return activation_url
        return None

    def create_loyalty_object(self, customer):
        self._require_enabled()
        business = customer.business
        result = {
            "id": self.get_object_id(customer), "classId": self.get_class_id(business), "state": "ACTIVE",
            "accountId": f"member-{customer.id}", "accountName": "Miembro",
            "barcode": {"type": "QR_CODE", "value": customer.qr_code_token},
            "loyaltyPoints": self._loyalty_points(customer),
            "textModulesData": [{"id": "program", "header": "Programa", "body":
                business.wallet_card_text or ("Sellos de recompensa" if business.loyalty_type == "stamps" else "Puntos acumulables")}],
        }
        if business.reward_description:
            result["textModulesData"].append({"id": "reward", "header": "Recompensa", "body": business.reward_description})
        if business.wallet_instructions:
            result["textModulesData"].append({"id": "instructions", "header": "Cómo utilizarla", "body": business.wallet_instructions})
        if business.wallet_terms:
            result["textModulesData"].append({"id": "terms", "header": "Términos", "body": business.wallet_terms})
        if business.wallet_contact:
            result["textModulesData"].append({"id": "contact", "header": "Contacto", "body": business.wallet_contact})
        invitation_url = self._invitation_url(customer)
        if invitation_url:
            result["appLinkData"] = {
                "webAppLinkInfo": {"appTarget": {"targetUri": {
                    "uri": invitation_url,
                    "description": "Invitación personal del programa de referidos",
                }}},
                "displayText": {"defaultValue": {
                    "language": "es-419", "value": "Invitar amigos",
                }},
            }
            # Google documents both object-level surfaces. appLinkData is the
            # preferred action, while linksModuleData keeps the same personal
            # URL discoverable in pass details (including clients that do not
            # render the app link consistently). Never put this URL on Class.
            result["linksModuleData"] = {"uris": [{
                "id": "invite_friends",
                "uri": invitation_url,
                "description": "Invitar amigos",
            }]}
        return result

    def _request(self, method, resource, *, body=None):
        operation = None
        options = {}
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            kind, _, identifier = resource.partition("/")
            identifier = unquote(identifier) if identifier else (body or {}).get("id")
            if self.mode != "real" or kind not in {"loyaltyClass", "loyaltyObject"} or not identifier:
                raise WalletWriteBlocked("Invalid Wallet write resource")
            if body and "id" in body and body["id"] != identifier:
                raise WalletWriteBlocked("Wallet write payload identifies another resource")
            operation = authorized_write(db.engine, f"{kind}/{identifier}")
            options["allow_redirects"] = False
        try:
            response = self.http.request(method, f"{self.API_BASE_URL}/{resource}", json=body,
                                         timeout=10, **options)
        except RefreshError:
            logger.warning("google_wallet_api_error event=authentication_failure")
            raise GoogleWalletAPIError(status_code=401, retryable=False) from None
        except (TransportError, requests.RequestException):
            logger.warning("google_wallet_api_error event=network_failure")
            raise GoogleWalletAPIError(retryable=True) from None
        if response.status_code in (200, 201, 204):
            payload = response.json() if getattr(response, "content", b"") else {}
            if operation:
                if not isinstance(payload, dict) or (response.status_code != 204 and not payload):
                    raise GoogleWalletAPIError(retryable=True)
                operation.move("confirmed", response.status_code)
            return payload
        if operation and response.status_code in {400, 401, 403, 404, 409}:
            # A definitive response to an intermediate request permits the
            # next step, but the logical operation keeps its reservation.
            operation.move("confirmed", response.status_code)
        retryable = response.status_code in {408, 429, 500, 502, 503, 504}
        logger.warning("google_wallet_api_error status_code=%s retryable=%s", response.status_code, retryable)
        raise GoogleWalletAPIError(response.status_code, retryable)

    def _verified_mutation(self, method, kind, resource_id, body, *, class_id=None):
        resource = kind if method == "POST" else f"{kind}/{quote(resource_id, safe='')}"
        result = self._request(method, resource, body=body)
        if (result.get("id") != resource_id
                or (class_id is not None and result.get("classId") != class_id)
                or any(result.get(key) != value for key, value in body.items())):
            raise GoogleWalletAPIError(retryable=True)
        return result

    def _upsert(self, kind, resource_id, body):
        resource = f"{kind}/{quote(resource_id, safe='')}"
        try:
            self._request("GET", resource)
        except GoogleWalletAPIError as exc:
            if exc.status_code != 404:
                raise
            try:
                return self._verified_mutation("POST", kind, resource_id, body)
            except GoogleWalletAPIError as create_error:
                if create_error.status_code != 409:
                    raise
        return self._verified_mutation("PATCH", kind, resource_id, body)

    @_coordinate(raises=True)
    def _ensure_loyalty_object(self, customer):
        """Create an object once; refresh existing metadata without its balance."""
        body = self.create_loyalty_object(customer)
        resource = f"loyaltyObject/{quote(body['id'], safe='')}"
        metadata = {
            key: value for key, value in body.items()
            if key not in {"id", "classId", "loyaltyPoints"}
        }
        try:
            existing = self._request("GET", resource)
        except GoogleWalletAPIError as exc:
            if exc.status_code != 404:
                raise
            try:
                return self._verified_mutation("POST", "loyaltyObject", body["id"], body,
                                               class_id=body["classId"])
            except GoogleWalletAPIError as create_error:
                if create_error.status_code != 409:
                    raise
                # A concurrent creator won. Read its complete object before
                # replacing any array-valued metadata in the PATCH.
                existing = self._request("GET", resource)
        # Reissuing an existing pass follows the same controlled-module policy
        # as design publication, while still updating the issuance-owned QR.
        for key in ("textModulesData", "linksModuleData", "appLinkData"):
            metadata.pop(key, None)
        metadata.update(self._owned_metadata_patch(existing, body))
        return self._verified_mutation("PATCH", "loyaltyObject", body["id"], metadata,
                                       class_id=body["classId"])

    @staticmethod
    def _merge_items(existing, desired, controlled_ids):
        """Replace only J2M-owned IDs while preserving foreign items and order."""
        desired_by_id = {
            item.get("id"): copy.deepcopy(item) for item in (desired or [])
            if isinstance(item, dict) and item.get("id") in controlled_ids
        }
        merged = []
        emitted = set()
        for item in existing or []:
            item_id = item.get("id") if isinstance(item, dict) else None
            if item_id not in controlled_ids:
                merged.append(copy.deepcopy(item))
            elif item_id in desired_by_id and item_id not in emitted:
                updated = copy.deepcopy(item)
                updated.update(desired_by_id[item_id])
                merged.append(updated)
                emitted.add(item_id)
        for item in desired or []:
            item_id = item.get("id") if isinstance(item, dict) else None
            if item_id in controlled_ids and item_id not in emitted:
                merged.append(copy.deepcopy(item))
                emitted.add(item_id)
        return merged

    def _owned_metadata_patch(self, existing, desired):
        """Merge owned modules and remove obsolete owned links, preserving foreign data."""
        controlled_text = {"program", "reward", "instructions", "terms", "contact"}
        existing_text = existing.get("textModulesData") if isinstance(existing, dict) else []
        patch_body = {"textModulesData": self._merge_items(
            existing_text if isinstance(existing_text, list) else [],
            desired.get("textModulesData", []), controlled_text,
        )}
        patch_body.update(self._referral_links_patch(existing, desired))
        return patch_body

    def _referral_links_patch(self, existing, desired):
        patch_body = {}
        existing_links = existing.get("linksModuleData") if isinstance(existing, dict) else None
        desired_links = desired.get("linksModuleData")
        merged_links = copy.deepcopy(existing_links) if isinstance(existing_links, dict) else {}
        current_uris = merged_links.get("uris") if isinstance(merged_links.get("uris"), list) else []
        wanted_uris = desired_links.get("uris", []) if isinstance(desired_links, dict) else []
        merged_links["uris"] = self._merge_items(current_uris, wanted_uris, {"invite_friends"})
        if merged_links["uris"] or existing_links is not None:
            patch_body["linksModuleData"] = merged_links

        if "appLinkData" in desired:
            patch_body["appLinkData"] = desired["appLinkData"]
        else:
            current_app_link = existing.get("appLinkData") if isinstance(existing, dict) else None
            try:
                current_uri = current_app_link["webAppLinkInfo"]["appTarget"]["targetUri"]["uri"]
            except (KeyError, TypeError):
                current_uri = ""
            if self._is_owned_referral_uri(current_uri):
                patch_body["appLinkData"] = None
        return patch_body

    def _is_owned_referral_uri(self, uri):
        if not isinstance(uri, str):
            return False
        try:
            parsed = urlparse(uri)
        except ValueError:
            return False
        origins = [self._config("PUBLIC_BASE_URL", "") or ""]
        origins.extend(str(self._config("GOOGLE_WALLET_REFERRAL_HISTORICAL_ORIGINS", "") or "").split(","))
        return (parsed.scheme == "https" and not parsed.query and not parsed.fragment
                and f"https://{parsed.netloc}" in {str(origin).strip().rstrip("/") for origin in origins}
                and re.fullmatch(r"/invite/(?:activate/)?[A-Za-z0-9_-]{16,128}", parsed.path) is not None)

    @_coordinate()
    def publish_referral_links(self, customer):
        """Refresh only J2M referral surfaces. Never create a missing object."""
        if self.mode != "real":
            return {"status": "disabled"}
        db.session.refresh(customer)
        db.session.refresh(customer.business)
        object_id = self.get_object_id(customer)
        class_id = self.get_class_id(customer.business)
        resource = f"loyaltyObject/{quote(object_id, safe='')}"
        try:
            existing = self._request("GET", resource)
            if not isinstance(existing, dict) or existing.get("id") != object_id or existing.get("classId") != class_id:
                return {"status": "error", "reason": "remote_identity_mismatch"}
            body = self.create_loyalty_object(customer)
            try:
                current_uri = existing["appLinkData"]["webAppLinkInfo"]["appTarget"]["targetUri"]["uri"]
            except (KeyError, TypeError):
                current_uri = None
            if "appLinkData" not in body and current_uri and self._invitation_url(
                customer, existing_activation=current_uri,
            ) == current_uri:
                return {"status": "success"}
            patch_body = {
                key: value for key, value in self._referral_links_patch(existing, body).items()
                if existing.get(key) != value
            }
            if existing.get("appLinkData") and not self._is_owned_referral_uri(current_uri):
                patch_body.pop("appLinkData", None)
            if patch_body:
                self._verified_mutation("PATCH", "loyaltyObject", object_id, patch_body, class_id=class_id)
            return {"status": "success"}
        except GoogleWalletAPIError as exc:
            if exc.status_code == 404:
                return {"status": "skipped", "reason": "not_issued"}
            return {"status": "retry" if exc.retryable else "error", "status_code": exc.status_code}

    @_coordinate(class_only=True)
    def publish_loyalty_class(self, business, prepared=None):
        """Publish shared design without changing the stable class identity."""
        if self.mode == "disabled":
            return {"status": "error", "status_code": None}
        body = (prepared() if callable(prepared) else prepared) or self.create_or_update_class(business)
        if self.mode == "mock":
            return {"status": "mock", "class_id": body["id"], "patch_body": body}
        try:
            self._upsert("loyaltyClass", body["id"], body)
            return {"status": "success", "class_id": body["id"]}
        except GoogleWalletAPIError as exc:
            return {"status": "retry" if exc.retryable else "error", "status_code": exc.status_code}

    @_coordinate()
    def publish_loyalty_object_metadata(self, customer, prepared=None):
        """Publish J2M-owned metadata while preserving balance, barcode and foreign modules."""
        if self.mode == "disabled":
            return {"status": "error", "status_code": None}
        desired = (prepared() if callable(prepared) else prepared) or self.create_loyalty_object(customer)
        object_id = desired["id"]
        if self.mode == "mock":
            return {"status": "mock", "object_id": object_id}
        resource = f"loyaltyObject/{quote(object_id, safe='')}"
        try:
            existing = self._request("GET", resource)
        except GoogleWalletAPIError as exc:
            if exc.status_code == 404:
                return {"status": "skipped", "status_code": 404, "reason": "not_issued"}
            else:
                return {"status": "retry" if exc.retryable else "error", "status_code": exc.status_code}

        patch_body = self._owned_metadata_patch(existing, desired)
        try:
            self._verified_mutation("PATCH", "loyaltyObject", object_id, patch_body,
                                    class_id=desired["classId"])
            return {"status": "success", "object_id": object_id}
        except GoogleWalletAPIError as exc:
            return {"status": "retry" if exc.retryable else "error", "status_code": exc.status_code}

    @_coordinate(include_class=True, raises=True)
    def ensure_resources(self, customer):
        if self.mode != "real":
            return
        state = inspect(customer, raiseerr=False)
        if state is not None and state.persistent:
            db.session.refresh(customer)
            db.session.refresh(customer.business)
        if not customer.business.is_active:
            raise GoogleWalletConfigurationError("El negocio no está activo.")
        loyalty_class = self.create_or_update_class(customer.business)
        if "programLogo" not in loyalty_class:
            raise GoogleWalletConfigurationError("Google Wallet real requiere un logotipo HTTPS público.")
        self._upsert("loyaltyClass", loyalty_class["id"], loyalty_class)
        self._ensure_loyalty_object(customer)

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

    @_coordinate()
    def reconcile_existing_loyalty_balance(self, customer, *, business_id):
        """Reconcile one existing remote object; never issue, recreate or activate it.

        Caller must authorize the tenant, verify the local ledger and hold the
        per-customer synchronization fence while obtaining the current balance
        and performing this operation. This method does not mutate local data,
        claim a job, or authorize an operational batch.
        """
        if self.mode != "real":
            return {"status": "disabled", "reason": "real_mode_required"}
        business = customer.business
        if (type(business_id) is not int or business_id < 1
                or customer.business_id != business_id
                or business is None or business.id != business_id):
            return {"status": "error", "reason": "tenant_mismatch"}
        if not business.is_active:
            return {"status": "skipped", "reason": "business_inactive"}

        object_id = self.get_object_id(customer)
        class_id = self.get_class_id(business)
        resource = f"loyaltyObject/{quote(object_id, safe='')}"
        desired = self._loyalty_points(customer)

        def identity_matches(value):
            return (isinstance(value, dict) and value.get("id") == object_id
                    and value.get("classId") == class_id)

        def is_active(value):
            return str(value.get("state", "")).upper() == "ACTIVE"

        def balance_matches(value):
            points = value.get("loyaltyPoints")
            if not isinstance(points, dict):
                return False
            balance = points.get("balance")
            return (isinstance(balance, dict)
                    and points.get("label") == desired["label"]
                    and balance.get("string") == desired["balance"]["string"]
                    and all(balance.get(key) is None for key in ("int", "double", "money")))

        try:
            existing = self._request("GET", resource)
            if not identity_matches(existing):
                return {"status": "error", "reason": "remote_identity_mismatch"}
            if not is_active(existing):
                return {"status": "skipped", "reason": "object_not_active"}
            if balance_matches(existing):
                return {"status": "success", "reason": "already_current"}

            updated = self._request("PATCH", resource, body={"loyaltyPoints": desired})
            if not identity_matches(updated):
                return {"status": "error", "reason": "remote_identity_mismatch"}
            if not is_active(updated):
                return {"status": "skipped", "reason": "object_not_active"}
            if not balance_matches(updated):
                return {"status": "retry", "reason": "balance_unconfirmed"}
            return {"status": "success", "reason": "balance_updated"}
        except GoogleWalletAPIError as exc:
            if exc.status_code == 404:
                return {"status": "skipped", "reason": "object_not_found", "status_code": 404}
            return {"status": "retry" if exc.retryable else "error",
                    "status_code": exc.status_code}
        except ValueError:
            # A malformed response is not proof that a remote write succeeded.
            return {"status": "retry", "reason": "invalid_provider_response"}

    @_coordinate()
    def _update_existing_balance(self, customer):
        if self.mode == "disabled":
            return {"status": "disabled"}
        state = inspect(customer, raiseerr=False)
        if self.mode == "real" and state is not None and state.persistent:
            db.session.refresh(customer)
            db.session.refresh(customer.business)
        patch_body = {"loyaltyPoints": self._loyalty_points(customer)}
        if self.mode == "mock":
            return {"status": "mock", "object_id": self.get_object_id(customer), "patch_body": patch_body}
        try:
            object_id = self.get_object_id(customer)
            result = self._request("PATCH", f"loyaltyObject/{quote(object_id, safe='')}", body=patch_body)
            if (result.get("id") != object_id
                    or result.get("classId") != self.get_class_id(customer.business)
                    or result.get("loyaltyPoints") != patch_body["loyaltyPoints"]):
                return {"status": "retry", "reason": "balance_unconfirmed"}
            return {"status": "success", "object_id": object_id, "data": result}
        except GoogleWalletAPIError as exc:
            return {"status": "retry" if exc.retryable else "error", "status_code": exc.status_code}

    def update_loyalty_object(self, customer):
        result = self._update_existing_balance(customer)
        if result.get("status_code") != 404:
            return result
        # The definitive 404 closed the object-only scope. Recovery acquires
        # class then object, and reads the object again before attempting creation.
        try:
            self.ensure_resources(customer)
        except GoogleWalletAPIError as exc:
            return {"status": "retry" if exc.retryable else "error", "status_code": exc.status_code}
        except GoogleWalletConfigurationError:
            return {"status": "error", "status_code": None}
        return self._update_existing_balance(customer)
