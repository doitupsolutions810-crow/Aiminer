"""Edges that mislabeled a non-canonical blob, or treated lookalikes as numbers.

``1.0`` hashes to its own receipt and used to come back as a conflict. Unicode
spaces are not JSON whitespace. A fullwidth exponent letter is not ``100``.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3

import pytest

from decimal import Decimal

from aiminer import (
    CanonError,
    Intake,
    Limits,
    ReceiptStore,
    StoreError,
    canonicalize,
    parse_json,
)

TOKEN = "further-edges"


def _insert(path, correlation_id: str, blob: bytes, receipt: str | None = None) -> None:
    digest = hashlib.sha256(blob).hexdigest() if receipt is None else receipt
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO receipts(correlation_id, canon, receipt) VALUES (?, ?, ?)",
        (correlation_id, blob, digest),
    )
    connection.commit()
    connection.close()


def test_noncanonical_blob_is_corrupt_not_conflict(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "ledger.sqlite"
    store = ReceiptStore(path)
    store.close()
    wide = b"1" + b"0" * 20
    assert wide == canonicalize(parse_json("1e20")).encode()
    _insert(path, "plain", b"1.0")
    _insert(path, "order", b'{"b":1,"a":2}')
    _insert(path, "newline", b"1\n")
    _insert(path, "empty", b"")
    _insert(path, "slash", b'"\\/"')
    _insert(path, "binary", b"\xff")
    _insert(path, "good", b"1")
    _insert(path, "upper", b"1", hashlib.sha256(b"1").hexdigest().upper())
    _insert(path, "wide", wide)

    store = ReceiptStore(path)
    expectations = {
        "plain": "canon",
        "order": "canon",
        "newline": "canon",
        "empty": "canon",
        "slash": "canon",
        "binary": "canon",
        "upper": "receipt",
    }
    for label, detail in expectations.items():
        with pytest.raises(StoreError) as caught:
            store.submit(label, 1)
        assert caught.value.code == "corrupt"
        assert caught.value.detail == detail
    assert store.submit("good", 1).code == "duplicate"
    # The caller's tight cap must not make a previously canonical row corrupt.
    tight = Limits(max_chars=10, max_exponent=20)
    clash = store.submit("wide", 2, tight)
    assert clash.code == "conflict" and clash.receipt is None
    assert store.submit("fresh", 3).code == "created"
    store.close()


def test_symlink_file_replays_and_a_dangling_link_is_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "ledger.sqlite"
    store = ReceiptStore(target)
    assert store.submit("job", {"n": 1}).code == "created"
    store.close()
    linked = tmp_path / "linked.sqlite"
    os.symlink(target, linked)
    again = ReceiptStore(linked)
    assert again.submit("job", {"n": Decimal("1.0")}).code == "duplicate"
    again.close()
    dangling = tmp_path / "missing.sqlite"
    os.symlink(tmp_path / "no-such-dir" / "ledger.sqlite", dangling)
    with pytest.raises(StoreError) as caught:
        ReceiptStore(dangling)
    assert caught.value.code == "path"
    folder = tmp_path / "dir-link"
    os.symlink(tmp_path, folder)
    with pytest.raises(StoreError) as caught:
        ReceiptStore(folder)
    assert caught.value.code == "path"


def test_fullwidth_exponent_and_nbsp_do_not_become_a_value() -> None:
    gate = Intake(token=TOKEN, boot="ab" * 16)
    weird = '{"id":"n","type":"e","payload":1ｅ2}'.encode()
    assert gate.submit(weird, TOKEN).code == "unexpected"
    hundred = gate.submit(b'{"id":"n","type":"e","payload":100}', TOKEN)
    assert hundred.ok
    assert gate.submit(b'{"id":"n","type":"e","payload":1}', TOKEN).code == "conflict"
    spaced = '{"id":"s","type":"e","payload":1\u00a0}'.encode()
    assert gate.submit(spaced, TOKEN).code == "unexpected"
    assert gate.submit(b'{"id":"s","type":"e","payload":1}', TOKEN).ok
    dot = '{"id":"d","type":"e","payload":1．2}'.encode()
    assert gate.submit(dot, TOKEN).code == "unexpected"


def test_escaped_form_cannot_outgrow_max_chars() -> None:
    assert len(canonicalize("\u2028")) == 8
    assert canonicalize("\u2028", Limits(max_chars=8)) == '"\\u2028"'
    with pytest.raises(CanonError) as caught:
        canonicalize("\u2028" * 20, Limits(max_chars=50))
    assert caught.value.code == "too_large"
    with pytest.raises(CanonError) as caught:
        canonicalize("\u2028", Limits(max_chars=7))
    assert caught.value.code == "too_large"
    assert canonicalize('"', Limits(max_chars=4)) == '"\\""'
    with pytest.raises(CanonError) as caught:
        canonicalize('"' * 40, Limits(max_chars=50))
    assert caught.value.code == "too_large"


def test_two_handles_share_one_ledger(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "shared.sqlite"
    first = ReceiptStore(path)
    second = ReceiptStore(path)
    assert first.submit("x", 1).code == "created"
    assert second.submit("x", 1).code == "duplicate"
    assert second.submit("y", {"z": 2}).code == "created"
    assert first.submit("y", {"z": Decimal("2.0")}).code == "duplicate"
    assert first.count() == 2 and second.count() == 2
    first.close()
    second.close()
