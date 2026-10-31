"""API-key authentication.

Clients send their secret in the X-API-Key header. The gateway stores only
SHA-256 hashes of valid secrets, and compares the hash of the presented key
against every stored hash with hmac.compare_digest.
"""

from __future__ import annotations

import hmac

from .config import Settings, hash_key


class KeyStore:
    def __init__(self, settings: Settings):
        self._hashes = dict(settings.api_key_hashes)  # digest -> key id

    def authenticate(self, presented: str | None) -> str | None:
        """Return the key id for a valid presented secret, else None."""
        if not presented:
            return None
        digest = hash_key(presented)
        for stored_digest, key_id in self._hashes.items():
            if hmac.compare_digest(digest, stored_digest):
                return key_id
        return None

    def __len__(self) -> int:
        return len(self._hashes)
