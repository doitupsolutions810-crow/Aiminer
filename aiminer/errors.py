"""Stable error codes. Tests mine inputs against these sets."""

from __future__ import annotations


class AiminerError(Exception):
    """Base error. ``code`` is a stable machine string, ``detail`` is diagnostic."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


class ScanError(AiminerError):
    """The text is not one bounded JSON value."""


class CanonError(AiminerError):
    """The value cannot be written as canonical JSON."""


class StoreError(AiminerError):
    """The receipt ledger cannot be used. Fail closed."""


SCAN_CODES = frozenset(
    {
        "type",
        "too_large",
        "empty",
        "trailing_junk",
        "depth",
        "truncated",
        "invalid_escape",
        "lone_surrogate",
        "control_char",
        "duplicate_key",
        "leading_zero",
        "invalid_number",
        "number_limit",
        "invalid_literal",
        "unexpected",
        "too_many_keys",
        "too_many_items",
        "string_too_long",
        "too_many_nodes",
        "trailing_comma",
    }
)

CANON_CODES = frozenset(
    {
        "cycle",
        "key_type",
        "key_collapse",
        "float",
        "invalid_number",
        "number_limit",
        "string_too_long",
        "lone_surrogate",
        "depth",
        "too_many_nodes",
        "too_many_keys",
        "too_many_items",
        "type",
    }
)

INTAKE_ONLY_CODES = frozenset(
    {
        "body_type",
        "unauthorized",
        "utf8",
        "bom",
        "forbidden_char",
        "envelope",
        "unknown_field",
        "missing_field",
        "id_type",
        "id_empty",
        "id_length",
        "id_chars",
        "id_form",
        "id_reserved",
        "type_type",
        "type_form",
        "accepted",
        "duplicate",
        "conflict",
    }
)

STORE_ONLY_CODES = frozenset(
    {
        "correlation_type",
        "correlation_empty",
        "correlation_length",
        "correlation_chars",
        "correlation_form",
        "correlation_reserved",
        "created",
    }
)

STORE_ERROR_CODES = frozenset({"corrupt", "closed", "path"})

INTAKE_RESULT_CODES = SCAN_CODES | CANON_CODES | INTAKE_ONLY_CODES | {"too_large"}
STORE_RESULT_CODES = CANON_CODES | STORE_ONLY_CODES | {"duplicate", "conflict"}
