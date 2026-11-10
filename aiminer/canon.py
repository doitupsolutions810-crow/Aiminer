"""Canonical JSON.

Numbers are exact :class:`~decimal.Decimal` values, not binary floats.
``-0``, ``1.0``, ``1e0``, and ``1`` share one form. Object keys are NFC-normalized
and ordered by UTF-16 code units (the RFC 8785 order), which differs from
Unicode code-point order for non-BMP characters. A float is rejected so
``0.1`` cannot enter through IEEE rounding. Shared or cyclic containers are
rejected rather than written twice.
"""

from __future__ import annotations

import unicodedata
from decimal import Context, Decimal, localcontext

from aiminer.errors import CanonError
from aiminer.limits import Limits


def canonicalize(value: object, limits: Limits | None = None) -> str:
    bounds = limits if limits is not None else Limits()
    if not isinstance(bounds, Limits):
        raise ValueError("limits")
    return _write(value, bounds, set(), 0, _Nodes())


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
        with localcontext(Context(prec=bounds.max_number_literal + 4)):
            return _decimal(Decimal(value), bounds)
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
    with localcontext(Context(prec=bounds.max_number_literal + 4)):
        if number.is_nan() or number.is_infinite():
            raise CanonError("invalid_number", "nonfinite")
        if number.is_zero():
            return "0"
        adjusted = number.adjusted()
        if adjusted > bounds.max_exponent or adjusted < -bounds.max_exponent:
            raise CanonError("number_limit", "magnitude")
        parts = number.copy_abs().normalize().as_tuple()
    if len(parts.digits) > bounds.max_number_literal:
        raise CanonError("number_limit", "digits")
    digits = "".join(str(digit) for digit in parts.digits)
    exponent = parts.exponent
    if not isinstance(exponent, int):
        raise CanonError("invalid_number", "exponent")
    sign = "-" if number.is_signed() else ""
    if exponent >= 0:
        body = digits + ("0" * exponent)
    elif len(digits) > -exponent:
        split = len(digits) + exponent
        body = digits[:split] + "." + digits[split:]
    else:
        body = "0." + ("0" * (-exponent - len(digits))) + digits
    return sign + body


def _escape(value: str, bounds: Limits) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized) > bounds.max_string:
        raise CanonError("string_too_long", "nfc")
    out = ['"']
    for char in normalized:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise CanonError("lone_surrogate", "string")
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif code < 0x20:
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
        if len(folded) >= bounds.max_keys:
            raise CanonError("too_many_keys", "object")
        normalized = unicodedata.normalize("NFC", key)
        if normalized in folded:
            raise CanonError("key_collapse", "nfc")
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
