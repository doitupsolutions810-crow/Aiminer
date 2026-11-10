"""Bounded JSON scanner.

The walker stops at ``max_depth`` (ceiling 64), so a deeply nested value
returns ``depth`` instead of raising ``RecursionError``. Number literals
stop being read once they pass the literal or exponent cap, so a multi-megabyte
digit run cannot force a big integer allocation. Duplicate keys are rejected.
``1e309``, which :func:`json.loads` turns into infinity, is rejected.
"""

from __future__ import annotations

from decimal import Context, Decimal, InvalidOperation, localcontext

from aiminer.errors import ScanError
from aiminer.limits import Limits

_WS = frozenset(" \t\r\n")
_HEX = frozenset("0123456789abcdefABCDEF")
_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


class _Nodes:
    __slots__ = ("n",)

    def __init__(self) -> None:
        self.n = 0


def parse_json(text: str, limits: Limits | None = None) -> object:
    """Parse one JSON value. Trailing whitespace is ignored. Anything else is junk."""

    bounds = limits if limits is not None else Limits()
    if not isinstance(bounds, Limits):
        raise ValueError("limits")
    if not isinstance(text, str):
        raise ScanError("type", "expected str")
    if len(text) > bounds.max_chars:
        raise ScanError("too_large", "character cap")
    index = _skip(text, 0)
    if index >= len(text):
        raise ScanError("empty", "no value")
    value, index = _value(text, index, bounds, 0, _Nodes())
    index = _skip(text, index)
    if index != len(text):
        raise ScanError("trailing_junk", "extra data")
    return value


def _skip(text: str, index: int) -> int:
    length = len(text)
    while index < length and text[index] in _WS:
        index += 1
    return index


def _bump(bounds: Limits, nodes: _Nodes) -> None:
    nodes.n += 1
    if nodes.n > bounds.max_nodes:
        raise ScanError("too_many_nodes", "node cap")


def _value(text: str, index: int, bounds: Limits, depth: int, nodes: _Nodes) -> tuple[object, int]:
    char = text[index]
    if char == "{":
        if depth >= bounds.max_depth:
            raise ScanError("depth", "object")
        _bump(bounds, nodes)
        return _object(text, index, bounds, depth, nodes)
    if char == "[":
        if depth >= bounds.max_depth:
            raise ScanError("depth", "array")
        _bump(bounds, nodes)
        return _array(text, index, bounds, depth, nodes)
    _bump(bounds, nodes)
    if char == '"':
        return _string(text, index, bounds)
    if char == "t":
        return _literal(text, index, "true", True)
    if char == "f":
        return _literal(text, index, "false", False)
    if char == "n":
        return _literal(text, index, "null", None)
    if char == "-" or char.isdigit():
        return _number(text, index, bounds)
    raise ScanError("unexpected", "value")


def _literal(text: str, index: int, word: str, value: object) -> tuple[object, int]:
    end = index + len(word)
    if text[index:end] != word:
        raise ScanError("invalid_literal", word)
    if end < len(text):
        nxt = text[end]
        if nxt.isalnum() or nxt == "_":
            raise ScanError("invalid_literal", "prefix")
    return value, end


def _hex4(text: str, index: int) -> tuple[int, int]:
    chunk = text[index : index + 4]
    if len(chunk) < 4 or any(char not in _HEX for char in chunk):
        raise ScanError("invalid_escape", "hex")
    return int(chunk, 16), index + 4


def _string(text: str, index: int, bounds: Limits) -> tuple[str, int]:
    index += 1
    length = len(text)
    out: list[str] = []
    count = 0
    while index < length:
        char = text[index]
        if char == '"':
            return "".join(out), index + 1
        if char == "\\":
            index += 1
            if index >= length:
                raise ScanError("invalid_escape", "truncated")
            escaped = text[index]
            index += 1
            if escaped in _ESCAPES:
                decoded = _ESCAPES[escaped]
            elif escaped == "u":
                code, index = _hex4(text, index)
                if 0xD800 <= code <= 0xDBFF:
                    if index + 1 >= length or text[index] != "\\" or text[index + 1] != "u":
                        raise ScanError("lone_surrogate", "high")
                    index += 2
                    low, index = _hex4(text, index)
                    if not 0xDC00 <= low <= 0xDFFF:
                        raise ScanError("lone_surrogate", "low")
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                elif 0xDC00 <= code <= 0xDFFF:
                    raise ScanError("lone_surrogate", "low")
                decoded = chr(code)
            else:
                raise ScanError("invalid_escape", "unknown")
            count += 1
            if count > bounds.max_string:
                raise ScanError("string_too_long", "escape")
            out.append(decoded)
            continue
        if ord(char) < 0x20:
            raise ScanError("control_char", "raw control")
        count += 1
        if count > bounds.max_string:
            raise ScanError("string_too_long", "char")
        out.append(char)
        index += 1
    raise ScanError("truncated", "string")


def _number(text: str, index: int, bounds: Limits) -> tuple[Decimal, int]:
    start = index
    length = len(text)

    def too_long() -> None:
        if index - start > bounds.max_number_literal:
            raise ScanError("number_limit", "literal length")

    if text[index] == "-":
        index += 1
        too_long()
        if index >= length:
            raise ScanError("invalid_number", "dangling minus")
    if index >= length or not text[index].isdigit():
        raise ScanError("invalid_number", "expected digit")
    if text[index] == "0":
        index += 1
        too_long()
        if index < length and text[index].isdigit():
            raise ScanError("leading_zero", "leading zero")
    else:
        while index < length and text[index].isdigit():
            index += 1
            too_long()
    if index < length and text[index] == ".":
        index += 1
        too_long()
        fraction = index
        while index < length and text[index].isdigit():
            index += 1
            too_long()
        if index == fraction:
            raise ScanError("invalid_number", "trailing decimal")
    if index < length and text[index] in "eE":
        index += 1
        too_long()
        if index < length and text[index] in "+-":
            index += 1
            too_long()
        exp_start = index
        exp_digits = 0
        digit_cap = len(str(bounds.max_exponent)) + 1
        while index < length and text[index].isdigit():
            exp_digits += 1
            index += 1
            too_long()
            if exp_digits > digit_cap:
                raise ScanError("number_limit", "exponent length")
        if exp_digits == 0:
            raise ScanError("invalid_number", "exponent")
        exponent = int(text[exp_start:index])
        if text[exp_start - 1] == "-":
            exponent = -exponent
        if abs(exponent) > bounds.max_exponent:
            raise ScanError("number_limit", "exponent")
    literal = text[start:index]
    try:
        # Default Decimal precision is 28 digits, which would silently round a
        # longer literal that is still inside max_number_literal.
        with localcontext(Context(prec=bounds.max_number_literal + 4)):
            number = Decimal(literal)
    except InvalidOperation as exc:
        raise ScanError("invalid_number", "decimal") from exc
    if number.is_nan() or number.is_infinite():
        raise ScanError("invalid_number", "nonfinite")
    if not number.is_zero():
        adjusted = number.adjusted()
        if adjusted > bounds.max_exponent or adjusted < -bounds.max_exponent:
            raise ScanError("number_limit", "magnitude")
    return number, index


def _object(
    text: str, index: int, bounds: Limits, depth: int, nodes: _Nodes
) -> tuple[dict[str, object], int]:
    index += 1
    index = _skip(text, index)
    found: dict[str, object] = {}
    if index < len(text) and text[index] == "}":
        return found, index + 1
    while True:
        if index >= len(text) or text[index] != '"':
            if index >= len(text):
                raise ScanError("truncated", "object")
            raise ScanError("unexpected", "object key")
        key, index = _string(text, index, bounds)
        index = _skip(text, index)
        if index >= len(text) or text[index] != ":":
            raise ScanError("unexpected", "colon")
        index = _skip(text, index + 1)
        # A repeated or overflowing key is reported before a missing value, so
        # truncation cannot hide the structural error.
        if key in found:
            raise ScanError("duplicate_key", "object")
        if len(found) >= bounds.max_keys:
            raise ScanError("too_many_keys", "object")
        if index >= len(text):
            raise ScanError("truncated", "object value")
        value, index = _value(text, index, bounds, depth + 1, nodes)
        found[key] = value
        index = _skip(text, index)
        if index >= len(text):
            raise ScanError("truncated", "object")
        if text[index] == ",":
            index = _skip(text, index + 1)
            if index >= len(text):
                raise ScanError("truncated", "object")
            if text[index] == "}":
                raise ScanError("trailing_comma", "object")
            continue
        if text[index] == "}":
            return found, index + 1
        raise ScanError("unexpected", "object")


def _array(
    text: str, index: int, bounds: Limits, depth: int, nodes: _Nodes
) -> tuple[list[object], int]:
    index += 1
    index = _skip(text, index)
    items: list[object] = []
    if index < len(text) and text[index] == "]":
        return items, index + 1
    while True:
        if index >= len(text):
            raise ScanError("truncated", "array")
        value, index = _value(text, index, bounds, depth + 1, nodes)
        if len(items) >= bounds.max_items:
            raise ScanError("too_many_items", "array")
        items.append(value)
        index = _skip(text, index)
        if index >= len(text):
            raise ScanError("truncated", "array")
        if text[index] == ",":
            index = _skip(text, index + 1)
            if index >= len(text):
                raise ScanError("truncated", "array")
            if text[index] == "]":
                raise ScanError("trailing_comma", "array")
            continue
        if text[index] == "]":
            return items, index + 1
        raise ScanError("unexpected", "array")
