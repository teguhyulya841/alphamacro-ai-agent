"""Tamper-evident audit log (JSON Lines with a SHA-256 hash chain).

Each record embeds the hash of the previous record, so editing or deleting any record in the
middle of the file breaks verification. Truncating the *tail* is only detectable if the latest
hash (printed by ``alphamacro-audit verify``) is anchored somewhere else (ticket, WORM storage...).
By default only a hash of the query is stored, never its text, and users are pseudonymised.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64


def _digest(record: Mapping[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    records: int
    head_hash: str
    error: str | None = None


class AuditLog:
    """Append-only, thread-safe audit trail."""

    def __init__(self, path: str | Path, *, salt: str, store_query_text: bool = False) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._salt = salt.encode("utf-8")
        self.store_query_text = store_query_text
        self._lock = threading.Lock()
        result = self.verify(self._path)
        if not result.ok:
            raise ValueError(f"Existing audit log failed verification: {result.error}")
        self._seq = result.records
        self._prev = result.head_hash

    def pseudonymize(self, user_id: int | str) -> str:
        """Stable, non-reversible user reference (HMAC-SHA256 with a secret salt)."""
        return hmac.new(self._salt, str(user_id).encode("utf-8"), hashlib.sha256).hexdigest()[:16]

    def record(self, event: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            record: dict[str, Any] = {
                "seq": self._seq + 1,
                "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                **event,
                "prev_hash": self._prev,
            }
            record["hash"] = _digest(record)
            line = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._seq, self._prev = record["seq"], record["hash"]
            return record

    @staticmethod
    def verify(path: str | Path) -> VerifyResult:
        path = Path(path)
        if not path.exists():
            return VerifyResult(True, 0, GENESIS_HASH)
        prev, count = GENESIS_HASH, 0
        with path.open(encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return VerifyResult(False, count, prev, f"line {line_no}: invalid JSON")
                if record.get("seq") != count + 1:
                    return VerifyResult(False, count, prev, f"line {line_no}: sequence gap")
                if record.get("prev_hash") != prev:
                    return VerifyResult(False, count, prev, f"line {line_no}: chain broken")
                if record.get("hash") != _digest(record):
                    return VerifyResult(False, count, prev, f"line {line_no}: record modified")
                prev, count = record["hash"], count + 1
        return VerifyResult(True, count, prev)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alphamacro-audit", description="Audit log tools.")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="Verify the hash chain of an audit log file.")
    verify.add_argument("path")
    args = parser.parse_args(argv)

    result = AuditLog.verify(args.path)
    if result.ok:
        print(f"OK: {result.records} records. Head hash (anchor this): {result.head_hash}")
        return 0
    print(f"FAILED after {result.records} valid records: {result.error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
