import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def _fernet():
    secret = current_app.config["SECRET_KEY"].encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret + b":wallet-issuance-v2").digest())
    return Fernet(key)


def issue_wallet_token(customer):
    payload = json.dumps({
        "customer_id": customer.id,
        "business_id": customer.business_id,
        "purpose": "wallet",
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(payload).decode("ascii")


def validate_wallet_token(token, customer):
    try:
        payload = json.loads(_fernet().decrypt(
            token.encode("ascii"), ttl=current_app.config["WALLET_TOKEN_MAX_AGE"]
        ))
    except (InvalidToken, UnicodeError, ValueError, json.JSONDecodeError):
        return False
    return payload == {
        "business_id": customer.business_id,
        "customer_id": customer.id,
        "purpose": "wallet",
    }
