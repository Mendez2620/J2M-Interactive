import io
import json
import os
import hashlib
import base64
import hmac
import ssl
import zipfile
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse
from pathlib import Path
from typing import Dict, Any, Optional, List
from flask import current_app, has_app_context
from PIL import Image, ImageOps, UnidentifiedImageError
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7
import httpx

from app.extensions import db

logger = logging.getLogger(__name__)

# Valid 1x1 transparent PNG bytes for placeholder asset generation
MINIMAL_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
)


def hex_to_rgb(hex_code: Optional[str], default: str = "rgb(30, 58, 138)") -> str:
    """Converts a hex color code (#RRGGBB or #RGB) into 'rgb(r, g, b)' format."""
    if not hex_code:
        return default
    cleaned = hex_code.strip().lstrip("#")
    if len(cleaned) == 6:
        try:
            r = int(cleaned[0:2], 16)
            g = int(cleaned[2:4], 16)
            b = int(cleaned[4:6], 16)
            return f"rgb({r}, {g}, {b})"
        except ValueError:
            return default
    elif len(cleaned) == 3:
        try:
            r = int(cleaned[0] * 2, 16)
            g = int(cleaned[1] * 2, 16)
            b = int(cleaned[2] * 2, 16)
            return f"rgb({r}, {g}, {b})"
        except ValueError:
            return default
    return default


class AppleWalletService:
    """Service to create Apple Wallet Pass JSON structures, geofencing, and bundled .pkpass files."""

    def __init__(
        self,
        pass_type_id: Optional[str] = None,
        team_id: Optional[str] = None,
        cert_path: Optional[str] = None,
        key_path: Optional[str] = None,
        key_password: Optional[str] = None,
        wwdr_path: Optional[str] = None,
        web_service_url: Optional[str] = None,
    ):
        self.pass_type_id = (
            pass_type_id
            or os.getenv("APPLE_PASS_TYPE_ID")
            or "pass.com.j2minteractive.loyalty"
        )
        self.team_id = (
            team_id
            or os.getenv("APPLE_TEAM_ID")
            or "J2M1234567"
        )
        self.cert_path = cert_path or os.getenv("APPLE_CERT_PATH", "certs/passcert.pem")
        self.key_path = key_path or os.getenv("APPLE_KEY_PATH", "certs/passkey.pem")
        self.key_password = key_password or os.getenv("APPLE_KEY_PASSWORD")
        self.wwdr_path = wwdr_path or os.getenv("APPLE_WWDR_PATH", "certs/wwdr.pem")
        self.web_service_url = (
            web_service_url
            or os.getenv("APPLE_WEB_SERVICE_URL")
            or "/api/apple/v1"
        )
        self.mode = (current_app.config.get("APPLE_WALLET_MODE", "mock") if has_app_context() else os.getenv("APPLE_WALLET_MODE", "mock")).lower()

    @staticmethod
    def _auth_keys():
        configured = current_app.config.get("APPLE_PASS_AUTH_KEYS", "") if has_app_context() else os.getenv("APPLE_PASS_AUTH_KEYS", "")
        keys = [item.strip().encode("utf-8") for item in str(configured).split(",") if item.strip()]
        if not keys and (not has_app_context() or current_app.testing):
            keys = [b"testing-apple-pass-auth-key-at-least-32-bytes"]
        if not keys or len(keys[0]) < 32:
            raise RuntimeError("Apple PassKit authentication is not configured securely.")
        return keys

    def _derive_auth_token(self, customer, key_version=1, serial_number=None):
        keys = self._auth_keys()
        if key_version < 1 or key_version > len(keys):
            raise RuntimeError("Apple PassKit authentication key version is unavailable.")
        return self._derive_auth_token_with_key(customer, keys[key_version - 1], serial_number)

    def _derive_auth_token_with_key(self, customer, key, serial_number=None):
        serial_number = serial_number or self.get_serial_number(customer)
        material = f"apple-pass:v1:{customer.business_id}:{customer.id}:{serial_number}".encode()
        digest = hmac.new(key, material, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def ensure_pass_credential(self, customer):
        from app.models import ApplePassCredential
        credential = ApplePassCredential.query.filter_by(
            business_id=customer.business_id, customer_id=customer.id,
        ).one_or_none()
        if credential:
            if credential.pass_type_identifier != self.pass_type_id:
                raise RuntimeError("Stored Apple pass identity does not match the configured pass.")
            return credential
        from app.services.apple_pass_update_sequence import allocate_apple_update_tag
        serial = self._initial_serial_number(customer)
        token = self._derive_auth_token(customer, serial_number=serial)
        credential = ApplePassCredential(
            business_id=customer.business_id, customer_id=customer.id,
            pass_type_identifier=self.pass_type_id, serial_number=serial,
            auth_key_version=1, auth_token_digest=hashlib.sha256(token.encode()).hexdigest(),
            update_tag=allocate_apple_update_tag(),
            design_version=max(1, int(customer.business.wallet_design_version or 1)),
        )
        db.session.add(credential)
        db.session.flush()
        return credential

    def authentication_token(self, customer, credential=None):
        credential = credential or self.ensure_pass_credential(customer)
        if credential.revoked_at is not None:
            raise RuntimeError("Apple pass credential is revoked.")
        # Match by digest so retained keys remain valid even when a new key is
        # prepended to the rotation ring.
        for key in self._auth_keys():
            token = self._derive_auth_token_with_key(customer, key, credential.serial_number)
            if hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), credential.auth_token_digest):
                return token
        raise RuntimeError("Apple pass credential integrity check failed.")

    def authenticate(self, customer, supplied_token, credential=None):
        if not isinstance(supplied_token, str) or not supplied_token:
            return False
        try:
            expected = self.authentication_token(customer, credential)
        except RuntimeError:
            return False
        return hmac.compare_digest(expected, supplied_token)

    def get_serial_number(self, customer) -> str:
        """Return the persisted pass identity; legacy generation is only for first issue."""
        from app.models import ApplePassCredential
        credential = ApplePassCredential.query.filter_by(
            business_id=customer.business_id, customer_id=customer.id,
        ).one_or_none()
        if credential:
            return credential.serial_number
        return self._initial_serial_number(customer)

    @staticmethod
    def _initial_serial_number(customer) -> str:
        return f"customer_{customer.id}_{customer.qr_code_token[:8]}"

    def pass_generation_allowed(self):
        if self.mode == "real":
            return True
        environment = str(current_app.config.get("APP_ENV", "development")).lower() if has_app_context() else ""
        return bool(
            self.mode == "mock" and has_app_context() and
            (current_app.testing or environment in {"development", "testing"})
        )

    def _validate_pass_certificate(self, cert):
        now = datetime.now(timezone.utc)
        if cert.not_valid_before_utc > now or cert.not_valid_after_utc <= now:
            raise RuntimeError("Apple Pass Type certificate is not currently valid.")
        if self.pass_type_id not in [str(attribute.value) for attribute in cert.subject]:
            raise RuntimeError("Apple certificate does not match APPLE_PASS_TYPE_ID.")

    @staticmethod
    def _invitation_url(customer):
        if not has_app_context():
            return None
        from app.models import ReferralParticipant
        base = str(current_app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")
        parsed = urlparse(base)
        if parsed.scheme != "https" or not parsed.netloc:
            return None
        participant = ReferralParticipant.query.filter_by(
            business_id=customer.business_id,
            customer_id=customer.id,
            participant_type="customer_ambassador",
        ).first()
        if not participant or participant.revoked_at is not None:
            return None
        if participant.currently_eligible():
            return f"{base}/invite/{participant.invitation_code}"
        return getattr(customer, "_referral_wallet_invitation_url", None)

    def build_pass_json(self, customer) -> Dict[str, Any]:
        """Generates the dictionary structure for Apple Wallet pass.json."""
        business = customer.business
        serial_number = self.get_serial_number(customer)

        bg_color = hex_to_rgb(business.primary_color, "rgb(30, 58, 138)")
        label_color = hex_to_rgb(business.secondary_color, "rgb(255, 255, 255)")
        foreground_color = hex_to_rgb(business.wallet_text_color, "rgb(255, 255, 255)")
        balance_label = business.wallet_balance_label or (
            "Sellos" if business.loyalty_type == "stamps" else "Puntos"
        )

        # Primary field based on loyalty type
        if business.loyalty_type == "stamps":
            primary_fields = [
                {
                    "key": "stamps_balance",
                    "label": balance_label.upper(),
                    "value": f"{customer.current_stamps} / {business.stamps_reward_limit}",
                    "changeMessage": "¡Tu saldo de sellos se actualizó a %@!",
                }
            ]
            program_label = business.wallet_program_name or "Sellos de Lealtad"
        else:
            primary_fields = [
                {
                    "key": "points_balance",
                    "label": balance_label.upper(),
                    "value": f"{customer.current_points:.0f}",
                    "changeMessage": "¡Tu saldo de puntos se actualizó a %@ pts!",
                }
            ]
            program_label = business.wallet_program_name or "Puntos Acumulables"

        secondary_fields = [
            {
                "key": "customer_name",
                "label": "TITULAR",
                "value": customer.full_name,
            }
        ]
        card_text = (business.wallet_card_text or "Tu lealtad merece recompensas").strip()
        if card_text:
            secondary_fields.append({
                "key": "card_message",
                "label": "MENSAJE",
                "value": card_text[:60],
            })

        auxiliary_fields = [
            {
                "key": "program_type",
                "label": "PROGRAMA",
                "value": program_label,
            },
            {
                "key": "account_id",
                "label": "ID CLIENTE",
                "value": str(customer.id),
            },
        ]

        terms_text = business.wallet_terms or (
            f"Presenta esta tarjeta digital en cada visita a {business.name} "
            "para acumular beneficios exclusivos."
        )

        back_fields = [
            {
                "key": "instructions",
                "label": "Cómo utilizarla",
                "value": business.wallet_instructions or "Presenta tu tarjeta al realizar tu compra.",
            },
            {
                "key": "terms",
                "label": "Términos y Condiciones",
                "value": terms_text,
            },
            {
                "key": "customer_support",
                "label": "Atención al Cliente",
                "value": business.wallet_contact or "J2M Interactive",
            },
        ]
        invitation_url = self._invitation_url(customer)
        if invitation_url:
            back_fields.append({
                "key": "invite_friends",
                "label": "Invitar amigos",
                "value": invitation_url,
            })

        barcode_data = {
            "format": "PKBarcodeFormatQR",
            "message": customer.qr_code_token,
            "messageEncoding": "iso-8859-1",
            "altText": customer.qr_code_token,
        }

        pass_dict = {
            "formatVersion": 1,
            "passTypeIdentifier": self.pass_type_id,
            "serialNumber": serial_number,
            "teamIdentifier": self.team_id,
            "organizationName": business.name,
            "description": business.wallet_card_text or f"Tarjeta de Lealtad {business.name}",
            "logoText": business.wallet_program_name or business.name,
            "foregroundColor": foreground_color,
            "backgroundColor": bg_color,
            "labelColor": label_color,
            "barcodes": [barcode_data],
            "barcode": barcode_data,
            "storeCard": {
                "primaryFields": primary_fields,
                "secondaryFields": secondary_fields,
                "auxiliaryFields": auxiliary_fields,
                "backFields": back_fields,
            },
        }

        # Geofencing / Location support for proximity notifications on Lock Screen
        if business.latitude is not None and business.longitude is not None:
            pass_dict["locations"] = [
                {
                    "latitude": float(business.latitude),
                    "longitude": float(business.longitude),
                    "relevantText": f"¡Estás cerca de {business.name}! Muestra tu tarjeta y acumula beneficios.",
                }
            ]

        # PassKit authentication is deliberately independent from the loyalty QR.
        if self.web_service_url:
            parsed_service = urlparse(self.web_service_url)
            if self.mode == "real" and (parsed_service.scheme != "https" or not parsed_service.netloc):
                raise RuntimeError("APPLE_WEB_SERVICE_URL must be an absolute HTTPS URL in real mode.")
            pass_dict["webServiceURL"] = self.web_service_url
            pass_dict["authenticationToken"] = self.authentication_token(customer)

        return pass_dict

    def _sign_manifest(self, manifest_data: bytes) -> bytes:
        """Signs manifest.json using PKCS7 with Apple certificates or development fallback."""
        if (
            os.path.exists(self.cert_path)
            and os.path.exists(self.key_path)
        ):
            try:
                with open(self.cert_path, "rb") as f:
                    cert = x509.load_pem_x509_certificate(f.read())

                password_bytes = self.key_password.encode("utf-8") if self.key_password else None
                with open(self.key_path, "rb") as f:
                    private_key = serialization.load_pem_private_key(f.read(), password=password_bytes)

                self._validate_pass_certificate(cert)
                if not os.path.exists(self.wwdr_path):
                    raise RuntimeError("Apple WWDR certificate is required.")

                builder = pkcs7.PKCS7SignatureBuilder().set_data(manifest_data)
                builder = builder.add_signer(cert, private_key, hashes.SHA256())

                with open(self.wwdr_path, "rb") as f:
                    wwdr_cert = x509.load_pem_x509_certificate(f.read())
                    builder = builder.add_certificate(wwdr_cert)

                return builder.sign(
                    serialization.Encoding.DER,
                    options=[pkcs7.PKCS7Options.DetachedSignature],
                )
            except Exception as e:
                if self.mode == "real":
                    raise RuntimeError("Apple pass signing configuration is invalid.") from e
                logger.warning("Apple pass signing unavailable in non-real mode (%s).", type(e).__name__)

        if self.mode == "real":
            raise RuntimeError("Apple pass signing credentials are incomplete.")

        if not self.pass_generation_allowed():
            raise RuntimeError("Apple Wallet pass generation is disabled.")

        # Fallback dummy detached signature for local development / testing
        return b"PKCS7_DEV_DUMMY_SIGNATURE_" + hashlib.sha1(manifest_data).digest()

    def generate_pkpass(self, customer) -> io.BytesIO:
        """
        Builds and packs a complete .pkpass ZIP bundle in-memory.
        
        Returns:
            io.BytesIO: Binary stream of the .pkpass file.
        """
        if not self.pass_generation_allowed():
            raise RuntimeError("Apple Wallet pass generation is disabled.")
        pass_dict = self.build_pass_json(customer)
        pass_json_bytes = json.dumps(pass_dict, indent=2, ensure_ascii=False).encode("utf-8")

        business = customer.business
        logo_asset = self._business_asset(business.logo_path, (320, 100), fit=False)
        strip_asset = self._business_asset(business.cover_path, (750, 294), fit=True)

        # Required pass assets. Uploaded branding is converted to safe PNG data.
        assets = {
            "pass.json": pass_json_bytes,
            "icon.png": logo_asset or MINIMAL_PNG_BYTES,
            "icon@2x.png": logo_asset or MINIMAL_PNG_BYTES,
            "logo.png": logo_asset or MINIMAL_PNG_BYTES,
            "logo@2x.png": logo_asset or MINIMAL_PNG_BYTES,
            "strip.png": strip_asset or MINIMAL_PNG_BYTES,
            "strip@2x.png": strip_asset or MINIMAL_PNG_BYTES,
        }

        # Build manifest.json with SHA-1 hashes of all assets
        manifest = {}
        for filename, file_data in assets.items():
            manifest[filename] = hashlib.sha1(file_data).hexdigest()

        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")

        # Generate PKCS7 signature of manifest.json
        signature_bytes = self._sign_manifest(manifest_bytes)

        # Package into in-memory ZIP (.pkpass)
        pkpass_buffer = io.BytesIO()
        with zipfile.ZipFile(pkpass_buffer, "w", zipfile.ZIP_DEFLATED) as pkpass_zip:
            for filename, file_data in assets.items():
                pkpass_zip.writestr(filename, file_data)
            pkpass_zip.writestr("manifest.json", manifest_bytes)
            pkpass_zip.writestr("signature", signature_bytes)

        pkpass_buffer.seek(0)
        return pkpass_buffer

    @staticmethod
    def _business_asset(relative_path, size, fit):
        if not relative_path or not has_app_context():
            return None
        configured_upload = current_app.config.get("UPLOAD_FOLDER")
        if configured_upload and relative_path.startswith("uploads/"):
            static_root = Path(configured_upload).resolve()
            image_path = (static_root / Path(relative_path).name).resolve()
        else:
            static_root = Path(current_app.static_folder).resolve()
            image_path = (static_root / relative_path).resolve()
        try:
            image_path.relative_to(static_root)
            with Image.open(image_path) as source:
                image = source.convert("RGBA")
                if fit:
                    image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
                else:
                    image.thumbnail(size, Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, format="PNG", optimize=True)
                return output.getvalue()
        except (FileNotFoundError, OSError, UnidentifiedImageError, ValueError):
            logger.warning("Could not load business branding asset: %s", relative_path)
            return None

    def send_push_notification(self, push_token: str) -> dict:
        """
        Sends an empty APNs push notification to prompt the device to request the updated pass.
        In development / mock environments, logs the action and returns True.
        """
        if not isinstance(push_token, str) or not push_token or len(push_token) > 255:
            return {"ok": False, "status_code": 400, "permanent": True}
        if self.mode == "disabled":
            return {"ok": False, "status_code": 503, "permanent": True}
        if self.mode == "mock":
            return {"ok": True, "status_code": 200, "mock": True}
        if (self.mode != "real" or not self.cert_path or not self.key_path
                or not os.path.isfile(self.cert_path) or not os.path.isfile(self.key_path)):
            return {"ok": False, "status_code": 503, "permanent": True}
        host = str(current_app.config.get("APPLE_APNS_HOST", "https://api.push.apple.com")).rstrip("/")
        timeout = float(current_app.config.get("APPLE_APNS_TIMEOUT_SECONDS", 10))
        try:
            with open(self.cert_path, "rb") as cert_file:
                self._validate_pass_certificate(x509.load_pem_x509_certificate(cert_file.read()))
            ssl_context = ssl.create_default_context()
            ssl_context.load_cert_chain(self.cert_path, self.key_path, password=self.key_password)
            with httpx.Client(http2=True, verify=ssl_context, timeout=timeout) as client:
                response = client.post(
                    f"{host}/3/device/{push_token}", content=b"{}",
                    headers={"apns-topic": self.pass_type_id, "apns-push-type": "background", "content-type": "application/json"},
                )
            status = response.status_code
            permanent = status in {400, 403, 404, 410}
            return {"ok": status == 200, "status_code": status, "permanent": permanent}
        except (httpx.TimeoutException, httpx.NetworkError, OSError):
            return {"ok": False, "status_code": 503, "permanent": False}
