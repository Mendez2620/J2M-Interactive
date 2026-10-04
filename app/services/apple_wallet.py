import io
import json
import os
import hashlib
import zipfile
import logging
from typing import Dict, Any, Optional
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7

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
    """Service to create Apple Wallet Pass JSON structures and bundled .pkpass files."""

    def __init__(
        self,
        pass_type_id: Optional[str] = None,
        team_id: Optional[str] = None,
        cert_path: Optional[str] = None,
        key_path: Optional[str] = None,
        key_password: Optional[str] = None,
        wwdr_path: Optional[str] = None,
    ):
        self.pass_type_id = (
            pass_type_id
            or os.getenv("APPLE_PASS_TYPE_ID")
            or "pass.com.j2m.loyalty"
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
        self.web_service_url = os.getenv("APPLE_WEB_SERVICE_URL")

    def build_pass_json(self, customer) -> Dict[str, Any]:
        """Generates the dictionary structure for Apple Wallet pass.json."""
        business = customer.business
        serial_number = f"customer_{customer.id}_{customer.qr_code_token[:8]}"

        bg_color = hex_to_rgb(business.primary_color, "rgb(30, 58, 138)")
        label_color = hex_to_rgb(business.secondary_color, "rgb(255, 255, 255)")
        foreground_color = "rgb(255, 255, 255)"

        # Primary field based on loyalty type
        if business.loyalty_type == "stamps":
            primary_fields = [
                {
                    "key": "stamps_balance",
                    "label": "SELLOS",
                    "value": f"{customer.current_stamps} / {business.stamps_reward_limit}",
                }
            ]
            program_label = "Sellos de Lealtad"
        else:
            primary_fields = [
                {
                    "key": "points_balance",
                    "label": "PUNTOS",
                    "value": f"{customer.current_points:.0f}",
                }
            ]
            program_label = "Puntos Acumulables"

        secondary_fields = [
            {
                "key": "customer_name",
                "label": "TITULAR",
                "value": customer.full_name,
            }
        ]

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

        back_fields = [
            {
                "key": "terms",
                "label": "Términos y Condiciones",
                "value": (
                    f"Presenta esta tarjeta digital en cada visita a {business.name} "
                    "para acumular beneficios exclusivos."
                ),
            },
            {
                "key": "customer_support",
                "label": "Atención al Cliente",
                "value": customer.email or "soporte@j2minteractive.com",
            },
        ]

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
            "description": f"Tarjeta de Lealtad {business.name}",
            "logoText": business.name,
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

        # Optional webServiceURL and authenticationToken for APNs push updates
        if self.web_service_url:
            pass_dict["webServiceURL"] = self.web_service_url
            pass_dict["authenticationToken"] = customer.qr_code_token

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

                builder = pkcs7.PKCS7SignatureBuilder().set_data(manifest_data)
                builder = builder.add_signer(cert, private_key, hashes.SHA256())

                if os.path.exists(self.wwdr_path):
                    with open(self.wwdr_path, "rb") as f:
                        wwdr_cert = x509.load_pem_x509_certificate(f.read())
                        builder = builder.add_certificate(wwdr_cert)

                return builder.sign(
                    serialization.Encoding.DER,
                    options=[pkcs7.PKCS7Options.DetachedSignature],
                )
            except Exception as e:
                logger.warning(
                    "Failed to sign with certificates at %s (%s). Using dev signature.",
                    self.cert_path,
                    str(e),
                )

        # Fallback dummy detached signature for local development / testing
        return b"PKCS7_DEV_DUMMY_SIGNATURE_" + hashlib.sha1(manifest_data).digest()

    def generate_pkpass(self, customer) -> io.BytesIO:
        """
        Builds and packs a complete .pkpass ZIP bundle in-memory.
        
        Returns:
            io.BytesIO: Binary stream of the .pkpass file.
        """
        pass_dict = self.build_pass_json(customer)
        pass_json_bytes = json.dumps(pass_dict, indent=2, ensure_ascii=False).encode("utf-8")

        # Required pass assets
        assets = {
            "pass.json": pass_json_bytes,
            "icon.png": MINIMAL_PNG_BYTES,
            "icon@2x.png": MINIMAL_PNG_BYTES,
            "logo.png": MINIMAL_PNG_BYTES,
            "logo@2x.png": MINIMAL_PNG_BYTES,
            "strip.png": MINIMAL_PNG_BYTES,
            "strip@2x.png": MINIMAL_PNG_BYTES,
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
