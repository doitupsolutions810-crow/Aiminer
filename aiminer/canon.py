"""Canonical JSON.

Numbers are exact :class:`~decimal.Decimal` values, not binary floats.
``-0``, ``1.0``, ``1e0``, and ``1`` share one form. Digits are never rounded to
fit a precision: too many significant digits is ``number_limit``. The plain
spelling is rejected, before a long zero run is allocated, when it would
exceed ``max_chars``. The finished document is rejected on the same cap.
Object keys are NFC-normalized and ordered by UTF-16 code units (the RFC 8785
order), which differs from Unicode code-point order for non-BMP characters. A
float is rejected so ``0.1`` cannot enter through IEEE rounding. Shared or
cyclic containers are rejected rather than written twice. Lone surrogates in
keys are ``lone_surrogate``, not ``UnicodeEncodeError``. Bidirectional
controls are ``format_char``. Line and paragraph separators are escaped.
"""

from __future__ import annotations

import unicodedata
from decimal import Context, Decimal, DecimalException, localcontext

from aiminer.errors import CanonError
from aiminer.limits import Limits


# Overrides, isolates, and the marks that can reorder a displayed string.
BIDI_CONTROLS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)


def plain_width(sign: int, digit_count: int, exponent: int) -> int:
    """Length of the plain decimal spelling after trailing zeros are removed."""

    extra = 1 if sign else 0
    if exponent >= 0:
        return extra + digit_count + exponent
    if digit_count > -exponent:
        return extra + digit_count + 1
    return extra + 2 - exponent


def plain_parts(number: Decimal) -> tuple[int, list[int], int] | None:
    """Return sign, digits, and exponent. ``None`` when the plain form is ``0``."""

    parts = number.as_tuple()
    exponent = parts.exponent
    if not isinstance(exponent, int):
        raise ValueError("exponent")
    digits = [int(digit) for digit in parts.digits]
    if not digits:
        raise ValueError("exponent")
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not digits:
        return None
    return parts.sign, digits, exponent


def canonicalize(value: object, limits: Limits | None = None) -> str:
    bounds = limits if limits is not None else Limits()
    if not isinstance(bounds, Limits):
        raise ValueError("limits")
    text = _write(value, bounds, set(), 0, _Nodes())
    if len(text) > bounds.max_chars:
        raise CanonError("too_large", "canonical")
    return text


def canonical_bytes(value: object, limits: Limits | None = None) -> bytes:
    return canonicalize(value, limits).encode("utf-8")


class _Nodes:
    __slots__ = ("n",)

    def __init__(self) -> None:
        self.n = 0


def _write(value: object, bounds: Limits, seen: set[int], depth: int, nodes: _Nodes) -> str:
    nodes.n += 1
    if nodes.n > bounds.max_nodes:
        raise CanonError("too_many_nodes", "node cap")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if value.bit_length() > 400:
            raise CanonError("number_limit", "bit length")
        if value == 0:
            return "0"
        magnitude = abs(value)
        digits = 1
        rest = magnitude
        while rest >= 10:
            rest //= 10
            digits += 1
        if digits > bounds.max_number_literal or digits - 1 > bounds.max_exponent:
            raise CanonError("number_limit", "magnitude")
        try:
            with localcontext(Context(prec=digits + 2)):
                number = Decimal(value)
        except DecimalException as exc:
            raise CanonError("invalid_number", "decimal") from exc
        return _decimal(number, bounds)
    if isinstance(value, Decimal):
        return _decimal(value, bounds)
    if isinstance(value, float):
        raise CanonError("float", "binary float")
    if isinstance(value, str):
        return _escape(value, bounds)
    if value is None:
        return "null"
    if isinstance(value, dict):
        return _object(value, bounds, seen, depth, nodes)
    if isinstance(value, list):
        return _array(value, bounds, seen, depth, nodes)
    raise CanonError("type", type(value).__name__)


def _decimal(number: Decimal, bounds: Limits) -> str:
    # as_tuple() is exact. normalize() under a short Decimal context would
    # round 40 nines into a power of ten and silently change the value.
    # Width is decided before the zero run is built, so 1e-100000 cannot
    # allocate a giant digit string on the way to being rejected.
    if number.is_nan() or number.is_infinite():
        raise CanonError("invalid_number", "nonfinite")
    try:
        parts = plain_parts(number)
    except ValueError as exc:
        raise CanonError("invalid_number", "exponent") from exc
    if parts is None:
        return "0"
    sign, digits, exponent = parts
    if len(digits) > bounds.max_number_literal:
        raise CanonError("number_limit", "digits")
    adjusted = exponent + len(digits) - 1
    if adjusted > bounds.max_exponent or adjusted < -bounds.max_exponent:
        raise CanonError("number_limit", "magnitude")
    if plain_width(sign, len(digits), exponent) > bounds.max_chars:
        raise CanonError("number_limit", "width")
    rendered = "".join(str(digit) for digit in digits)
    prefix = "-" if sign else ""
    if exponent >= 0:
        body = rendered + ("0" * exponent)
    elif len(rendered) > -exponent:
        split = len(rendered) + exponent
        body = rendered[:split] + "." + rendered[split:]
    else:
        body = "0." + ("0" * (-exponent - len(rendered))) + rendered
    return prefix + body


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _escape(value: str, bounds: Limits) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized) > bounds.max_string:
        raise CanonError("string_too_long", "nfc")
    out = ['"']
    for char in normalized:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise CanonError("lone_surrogate", "string")
        if char in BIDI_CONTROLS:
            raise CanonError("format_char", "bidi")
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif code < 0x20 or code in (0x2028, 0x2029):
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _remember(container: object, seen: set[int]) -> None:
    identity = id(container)
    if identity in seen:
        raise CanonError("cycle", "shared container")
    seen.add(identity)


def _object(
    value: dict[object, object],
    bounds: Limits,
    seen: set[int],
    depth: int,
    nodes: _Nodes,
) -> str:
    if depth >= bounds.max_depth:
        raise CanonError("depth", "object")
    _remember(value, seen)
    folded: dict[str, object] = {}
    for key, child in value.items():
        if not isinstance(key, str):
            raise CanonError("key_type", type(key).__name__)
        if _has_surrogate(key):
            raise CanonError("lone_surrogate", "key")
        normalized = unicodedata.normalize("NFC", key)
        if _has_surrogate(normalized):
            raise CanonError("lone_surrogate", "key")
        if normalized in folded:
            raise CanonError("key_collapse", "nfc")
        if len(folded) >= bounds.max_keys:
            raise CanonError("too_many_keys", "object")
        folded[normalized] = child
    pieces: list[str] = []
    for key in sorted(folded, key=lambda item: item.encode("utf-16-be")):
        pieces.append(_escape(key, bounds) + ":" + _write(folded[key], bounds, seen, depth + 1, nodes))
    return "{" + ",".join(pieces) + "}"


def _array(
    value: list[object],
    bounds: Limits,
    seen: set[int],
    depth: int,
    nodes: _Nodes,
) -> str:
    if depth >= bounds.max_depth:
        raise CanonError("depth", "array")
    _remember(value, seen)
    if len(value) > bounds.max_items:
        raise CanonError("too_many_items", "array")
    return "[" + ",".join(_write(item, bounds, seen, depth + 1, nodes) for item in value) + "]"
