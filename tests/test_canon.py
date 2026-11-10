"""Canonical form, Unicode key order, and value-level rejections."""

from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal

import pytest

from aiminer import CANON_CODES, CanonError, Limits, canonicalize, canonical_bytes, parse_json


def test_bool_is_not_an_integer_and_float_is_rejected() -> None:
    assert canonicalize(True) == "true"
    assert canonicalize(False) == "false"
    assert canonicalize(1) == "1"
    assert canonicalize(0) == "0"
    assert canonicalize(None) == "null"
    with pytest.raises(CanonError) as caught:
        canonicalize(1.0)
    assert caught.value.code == "float"
    assert caught.value.code in CANON_CODES


def test_signed_zero_and_decimal_equivalence() -> None:
    forms = ["0", "-0", "0.0", "-0.0", "0e+20", "0e-5"]
    rendered = {canonicalize(parse_json(item)) for item in forms}
    assert rendered == {"0"}
    assert canonicalize(Decimal("-0")) == "0"
    assert canonicalize(Decimal("1.0")) == canonicalize(Decimal("1e0")) == "1"
    assert canonicalize(Decimal("1e2")) == canonicalize(100) == "100"
    assert canonicalize(Decimal("0.10")) == "0.1"


def test_nonfinite_decimals() -> None:
    for text in ("NaN", "Infinity", "-Infinity", "sNaN"):
        with pytest.raises(CanonError) as caught:
            canonicalize(Decimal(text))
        assert caught.value.code == "invalid_number"


def test_utf16_key_order_differs_from_code_point_order() -> None:
    emoji = "\U0001f600"
    high = "\uffff"
    rendered = canonicalize({high: 1, emoji: 2, "a": 3, "a\u0000": 4})
    assert rendered.index(emoji) < rendered.index(high)
    assert rendered.index('"a"') < rendered.index(r'"a\u0000"')
    point_order = sorted([high, emoji])
    assert point_order == [high, emoji]
    assert canonicalize(OrderedDict([("b", 1), ("a", 2)])) == '{"a":2,"b":1}'


def test_nfc_collapses_keys_and_values() -> None:
    composed = "\u00e9"
    decomposed = "e\u0301"
    assert canonicalize(decomposed) == canonicalize(composed) == '"é"'
    with pytest.raises(CanonError) as caught:
        canonicalize({composed: 1, decomposed: 2})
    assert caught.value.code == "key_collapse"
    assert b"\x00" not in canonical_bytes("\u0000")
    assert canonicalize("\u0000") == r'"\u0000"'


def test_cycles_diamonds_and_foreign_types() -> None:
    loop: list[object] = []
    loop.append(loop)
    with pytest.raises(CanonError) as caught:
        canonicalize(loop)
    assert caught.value.code == "cycle"
    shared: dict[str, int] = {"n": 1}
    with pytest.raises(CanonError) as caught:
        canonicalize({"a": shared, "b": shared})
    assert caught.value.code == "cycle"
    assert canonicalize({"a": {}, "b": {}}) == '{"a":{},"b":{}}'
    box: dict[str, object] = {}
    box["self"] = box
    with pytest.raises(CanonError) as caught:
        canonicalize(box)
    assert caught.value.code == "cycle"
    for value in (("a",), {1, 2}, b"a", complex(1, 2), object()):
        with pytest.raises(CanonError) as caught:
            canonicalize(value)
        assert caught.value.code == "type"
    with pytest.raises(CanonError) as caught:
        canonicalize({1: "a"})
    assert caught.value.code == "key_type"
    with pytest.raises(CanonError) as caught:
        canonicalize("\ud800")
    assert caught.value.code == "lone_surrogate"


def test_direct_limits() -> None:
    with pytest.raises(CanonError) as caught:
        canonicalize("abcd", Limits(max_string=3))
    assert caught.value.code == "string_too_long"
    with pytest.raises(CanonError) as caught:
        canonicalize([[]], Limits(max_depth=1))
    assert caught.value.code == "depth"
    with pytest.raises(CanonError) as caught:
        canonicalize([1, 2], Limits(max_items=1))
    assert caught.value.code == "too_many_items"
    with pytest.raises(CanonError) as caught:
        canonicalize({"a": 1, "b": 2}, Limits(max_keys=1))
    assert caught.value.code == "too_many_keys"
    with pytest.raises(CanonError) as caught:
        canonicalize([1], Limits(max_nodes=1))
    assert caught.value.code == "too_many_nodes"
    with pytest.raises(CanonError) as caught:
        canonicalize(10**21)
    assert caught.value.code == "number_limit"
    assert canonicalize(10**20) == "1" + "0" * 20
    with pytest.raises(CanonError) as caught:
        canonicalize(10**5000)
    assert caught.value.code == "number_limit"
    with pytest.raises(ValueError):
        canonicalize(1, object())  # type: ignore[arg-type]
