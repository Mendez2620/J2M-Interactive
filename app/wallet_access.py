from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


def issue_wallet_token(customer):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="wallet-issuance-v1")
    return serializer.dumps({"customer_id": customer.id, "business_id": customer.business_id, "purpose": "wallet"})


def validate_wallet_token(token, customer):
    try:
        payload = URLSafeTimedSerializer(
            current_app.config["SECRET_KEY"], salt="wallet-issuance-v1"
        ).loads(token, max_age=current_app.config["WALLET_TOKEN_MAX_AGE"])
    except (BadSignature, SignatureExpired):
        return False
    return payload == {"customer_id": customer.id, "business_id": customer.business_id, "purpose": "wallet"}
