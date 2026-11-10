"""Edges that mislabeled corruption or measured strings before NFC.

A non-blob canon used to come back as a conflict, and a text canon used to
raise TypeError while holding the write transaction. String caps now follow
the normalized length, and a raw surrogate never parses.
"""

from __future__ import annotations

import sqlite3
import unicodedata

import pytest

from aiminer import (
    CanonError,
    Intake,
    Limits,
    ReceiptStore,
    ScanError,
    StoreError,
    canonicalize,
    parse_json,
)

TOKEN = "deeper-edges"
DECOMPOSED = "\u03b1\u0313\u0300\u0345"
COMPOSED = "\u1f82"


def test_raw_surrogate_and_number_aliases_fail_closed() -> None:
    with pytest.raises(ScanError) as caught:
        parse_json('"\ud800"')
    assert caught.value.code == "lone_surrogate"
    with pytest.raises(ScanError) as caught:
        parse_json('["\udfff"]')
    assert caught.value.code == "lone_surrogate"
    for text in ("+1", "−1", "＋1", "1e−1"):
        with pytest.raises(ScanError) as caught:
            parse_json(text)
        assert caught.value.code in {"unexpected", "invalid_number"}
    for text in ("1_000", "1,2", "0x10", "1e2e3"):
        with pytest.raises(ScanError) as caught:
            parse_json(text)
        assert caught.value.code == "trailing_junk"
    with pytest.raises(ScanError) as caught:
        parse_json("-01")
    assert caught.value.code == "leading_zero"
    assert canonicalize(parse_json(r'"\/"')) == '"/"'
    with pytest.raises(ScanError) as caught:
        parse_json(r'{"\u002f":1,"/":2}')
    assert caught.value.code == "duplicate_key"


def test_max_string_follows_nfc_length() -> None:
    assert unicodedata.normalize("NFC", "\ufb2c") == "\u05e9\u05bc\u05c1"
    assert unicodedata.normalize("NFC", DECOMPOSED) == COMPOSED
    assert len(DECOMPOSED) == 4
    tight = Limits(max_string=1)
    with pytest.raises(ScanError) as caught:
        parse_json('"\ufb2c"', tight)
    assert caught.value.code == "string_too_long"
    with pytest.raises(CanonError) as caught:
        canonicalize("\ufb2c", tight)
    assert caught.value.code == "string_too_long"
    assert parse_json('"' + DECOMPOSED + '"', tight) == DECOMPOSED
    assert canonicalize(DECOMPOSED, tight) == canonicalize(COMPOSED, tight) == '"' + COMPOSED + '"'
    # Five raw characters cannot fit in one normalized character.
    with pytest.raises(ScanError) as caught:
        parse_json('"\u0300\u0300\u0300\u0300\u0300"', tight)
    assert caught.value.code == "string_too_long"

    # "payload" is 7 code points, so the cap has to allow the envelope keys.
    # Three copies of U+FB2C are 3 raw characters and 9 after NFC.
    room = Limits(max_string=7)
    grown = "\ufb2c" * 3
    assert len(grown) == 3 and len(unicodedata.normalize("NFC", grown)) == 9
    gate = Intake(token=TOKEN, limits=room, max_bytes=400, boot="ab" * 16)
    assert gate.submit(('{"id":"j","type":"e","payload":"' + grown + '"}').encode(), TOKEN).code == "string_too_long"
    first = '{"id":"j","type":"e","payload":"' + COMPOSED + '"}'
    second = '{"id":"j","type":"e","payload":"' + DECOMPOSED + '"}'
    opened = gate.submit(first.encode(), TOKEN)
    replay = gate.submit(second.encode(), TOKEN)
    assert opened.ok and replay.duplicate and replay.receipt == opened.receipt
    decomposed_id = '{"id":"' + DECOMPOSED + '","type":"e","payload":1}'
    assert gate.submit(decomposed_id.encode(), TOKEN).code == "id_form"
    assert gate.submit(b'{"id":"k","type":"e","payload":1}', TOKEN).code == "accepted"


def test_corrupt_canon_is_not_a_conflict_and_does_not_wedge(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "ledger.sqlite"
    store = ReceiptStore(path)
    assert store.submit("keep", {"n": 1}).code == "created"
    store.close()

    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO receipts(correlation_id, canon, receipt) VALUES ('num', 5, 'zz')")
    connection.execute("INSERT INTO receipts(correlation_id, canon, receipt) VALUES ('txt', 'hello', 'zz')")
    good = canonicalize({"n": 1}).encode()
    connection.execute(
        "INSERT INTO receipts(correlation_id, canon, receipt) VALUES ('poison', ?, ?)",
        (good, "0" * 64),
    )
    connection.commit()
    connection.close()

    store = ReceiptStore(path)
    for label, value in (("num", 1), ("txt", 1), ("poison", {"n": 1}), ("poison", {"n": 2})):
        with pytest.raises(StoreError) as caught:
            store.submit(label, value)
        assert caught.value.code == "corrupt"
    assert store.submit("keep", {"n": 1}).code == "duplicate"
    assert store.submit("other", 2).code == "created"
    assert store.count() == 5

    gate = Intake(token=TOKEN, store=store, boot="cd" * 16)
    with pytest.raises(StoreError) as caught:
        gate.submit(b'{"id":"num","type":"e","payload":1}', TOKEN)
    assert caught.value.code == "corrupt"
    assert gate.submit(b'{"id":"brand","type":"e","payload":1}', TOKEN).ok
    store.close()

    nullable = tmp_path / "nullable.sqlite"
    connection = sqlite3.connect(nullable)
    connection.execute(
        "CREATE TABLE receipts (correlation_id TEXT PRIMARY KEY, canon BLOB, receipt TEXT NOT NULL)"
    )
    connection.commit()
    connection.close()
    with pytest.raises(StoreError) as caught:
        ReceiptStore(nullable)
    assert caught.value.code == "corrupt"
