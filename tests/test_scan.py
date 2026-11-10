"""Scanner corpus, ASCII matrix, and resource caps."""

from __future__ import annotations

import json
import math

import pytest

from aiminer import CANON_CODES, SCAN_CODES, CanonError, Limits, ScanError, canonicalize, parse_json
from aiminer.corpus import build_cases
from aiminer.__main__ import main

CASES = build_cases()


@pytest.mark.parametrize("case", CASES, ids=[str(item["name"]) for item in CASES])
def test_corpus_case(case: dict[str, object]) -> None:
    limits = Limits(**case["limits"]) if case["limits"] else Limits()
    try:
        value = parse_json(str(case["text"]), limits)
    except ScanError as exc:
        assert case["code"] == exc.code
        assert exc.code in SCAN_CODES
        return
    if case["code"] is not None:
        with pytest.raises(CanonError) as caught:
            canonicalize(value, limits)
        assert caught.value.code == case["code"]
        assert caught.value.code in CANON_CODES
        return
    assert not isinstance(value, float)
    rendered = canonicalize(value, limits)
    assert rendered == case["canon"]
    assert canonicalize(parse_json(rendered, limits), limits) == rendered
    assert b"\x00" not in rendered.encode("utf-8")


def test_corpus_is_large_and_unique() -> None:
    names = [item["name"] for item in CASES]
    assert len(names) >= 140
    assert len(names) == len(set(names))


def test_module_entrypoint() -> None:
    assert main() == 0


def test_non_string_inputs() -> None:
    for value in (b"1", None, 1, True, ["1"]):
        with pytest.raises(ScanError) as caught:
            parse_json(value)  # type: ignore[arg-type]
        assert caught.value.code == "type"


def test_limits_reject_bools_and_ceilings() -> None:
    with pytest.raises(ValueError):
        Limits(max_depth=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Limits(max_depth=65)
    with pytest.raises(ValueError):
        Limits(max_depth=-1)
    with pytest.raises(ValueError):
        parse_json("1", limits=object())  # type: ignore[arg-type]
    Limits(max_depth=0)
    Limits(max_depth=64)


def _ascii_expectation(char: str) -> tuple[str, str | None]:
    if char in " \t\r\n":
        return "empty", None
    if char in "0123456789":
        return "ok", char
    if char in "tfn":
        return "invalid_literal", None
    if char == "-":
        return "invalid_number", None
    if char in '"{[':
        return "truncated", None
    return "unexpected", None


@pytest.mark.parametrize("codepoint", range(128))
def test_ascii_singleton(codepoint: int) -> None:
    char = chr(codepoint)
    kind, canon = _ascii_expectation(char)
    if kind == "ok":
        assert canonicalize(parse_json(char)) == canon
        return
    with pytest.raises(ScanError) as caught:
        parse_json(char)
    assert caught.value.code == kind


def test_dangerous_pairs_stay_inside_scan_codes() -> None:
    alphabet = list(dict.fromkeys(" \t\r\n{}[]\",:-+0123456789truefalsn./\\eE"))
    failures: list[str] = []
    for left in alphabet:
        for right in alphabet:
            text = left + right
            try:
                value = parse_json(text)
            except ScanError as exc:
                if exc.code not in SCAN_CODES:
                    failures.append(f"{text!r} code {exc.code}")
                continue
            except Exception as exc:
                failures.append(f"{text!r} {type(exc).__name__}")
                continue
            try:
                rendered = canonicalize(value)
                if canonicalize(parse_json(rendered)) != rendered:
                    failures.append(f"{text!r} fixed point")
            except Exception as exc:
                failures.append(f"{text!r} canon {type(exc).__name__}")
    assert failures == []


def _nested_arrays(depth: int) -> list[object]:
    value: list[object] = []
    for _ in range(depth - 1):
        value = [value]
    return value


def test_depth_boundary_and_does_not_recurse_away() -> None:
    assert parse_json("[" * 32 + "]" * 32) == _nested_arrays(32)
    with pytest.raises(ScanError) as caught:
        parse_json("[" * 33 + "]" * 33)
    assert caught.value.code == "depth"
    with pytest.raises(ScanError) as caught:
        parse_json("[" * 4000)
    assert caught.value.code == "depth"


def test_unclosed_shallow_array_is_truncated() -> None:
    with pytest.raises(ScanError) as caught:
        parse_json("[" * 5)
    assert caught.value.code == "truncated"


def test_node_and_width_boundaries() -> None:
    limits = Limits(max_nodes=3, max_keys=8, max_items=8)
    assert canonicalize(parse_json('{"a":1,"b":2}', limits), limits) == '{"a":1,"b":2}'
    with pytest.raises(ScanError) as caught:
        parse_json('{"a":1,"b":2,"c":3}', limits)
    assert caught.value.code == "too_many_nodes"
    wide = Limits(max_items=4, max_nodes=20)
    parsed = parse_json("[1,2,3,4]", wide)
    assert [int(item) for item in parsed] == [1, 2, 3, 4]
    with pytest.raises(ScanError) as caught:
        parse_json("[1,2,3,4,5]", wide)
    assert caught.value.code == "too_many_items"


def test_literal_and_exponent_caps_stop_early() -> None:
    limits = Limits(max_exponent=100, max_number_literal=64, max_chars=200_000)

    class Probe(str):
        reads = 0

        def __getitem__(self, item: object) -> str:
            if isinstance(item, int):
                Probe.reads += 1
            return str.__getitem__(self, item)

    Probe.reads = 0
    assert canonicalize(parse_json(Probe("1" * 64), limits), limits) == "1" * 64
    with pytest.raises(ScanError) as caught:
        parse_json("1" * 65, limits)
    assert caught.value.code == "number_limit"

    Probe.reads = 0
    huge = Probe("1" + ("9" * 100_000))
    with pytest.raises(ScanError) as caught:
        parse_json(huge, limits)
    assert caught.value.code == "number_limit"
    assert 0 < Probe.reads < 500

    Probe.reads = 0
    exponent = Probe("1e" + ("9" * 100_000))
    with pytest.raises(ScanError) as caught:
        parse_json(exponent, Limits(max_chars=200_000))
    assert caught.value.code == "number_limit"
    assert 0 < Probe.reads < 500

    Probe.reads = 0
    blob = Probe('"' + ("a" * 100_000))
    with pytest.raises(ScanError) as caught:
        parse_json(blob, Limits(max_string=32, max_chars=200_000))
    assert caught.value.code == "string_too_long"
    assert 0 < Probe.reads < 500


def test_sign_counts_toward_literal_cap() -> None:
    limits = Limits(max_exponent=100, max_number_literal=8, max_chars=50)
    assert canonicalize(parse_json("-1234567", limits), limits) == "-1234567"
    with pytest.raises(ScanError) as caught:
        parse_json("-12345678", limits)
    assert caught.value.code == "number_limit"


def test_stdlib_json_accepts_cases_this_scanner_rejects() -> None:
    assert json.loads('{"a":1,"a":2}') == {"a": 2}
    with pytest.raises(ScanError) as caught:
        parse_json('{"a":1,"a":2}')
    assert caught.value.code == "duplicate_key"
    assert math.isinf(json.loads("1e309"))
    with pytest.raises(ScanError) as caught:
        parse_json("1e309")
    assert caught.value.code == "number_limit"


def test_exact_exponent_edges() -> None:
    assert canonicalize(parse_json("1e20")) == "1" + "0" * 20
    assert canonicalize(parse_json("1e-20")) == "0." + "0" * 19 + "1"
    for text in ("1e21", "1e-21", "1e+21", "-1e21", "5e309"):
        with pytest.raises(ScanError) as caught:
            parse_json(text)
        assert caught.value.code == "number_limit", text


def test_array_number_glued_is_unexpected() -> None:
    with pytest.raises(ScanError) as caught:
        parse_json("[1a]")
    assert caught.value.code == "unexpected"
    with pytest.raises(ScanError) as caught:
        parse_json("[01]")
    assert caught.value.code == "leading_zero"
