"""Byte-oriented intake.

Rejection order is part of the contract:

1. The body must be ``bytes`` or ``bytearray`` (``body_type``).
2. Size is checked before the token (``too_large``). An oversized body does
   not reveal whether the token was right, and it does not reveal ``boot``.
3. The token is compared exactly, with ``hmac.compare_digest``. It is not
   trimmed and not Unicode-normalized (``unauthorized``). ``boot`` stays
   hidden when the token is wrong, including when the body is not valid UTF-8.
4. UTF-8, a leading BOM, and a raw NUL are rejected.
5. The body must be one JSON value of the envelope ``id``, ``type``, ``payload``.
6. Only a canonical match is stored. A second body with the same id and a
   different canonical value is ``conflict`` and leaves the first row in place.
7. A memory intake stores at most ``max_entries`` distinct ids. A store has
   its own cap. A full ledger rejects a new id and still replays the old one.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from dataclasses import dataclass

from aiminer.canon import canonical_bytes
from aiminer.errors import INTAKE_RESULT_CODES, CanonError, ScanError, StoreError
from aiminer.ids import check_label, check_type
from aiminer.limits import Limits, check_max_entries
from aiminer.scan import parse_json
from aiminer.store import ReceiptStore, Stored

_BOOT = re.compile(r"^[0-9a-f]{32}$")
_FIELDS = frozenset({"id", "type", "payload"})


@dataclass(frozen=True)
class Decision:
    ok: bool
    code: str
    duplicate: bool = False
    receipt: str | None = None
    boot: str | None = None
    canonical: str | None = None


class Intake:
    def __init__(
        self,
        *,
        token: str,
        max_bytes: int = 4096,
        boot: str | None = None,
        limits: Limits | None = None,
        store: ReceiptStore | None = None,
        max_entries: int = 4096,
    ) -> None:
        if not isinstance(token, str) or token == "":
            raise ValueError("token")
        try:
            token.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("token") from exc
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 5_000_000:
            raise ValueError("max_bytes")
        if boot is None:
            boot = secrets.token_hex(16)
        elif not isinstance(boot, str) or _BOOT.fullmatch(boot) is None:
            raise ValueError("boot")
        bounds = limits if limits is not None else Limits()
        if not isinstance(bounds, Limits):
            raise ValueError("limits")
        if store is not None and not isinstance(store, ReceiptStore):
            raise ValueError("store")
        self.token = token
        self._token_digest = hashlib.sha256(token.encode("utf-8")).digest()
        self.max_bytes = max_bytes
        self.boot = boot
        self.limits = bounds
        self.store = store
        self.max_entries = check_max_entries(max_entries)
        self._memory: dict[str, tuple[bytes, str]] = {}
        self._lock = threading.Lock()

    def _token_ok(self, token: object) -> bool:
        if not isinstance(token, str):
            return False
        # compare_digest rejects non-ASCII str. Encode first so a token may
        # contain any Unicode scalar, and a lone surrogate cannot raise.
        try:
            presented = hashlib.sha256(token.encode("utf-8")).digest()
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(presented, self._token_digest)

    def submit(self, raw: object, token: object) -> Decision:
        if not isinstance(raw, (bytes, bytearray)):
            return self._finish(False, "body_type", authed=False)
        if len(raw) > self.max_bytes:
            return self._finish(False, "too_large", authed=False)
        if not self._token_ok(token):
            return self._finish(False, "unauthorized", authed=False)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return self._finish(False, "utf8", authed=True)
        if text.startswith("\ufeff"):
            return self._finish(False, "bom", authed=True)
        if "\x00" in text:
            return self._finish(False, "forbidden_char", authed=True)
        try:
            value = parse_json(text, self.limits)
        except ScanError as exc:
            return self._finish(False, exc.code, authed=True)
        if not isinstance(value, dict):
            return self._finish(False, "envelope", authed=True)
        if set(value) - _FIELDS:
            return self._finish(False, "unknown_field", authed=True)
        if set(value) != _FIELDS:
            return self._finish(False, "missing_field", authed=True)
        event_id = value["id"]
        label_error = check_label(event_id, what="id")
        if label_error is not None:
            return self._finish(False, label_error, authed=True)
        type_error = check_type(value["type"])
        if type_error is not None:
            return self._finish(False, type_error, authed=True)
        try:
            canon = canonical_bytes(value, self.limits)
        except CanonError as exc:
            return self._finish(False, exc.code, authed=True)
        canonical = canon.decode("utf-8")
        receipt = hashlib.sha256(canon).hexdigest()
        if self.store is not None:
            stored = self.store.submit(event_id, value, self.limits)
            return self._from_store(stored, canonical)
        assert isinstance(event_id, str)
        with self._lock:
            previous = self._memory.get(event_id)
            if previous is None:
                if len(self._memory) >= self.max_entries:
                    return self._finish(False, "too_many_entries", authed=True)
                self._memory[event_id] = (canon, receipt)
                return self._finish(
                    True, "accepted", receipt=receipt, canonical=canonical, authed=True
                )
            if previous[0] == canon:
                return self._finish(
                    True,
                    "duplicate",
                    duplicate=True,
                    receipt=previous[1],
                    canonical=canonical,
                    authed=True,
                )
            return self._finish(False, "conflict", authed=True)

    def _from_store(self, stored: Stored, canonical: str) -> Decision:
        if stored.code == "created":
            return self._finish(True, "accepted", receipt=stored.receipt, canonical=canonical, authed=True)
        if stored.code == "duplicate":
            return self._finish(
                True,
                "duplicate",
                duplicate=True,
                receipt=stored.receipt,
                canonical=canonical,
                authed=True,
            )
        if stored.code in INTAKE_RESULT_CODES:
            return self._finish(False, stored.code, authed=True)
        raise StoreError("corrupt", stored.code)

    def _finish(
        self,
        ok: bool,
        code: str,
        *,
        duplicate: bool = False,
        receipt: str | None = None,
        canonical: str | None = None,
        authed: bool,
    ) -> Decision:
        if code not in INTAKE_RESULT_CODES:
            raise StoreError("corrupt", code)
        boot = self.boot if authed else None
        if ok:
            if code not in {"accepted", "duplicate"} or receipt is None or canonical is None:
                raise StoreError("corrupt", "success shape")
            if duplicate != (code == "duplicate"):
                raise StoreError("corrupt", "duplicate flag")
            if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != receipt:
                raise StoreError("corrupt", "receipt hash")
        elif receipt is not None or canonical is not None or duplicate:
            raise StoreError("corrupt", "failure shape")
        if not authed and boot is not None:
            raise StoreError("corrupt", "boot leak")
        return Decision(ok, code, duplicate, receipt, boot, canonical)
