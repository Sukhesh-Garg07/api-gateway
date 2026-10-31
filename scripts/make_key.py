"""Generate an API key and the SHA-256 hash stored for it.

Usage:
    python scripts/make_key.py            # prints a fresh secret + its hash
    python scripts/make_key.py mysecret   # prints the hash of a chosen secret

Put the hash in the GATEWAY_API_KEYS_FILE JSON ({"key-id": "<hash>"}) or hand
the secret to the client, who sends it in the X-API-Key header.
"""

import secrets
import sys

sys.path.insert(0, ".")
from app.config import hash_key  # noqa: E402

secret = sys.argv[1] if len(sys.argv) > 1 else secrets.token_urlsafe(32)
print(f"secret (give to the client): {secret}")
print(f"sha256 hash (store this):    {hash_key(secret)}")
