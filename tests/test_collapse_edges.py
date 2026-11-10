"""NFC key collisions must not be relabeled as a size cap or a truncated value.

A composed key and its decomposed pair are ``key_collapse`` before the value
is read, including when the object is already at ``max_keys``. A raw repeat
stays ``duplicate_key``. Exponent padding does not create a second number.
"""

from __future__ import annotations

import pytest

from aiminer import (
    SCAN_CODES,
    CanonError,
    Intake,
    Limits,
    ReceiptStore,
    ScanError,
    canonicalize,
    parse_json,
)

TOKEN = "collapse-edges"


class Probe(str):
    reads = 0

    def __getitem__(self, item: object) -> str:
        if isinstance(item, int):
            Probe.reads += 1
        return str.__getitem__(self, item)


def test_nfc_collision_beats_the_cap_and_a_missing_value() -> None:
    assert "key_collapse" in SCAN_CODES
    with pytest.raises(ScanError) as caught:
        parse_json('{"é":1,"e\\u0301":2}', Limits(max_keys=1))
    assert caught.value.code == "key_collapse"
    with pytest.raises(ScanError) as caught:
        parse_json('{"a":1,"a":', Limits(max_keys=1))
    assert caught.value.code == "duplicate_key"
    with pytest.raises(ScanError) as caught:
        parse_json('{"a":1,"b":2}', Limits(max_keys=1))
    assert caught.value.code == "too_many_keys"
    with pytest.raises(CanonError) as caught:
        canonicalize({"b": 1, "é": 2, "e\u0301": 3}, Limits(max_keys=2))
    assert caught.value.code == "key_collapse"
    with pytest.raises(CanonError) as caught:
        canonicalize({"a": 1, "b": 2}, Limits(max_keys=1))
    assert caught.value.code == "too_many_keys"

    Probe.reads = 0
    blob = Probe('{"é":1,"e\\u0301":' + ("[" * 100_000))
    with pytest.raises(ScanError) as caught:
        parse_json(blob, Limits(max_depth=64, max_chars=200_000))
    assert caught.value.code == "key_collapse"
    assert 0 < Probe.reads < 200


def test_exponent_padding_shares_one_value_and_the_sign_stays() -> None:
    assert canonicalize(parse_json("1e01")) == "10"
    assert canonicalize(parse_json("1e+01")) == "10"
    assert canonicalize(parse_json("0e00")) == "0"
    assert canonicalize(-(10**20)) == "-" + "1" + "0" * 20
    assert canonicalize(10**20) == "1" + "0" * 20
    gate = Intake(token=TOKEN, boot="ab" * 16)
    first = gate.submit(b'{"id":"n","type":"e","payload":1e01}', TOKEN)
    second = gate.submit(b'{"id":"n","type":"e","payload":10}', TOKEN)
    assert first.ok and second.duplicate and second.receipt == first.receipt
    zero = gate.submit(b'{"id":"z","type":"e","payload":0e00}', TOKEN)
    again = gate.submit(b'{"id":"z","type":"e","payload":0}', TOKEN)
    assert zero.ok and again.duplicate and again.receipt == zero.receipt
    assert gate.submit(b'{"id":"n","type":"e","payload":1e 2}', TOKEN).code == "invalid_number"


def test_token_is_not_normalized_and_id_length_is_code_points() -> None:
    gate = Intake(token="é", boot="cd" * 16)
    body = b'{"id":"j","type":"e","payload":1}'
    denied = gate.submit(body, "e\u0301")
    assert denied.code == "unauthorized" and denied.boot is None
    assert gate.submit(body, "é").ok
    emoji = "😀"
    wide = Intake(token=TOKEN, max_bytes=8000, boot="ef" * 16)
    accepted = ('{"id":"%s","type":"e","payload":1}' % (emoji * 128)).encode()
    rejected = ('{"id":"%s","type":"e","payload":1}' % (emoji * 129)).encode()
    assert wide.submit(accepted, TOKEN).ok
    assert wide.submit(rejected, TOKEN).code == "id_length"


def test_depth_boundary_cycle_cap_and_line_separator_replay(tmp_path) -> None:  # type: ignore[no-untyped-def]
    nested = "[" * 32 + "1" + "]" * 32
    parsed = parse_json(nested)
    assert canonicalize(parsed) == nested
    with pytest.raises(ScanError) as caught:
        parse_json("[" * 33 + "1" + "]" * 33)
    assert caught.value.code == "depth"
    shared: list[object] = []
    with pytest.raises(CanonError) as caught:
        canonicalize([shared, shared], Limits(max_items=1))
    assert caught.value.code == "too_many_items"
    with pytest.raises(CanonError) as caught:
        canonicalize([shared, shared])
    assert caught.value.code == "cycle"

    path = tmp_path / "ledger.sqlite"
    store = ReceiptStore(path)
    created = store.submit("s", "\u2028")
    assert created.code == "created"
    assert store.submit("s", "\u2028").code == "duplicate"
    store.close()
    reopened = ReceiptStore(path)
    assert reopened.submit("s", "\u2028").receipt == created.receipt
    reopened.close()
