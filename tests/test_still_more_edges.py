"""Edges that still expanded, reordered, or mislabeled a value.

A short exponent must not become a giant digit string. Bidirectional
controls must not survive inside a string. A canonical document must not
grow past the character cap, and a rejected write must not drop a stored row.
"""

from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal, getcontext

import pytest

from aiminer import (
    CANON_CODES,
    INTAKE_RESULT_CODES,
    SCAN_CODES,
    STORE_RESULT_CODES,
    CanonError,
    Intake,
    Limits,
    ReceiptStore,
    ScanError,
    StoreError,
    canonicalize,
    check_label,
    parse_json,
)
from aiminer.canon import BIDI_CONTROLS

TOKEN = "still-more"


def _body(payload: str, event_id: str = "j") -> bytes:
    return f'{{"id":"{event_id}","type":"e","payload":{payload}}}'.encode()


def test_bidi_controls_fail_in_every_string() -> None:
    assert "format_char" in SCAN_CODES
    assert "format_char" in CANON_CODES
    assert "format_char" in INTAKE_RESULT_CODES
    for char in sorted(BIDI_CONTROLS):
        raw = '"' + char + '"'
        escaped = '"\\u%04x"' % ord(char)
        for text in (raw, escaped, '{"k":' + raw + "}", "[" + escaped + "]"):
            with pytest.raises(ScanError) as caught:
                parse_json(text)
            assert caught.value.code == "format_char"
        with pytest.raises(CanonError) as caught:
            canonicalize("a" + char + "b")
        assert caught.value.code == "format_char"
        with pytest.raises(CanonError) as caught:
            canonicalize({char: 1})
        assert caught.value.code == "format_char"

    gate = Intake(token=TOKEN, boot="ab" * 16)
    hidden = gate.submit(_body('"\\u202e"'), "nope")
    assert hidden.code == "unauthorized" and hidden.boot is None
    shown = gate.submit(_body('"\\u202e"'), TOKEN)
    assert shown.code == "format_char" and shown.boot == gate.boot and shown.receipt is None
    assert check_label("a\u202eb", what="id") == "id_chars"
    assert gate.submit(_body("1", event_id="a\\u202eb"), TOKEN).code == "format_char"

    # A joiner and a soft hyphen are format characters, and they are not bidi.
    assert parse_json('"a\\u200db"') == "a\u200db"
    assert canonicalize("a\u200db") == '"a\u200db"'
    assert canonicalize("a\u00adb") == '"a\u00adb"'
    assert gate.submit(_body('"a\\u200db"'), TOKEN).code == "accepted"
    assert gate.submit(_body("1", event_id="a\\u200db"), TOKEN).code == "id_chars"
    assert gate.submit(_body("1", event_id="a\\u2028b"), TOKEN).code == "id_chars"
    kept = gate.submit(_body('"\\u2028"', event_id="sep"), TOKEN)
    assert kept.ok and kept.canonical is not None
    assert "\u2028" not in kept.canonical and "\\u2028" in kept.canonical


def test_line_separators_share_one_escaped_receipt() -> None:
    gate = Intake(token=TOKEN, boot="cd" * 16)
    raw = '{"id":"s","type":"e","payload":"\u2028"}'.encode()
    escaped = b'{"id":"s","type":"e","payload":"\\u2028"}'
    first = gate.submit(raw, TOKEN)
    second = gate.submit(escaped, TOKEN)
    assert first.ok and second.duplicate and second.receipt == first.receipt
    assert first.canonical is not None
    assert first.canonical.startswith("{") and first.canonical.endswith("}")
    assert "\\u2028" in first.canonical and "\u2028" not in first.canonical
    paragraph = canonicalize("\u2029")
    assert paragraph == '"\\u2029"'
    assert "\u2029" not in paragraph
    assert canonicalize(parse_json(paragraph)) == paragraph


def test_number_width_is_rejected_before_a_zero_run() -> None:
    tight = Limits(max_exponent=100_000, max_number_literal=64, max_chars=100)
    for text in ("1e100000", "1e-100000", "-1e100000", "1E+100000"):
        with pytest.raises(ScanError) as caught:
            parse_json(text, tight)
        assert caught.value.code == "number_limit"
    for number in (Decimal("1E100000"), Decimal("1E-100000"), Decimal("-1E+100000")):
        with pytest.raises(CanonError) as caught:
            canonicalize(number, tight)
        assert caught.value.code == "number_limit"

    exact = Limits(max_chars=21, max_exponent=20)
    assert canonicalize(parse_json("1e20", exact), exact) == "1" + "0" * 20
    assert len(canonicalize(parse_json("-1e20", Limits(max_chars=22)), Limits(max_chars=22))) == 22
    with pytest.raises(ScanError) as caught:
        parse_json("-1e20", Limits(max_chars=21, max_exponent=20))
    assert caught.value.code == "number_limit"

    body = _body("1e-100")
    limits = Limits(max_chars=len(body), max_exponent=100)
    with pytest.raises(ScanError) as caught:
        parse_json(body.decode(), limits)
    assert caught.value.code == "number_limit"
    gate = Intake(token=TOKEN, limits=limits, boot="ef" * 16)
    decision = gate.submit(body, TOKEN)
    assert decision.code == "number_limit" and decision.boot == gate.boot and not decision.ok


def test_canonical_document_cannot_outgrow_the_character_cap() -> None:
    assert "too_large" in CANON_CODES
    assert "too_large" in STORE_RESULT_CODES
    source = '{"n":1e20}'
    limits = Limits(max_chars=22, max_exponent=20)
    parsed = parse_json(source, limits)
    with pytest.raises(CanonError) as caught:
        canonicalize(parsed, limits)
    assert caught.value.code == "too_large"
    with pytest.raises(CanonError) as caught:
        canonicalize({"n": Decimal("1e20")}, limits)
    assert caught.value.code == "too_large"
    assert canonicalize({"n": Decimal("1e20")}, Limits(max_chars=27)) == '{"n":' + "1" + "0" * 20 + "}"

    body = _body('{"n":1e20}')
    room = Limits(max_chars=len(body), max_exponent=20)
    parse_json(body.decode(), room)
    gate = Intake(token=TOKEN, limits=room, boot="12" * 16)
    decision = gate.submit(body, TOKEN)
    assert decision.code == "too_large" and decision.receipt is None and decision.boot == gate.boot
    assert canonicalize("a", Limits(max_chars=3)) == '"a"'
    with pytest.raises(CanonError) as caught:
        canonicalize("a", Limits(max_chars=2))
    assert caught.value.code == "too_large"


def test_adjusted_exponents_and_context_stay_exact() -> None:
    before = getcontext().prec
    wide = Limits(max_number_literal=50, max_exponent=50, max_chars=80)
    assert canonicalize(parse_json("1.50e1")) == "15"
    assert canonicalize(parse_json("0.1e20", wide), wide) == "1" + "0" * 19
    assert canonicalize(parse_json("10e-1")) == "1"
    assert canonicalize(parse_json("-0.0e-1")) == "0"
    assert canonicalize(parse_json("1e+020", wide), wide) == "1" + "0" * 20
    with pytest.raises(ScanError) as caught:
        parse_json("10e20")
    assert caught.value.code == "number_limit"
    with pytest.raises(ScanError) as caught:
        parse_json("1e+0020")
    assert caught.value.code == "number_limit"
    parse_json("9" * 40, wide)
    with pytest.raises(ScanError):
        parse_json("1e-100000", Limits(max_exponent=100_000, max_chars=50))
    assert getcontext().prec == before


def test_surrogate_pairs_and_literal_suffixes() -> None:
    assert parse_json('"\\ud83d\\ude00"') == "\U0001f600"
    assert canonicalize(parse_json('"\\ud83d\\ude00"')) == '"😀"'
    with pytest.raises(ScanError) as caught:
        parse_json('{"😀":1,"\\ud83d\\ude00":2}')
    assert caught.value.code == "duplicate_key"
    for text in ('"\\ud800"', '"\\ude00"', '"\\uD800\\u0041"', '"\\ud800\\ud800"'):
        with pytest.raises(ScanError) as caught:
            parse_json(text)
        assert caught.value.code == "lone_surrogate"
    with pytest.raises(ScanError) as caught:
        parse_json("true_")
    assert caught.value.code == "invalid_literal"
    with pytest.raises(ScanError) as caught:
        parse_json("null0")
    assert caught.value.code == "invalid_literal"
    with pytest.raises(ScanError) as caught:
        parse_json("falseé")
    assert caught.value.code == "trailing_junk"
    with pytest.raises(ScanError) as caught:
        parse_json("[falseé]")
    assert caught.value.code == "unexpected"
    for text in ("1e", "1e+", "1e-", "-.1", ".1"):
        with pytest.raises(ScanError) as caught:
            parse_json(text)
        assert caught.value.code in {"invalid_number", "unexpected"}
    with pytest.raises(ScanError) as caught:
        parse_json("[1, ]")
    assert caught.value.code == "trailing_comma"
    with pytest.raises(ScanError) as caught:
        parse_json('{"a": 1 , }')
    assert caught.value.code == "trailing_comma"
    with pytest.raises(ScanError) as caught:
        parse_json("[,1]")
    assert caught.value.code == "unexpected"


def test_intake_precedence_nfc_and_conflicts() -> None:
    gate = Intake(token=TOKEN, boot="34" * 16, max_bytes=4000)
    assert gate.submit(b'{"id":"a","extra":1}', TOKEN).code == "unknown_field"
    assert gate.submit(b'{"id":"a","id":"b"}', TOKEN).code == "duplicate_key"
    composed = gate.submit('{"id":"é","type":"e","payload":"e\\u0301"}'.encode(), TOKEN)
    assert composed.ok
    again = gate.submit(b'{"id":"\\u00e9","type":"e","payload":"\\u00e9"}', TOKEN)
    assert again.duplicate and again.receipt == composed.receipt
    folded_key = gate.submit(b'{"id":"k","type":"e","payload":{"e\\u0301":1}}', TOKEN)
    assert gate.submit(b'{"id":"k","type":"e","payload":{"\\u00e9":1}}', TOKEN).receipt == folded_key.receipt
    assert gate.submit('{"id":"e\\u0301","type":"e","payload":1}'.encode(), TOKEN).code == "id_form"
    clash = gate.submit(b'{"id":"\\u00e9","type":"e","payload":0}', TOKEN)
    assert clash.code == "conflict" and clash.receipt is None
    assert gate.submit(b'{"id":"bits","type":"e","payload":false}', TOKEN).ok
    assert gate.submit(b'{"id":"bits","type":"e","payload":0}', TOKEN).code == "conflict"
    assert gate.submit(b'{"id":"box","type":"e","payload":[]}', TOKEN).ok
    assert gate.submit(b'{"id":"box","type":"e","payload":{}}', TOKEN).code == "conflict"

    with pytest.raises(ScanError) as caught:
        parse_json('{"a":{"é":1,"e\\u0301":2}}', Limits(max_depth=1))
    assert caught.value.code == "depth"
    with pytest.raises(CanonError) as caught:
        canonicalize(parse_json('{"é":1,"e\\u0301":2}'))
    assert caught.value.code == "key_collapse"

    empty = Intake(token=TOKEN, max_entries=0, boot="56" * 16)
    assert empty.submit(b'{"id":"a","type":"e","payload":1}', TOKEN).code == "too_many_entries"
    assert empty.submit(b'{"id":"a","type":"e","payload":1}', TOKEN).code == "too_many_entries"


def test_rejected_write_keeps_the_row_and_the_schema(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "kept.sqlite"
    store = ReceiptStore(path, max_entries=2)
    assert store.submit("ok", {"n": Decimal("1.0")}).code == "created"
    assert store.submit("ok", "\u202e").code == "format_char"
    assert store.submit("ok", Decimal("1E100000"), Limits(max_exponent=100_000, max_chars=20)).code == "number_limit"
    assert store.submit("wide", {"n": Decimal("1e20")}, Limits(max_chars=22)).code == "too_large"
    assert store.count() == 1
    assert store.submit("ok", {"n": 1}).code == "duplicate"
    store.close()
    store.close()
    with pytest.raises(StoreError) as caught:
        store.submit("ok", 1)
    assert caught.value.code == "closed"

    reopened = ReceiptStore(path, max_entries=0)
    assert reopened.submit("ok", {"n": 1}).code == "duplicate"
    assert reopened.submit("new", 1).code == "too_many_entries"
    assert reopened.count() == 1
    reopened.close()

    with pytest.raises(StoreError) as caught:
        ReceiptStore(tmp_path)
    assert caught.value.code == "path"

    extra = tmp_path / "extra.sqlite"
    connection = sqlite3.connect(extra)
    connection.execute(
        "CREATE TABLE receipts (correlation_id TEXT PRIMARY KEY, canon BLOB NOT NULL, receipt TEXT NOT NULL, extra TEXT)"
    )
    connection.commit()
    connection.close()
    with pytest.raises(StoreError) as caught:
        ReceiptStore(extra)
    assert caught.value.code == "corrupt"

    flipped = tmp_path / "flipped.sqlite"
    connection = sqlite3.connect(flipped)
    connection.execute(
        "CREATE TABLE receipts (receipt TEXT NOT NULL, canon BLOB NOT NULL, correlation_id TEXT PRIMARY KEY)"
    )
    connection.commit()
    connection.close()
    with pytest.raises(StoreError) as caught:
        ReceiptStore(flipped)
    assert caught.value.code == "corrupt"

    with ReceiptStore(tmp_path / "fresh.sqlite") as managed:
        assert managed.submit("row", [1, Decimal("1.0")]).code == "created"
    with pytest.raises(StoreError) as caught:
        managed.submit("row", [1])
    assert caught.value.code == "closed"


def test_memory_cap_under_threads_admits_one_winner_per_slot() -> None:
    gate = Intake(token=TOKEN, max_entries=5, boot="78" * 16)
    barrier = threading.Barrier(20)
    outcomes: list[str] = []
    lock = threading.Lock()

    def work(index: int) -> None:
        barrier.wait(5)
        body = f'{{"id":"id-{index}","type":"e","payload":{index}}}'.encode()
        result = gate.submit(body, TOKEN)
        with lock:
            outcomes.append(result.code)

    threads = [threading.Thread(target=work, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert outcomes.count("accepted") == 5
    assert set(outcomes) <= {"accepted", "too_many_entries"}
    assert gate.submit(b'{"id":"id-0","type":"e","payload":0}', TOKEN).code in {"duplicate", "too_many_entries"}
    # The five winners still replay, and a twenty-first id does not.
    winners = 0
    for index in range(20):
        body = f'{{"id":"id-{index}","type":"e","payload":{index}}}'.encode()
        if gate.submit(body, TOKEN).duplicate:
            winners += 1
    assert winners == 5
    assert gate.submit(b'{"id":"overflow","type":"e","payload":1}', TOKEN).code == "too_many_entries"
