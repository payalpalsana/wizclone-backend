# app/core/security.py

# Import JWT functions from python-jose library
from jose import jwt
from jose.exceptions import JWTError

from app.core.config import settings



async def verify_monday_token(token: str):
    """
    ─────────────────────────────────────────
    verify_monday_token()
    ─────────────────────────────────────────
    This function verifies a JWT token received from monday.com
    It checks:
    1. Token is valid (not tampered)
    2. Token is signed with correct secret
    3. Token is not expired

    If valid → return decoded data (payload)
    If invalid → return None
    """

    try:
        # Decode the JWT token using secret key
        payload = jwt.decode(
            token,                                   # Token received from client/monday.com
            settings.monday_signing_secret,          # Secret key (used to verify signature)
            algorithms=["HS256"],                    # Algorithm used for encoding
            options={"verify_aud": False}            # safety
        )

        # If decoding is successful → token is valid
        return payload

    except JWTError:
        return None
    

