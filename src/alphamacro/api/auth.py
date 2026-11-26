"""API-key utilities. Keys are shown once at creation; only hashes stored."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping, Sequence
from typing import Final

KEY_PREFIX: Final = "amc_"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str]:
    """Return ``(plaintext_key, sha256_hash)``. Store only the hash."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    return key, hash_key(key)


def verify_key(provided: str | None, hashes: Mapping[str, str]) -> str | None:
    """Return the key *name* if ``provided`` matches a stored hash
    User constant-time compare."""
    if not provided:
        return None
    digest = hash_key(provided)
    found: str | None = None
    for (
        name,
        stored,
    ) in hashes.items():  # no early exit: timing does not reveal which key matched
        if hmac.compare_digest(digest, stored):
            found = name
    return found


def keygen_main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="alphamacro-keygen", description="Create an API key."
    )
    parser.add_argument("name", help="Label for the key owner, e.g. 'acme-research'.")
    args = parser.parse_args(argv)
    if ":" in args.name or "," in args.name:
        parser.error("name must not contain ':' or ','")
    key, digest = generate_key()
    print("API key (shown ONCE, give it to the client, never commit it):")
    print(f"  {key}\n")
    print("Add this to the server environment (.env):")
    print(f"  API_KEY_HASHES={args.name}:{digest}")
    print("(append with a comma to keep existing keys)")
    return 0
