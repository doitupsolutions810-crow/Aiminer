"""Edges the first suite accepted or mislabeled.

Unicode digits must not become ASCII numbers. Tight limits must not round a
value into a different one. Caps, surrogates, and corrupt ledgers fail closed.
"""

from __future__ import annotations

import sqlite3
import threading
from decimal import Context, Decimal, localcontext
from pathlib import Path

import pytest

from aiminer import (
    INTAKE_RESULT_CODES,
    STORE_RESULT_CODES,
    CanonError,
    Intake,
    Limits,
    ReceiptStore,
    ScanError,
    StoreError,
    canonicalize,
    parse_json,
)

TOKEN = "more-edges"


class Probe(str):
    reads = 0

    def __getitem__(self, item: object) -> str:
        if isinstance(item, int):
            Probe.reads += 1
        return str.__getitem__(self, item)


def _reads(text: str, limits: Limits) -> tuple[str, int]:
    Probe.reads = 0
    try:
        parse_json(Probe(text), limits)
    except ScanError as exc:
        return exc.code, Probe.reads
    raise AssertionError("expected ScanError")


def test_unicode_digits_do_not_alias_ascii_numbers() -> None:
    rejected = {
        "２": "unexpected",
        "０": "unexpected",
        "١": "unexpected",
        "²": "unexpected",
        "1２": "trailing_junk",
        "1١": "trailing_junk",
        "1²": "trailing_junk",
        "0２": "trailing_junk",
        "1e２": "invalid_number",
        "1e²": "invalid_number",
        "1e١": "invalid_number",
    }
    for text, code in rejected.items():
        with pytest.raises(ScanError) as caught:
            parse_json(text)
        assert caught.value.code == code, text
    assert canonicalize(parse_json("12")) == "12"
    assert canonicalize(parse_json('"２"')) == '"２"'
    assert canonicalize(parse_json('"١"')) == '"١"'


def test_unicode_digit_payload_does_not_consume_the_ascii_value() -> None:
    gate = Intake(token=TOKEN, boot="ab" * 16)
    fullwidth = '{"id":"n","type":"event","payload":1\uff12}'.encode()
    arabic = '{"id":"a","type":"event","payload":\u0661}'.encode()
    # Inside an object the leftover digit is not top-level junk.
    assert gate.submit(fullwidth, TOKEN).code == "unexpected"
    assert gate.submit(arabic, TOKEN).code == "unexpected"
    twelve = gate.submit(b'{"id":"n","type":"event","payload":12}', TOKEN)
    one = gate.submit(b'{"id":"a","type":"event","payload":1}', TOKEN)
    assert twelve.code == "accepted"
    assert one.code == "accepted"
    assert twelve.receipt != one.receipt
    text = gate.submit('{"id":"s","type":"event","payload":"\uff12"}'.encode(), TOKEN)
    assert text.code == "accepted"
    assert "\uff12" in (text.canonical or "")
    number = canonicalize({"id": "s", "type": "event", "payload": 2})
    assert text.canonical != number


def test_tight_limits_do_not_round_away_significant_digits() -> None:
    tight = Limits(max_number_literal=10, max_exponent=100)
    wide = Limits(max_number_literal=50, max_exponent=50, max_chars=80)
    with localcontext(Context(prec=50)):
        forty_nines = Decimal("9" * 40)
        # Forty nines plus one is exactly 10**40, which has one significant digit.
        power = forty_nines + 1
    samples = [forty_nines, 10**50, 10**50 + 1, -(10**30 + 7), parse_json("9" * 40, wide)]
    for sample in samples:
        with pytest.raises(CanonError) as caught:
            canonicalize(sample, tight)
        assert caught.value.code == "number_limit"
    assert canonicalize(power, tight) == "1" + ("0" * 40)
    assert canonicalize(Decimal("12345"), tight) == "12345"
    assert canonicalize(10**20) == "1" + ("0" * 20)
    assert canonicalize(-(10**20)) == "-" + "1" + ("0" * 20)
    assert canonicalize(Decimal("-10.50")) == "-10.5"
    assert canonicalize(Decimal("1.2300")) == "1.23"
    assert canonicalize(Decimal("-0")) == "0"
    assert canonicalize(0) == "0"


def test_surrogate_keys_fail_closed() -> None:
    for value in ({"\ud800": 1}, {"a": 1, "\ud800": 2}, {"\ud800\udc00": 1}, ["\udfff"]):
        with pytest.raises(CanonError) as caught:
            canonicalize(value)
        assert caught.value.code == "lone_surrogate"


def test_shared_list_is_a_cycle() -> None:
    shared: list[int] = []
    with pytest.raises(CanonError) as caught:
        canonicalize([[shared], [shared]])
    assert caught.value.code == "cycle"
    assert canonicalize([[], []]) == "[[],[]]"


def test_array_cap_is_reported_before_the_tail_is_read() -> None:
    limits = Limits(max_items=1, max_depth=64, max_chars=200_000, max_nodes=1_000_000)
    code, reads = _reads("[1," + ("[" * 100_000), limits)
    assert code == "too_many_items"
    assert reads < 50
    code, reads = _reads('{"a":1,"b":' + ("[" * 100_000), Limits(max_keys=1, max_depth=64, max_chars=200_000))
    assert code == "too_many_keys"
    assert reads < 80
    with pytest.raises(ScanError) as caught:
        parse_json("[1,", Limits(max_items=1))
    assert caught.value.code == "truncated"
    with pytest.raises(ScanError) as caught:
        parse_json("[1,]", Limits(max_items=1))
    assert caught.value.code == "trailing_comma"


def test_exponent_unicode_digits_stop_immediately() -> None:
    code, reads = _reads("1e" + ("２" * 100_000), Limits(max_chars=200_000))
    assert code == "invalid_number"
    assert 0 < reads < 40


def test_surrogate_pair_counts_as_one_character() -> None:
    limits = Limits(max_string=1)
    assert parse_json(r'"\uD83D\uDE00"', limits) == "😀"
    with pytest.raises(ScanError) as caught:
        parse_json(r'"\uD83D\uDE00\uD83D\uDE00"', limits)
    assert caught.value.code == "string_too_long"


def test_depth_boundary_round_trips() -> None:
    nested = "[" * 32 + "1" + "]" * 32
    value = parse_json(nested)
    rendered = canonicalize(value)
    assert rendered == nested
    assert canonicalize(parse_json(rendered)) == rendered
    mixed = '{"a":' + ("[" * 31) + "1" + ("]" * 31) + "}"
    mixed_value = parse_json(mixed)
    mixed_canon = canonicalize(mixed_value)
    assert canonicalize(parse_json(mixed_canon)) == mixed_canon


def test_nested_number_forms_replay_as_one_receipt() -> None:
    gate = Intake(token=TOKEN, boot="cd" * 16)
    first = (
        '{"id":"deep","type":"event","payload":{"outer":{"n":[1.0,{"m":1e2}]}}}'
    ).encode()
    second = (
        '{ "payload" : { "outer" : { "n" : [ 1 , { "m" : 100 } ] } } , '
        '"type" : "event" , "id" : "deep" }'
    ).encode()
    opened = gate.submit(first, TOKEN)
    replay = gate.submit(second, TOKEN)
    assert opened.code == "accepted"
    assert replay.duplicate and replay.receipt == opened.receipt
    changed = b'{"id":"deep","type":"event","payload":{"outer":{"n":[1,{"m":101}]}}}'
    assert gate.submit(changed, TOKEN).code == "conflict"
    assert gate.submit(second, TOKEN).receipt == opened.receipt


def test_more_label_and_utf8_edges() -> None:
    gate = Intake(token=TOKEN, max_bytes=8000, limits=Limits(max_string=400, max_chars=8000))

    def code(body: bytes) -> str:
        return gate.submit(body, TOKEN).code

    assert code('{"id":"a\\u3000b","type":"event","payload":1}'.encode()) == "id_chars"
    assert code('{"id":"a\\u00adb","type":"event","payload":1}'.encode()) == "id_chars"
    assert code('{"id":"\\ufffe","type":"event","payload":1}'.encode()) == "id_chars"
    assert code('{"id":"ok","type":"\\u0435vent","payload":1}'.encode()) == "type_form"
    kept = gate.submit(b'{"id":"fffe","type":"event","payload":"\\ufffe"}', TOKEN)
    assert kept.code == "accepted"
    assert "\ufffe" in (kept.canonical or "")
    overlong = [
        b"\xc0\x80",
        b"\xe0\x80\xaf",
        b"\xf0\x80\x80\x80",
        b"\xed\xa0\x80",
        b"\xff",
        b"\xc2",
    ]
    for raw in overlong:
        assert gate.submit(raw, "nope").code == "unauthorized"
        assert gate.submit(raw, TOKEN).code == "utf8"
        assert gate.submit(raw, TOKEN).boot == gate.boot


def test_entry_cap_keeps_replays_and_conflicts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        Intake(token=TOKEN, max_entries=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Intake(token=TOKEN, max_entries=-1)
    with pytest.raises(ValueError):
        ReceiptStore(":memory:", max_entries=False)  # type: ignore[arg-type]

    memory = Intake(token=TOKEN, max_entries=2, boot="11" * 16)
    assert memory.submit(b'{"id":"a","type":"event","payload":1}', TOKEN).code == "accepted"
    assert memory.submit(b'{"id":"b","type":"event","payload":1}', TOKEN).code == "accepted"
    assert memory.submit(b'{"id":"c","type":"event","payload":1}', TOKEN).code == "too_many_entries"
    assert memory.submit(b'{"id":"a","type":"event","payload":1.0}', TOKEN).duplicate
    clash = memory.submit(b'{"id":"a","type":"event","payload":9}', TOKEN)
    assert clash.code == "conflict" and clash.receipt is None
    assert memory.submit(b'{"id":"c","type":"event","payload":1}', TOKEN).code == "too_many_entries"

    store = ReceiptStore(tmp_path / "cap.sqlite", max_entries=1)
    assert store.submit("a", 1).code == "created"
    assert store.submit("a", 2).code == "conflict"
    assert store.submit("b", 1).code == "too_many_entries"
    assert store.submit("a", 1).code == "duplicate"
    assert store.count() == 1
    empty = ReceiptStore(":memory:", max_entries=0)
    assert empty.submit("a", 1).code == "too_many_entries"
    assert empty.count() == 0
    assert "too_many_entries" in INTAKE_RESULT_CODES
    assert "too_many_entries" in STORE_RESULT_CODES


def test_threaded_cap_has_exactly_one_full_ledger(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = ReceiptStore(tmp_path / "race.sqlite", max_entries=5)
    barrier = threading.Barrier(20)
    outcomes: list[tuple[int, str]] = []
    lock = threading.Lock()

    def work(index: int) -> None:
        barrier.wait(5)
        result = store.submit(f"id-{index}", {"n": index})
        with lock:
            outcomes.append((index, result.code))

    threads = [threading.Thread(target=work, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    created = [index for index, code in outcomes if code == "created"]
    assert len(created) == 5
    assert all(code in {"created", "too_many_entries"} for _, code in outcomes)
    assert store.count() == 5
    assert store.submit(f"id-{created[0]}", {"n": created[0]}).code == "duplicate"
    assert store.submit("one-more", 1).code == "too_many_entries"
    assert store.count() == 5


def test_store_attached_to_intake_uses_the_store_cap(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = ReceiptStore(tmp_path / "led.sqlite", max_entries=1)
    gate = Intake(token=TOKEN, store=store, max_entries=100, boot="22" * 16)
    assert gate.submit(b'{"id":"a","type":"event","payload":1}', TOKEN).code == "accepted"
    assert gate.submit(b'{"id":"b","type":"event","payload":1}', TOKEN).code == "too_many_entries"
    replay = Intake(token=TOKEN, store=store, boot="33" * 16)
    assert replay.submit(b'{"id":"a","type":"event","payload":1e0}', TOKEN).duplicate


def test_nul_path_and_schema_fail_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(StoreError) as caught:
        ReceiptStore("a\x00b.sqlite")
    assert caught.value.code == "path"
    with pytest.raises(StoreError) as caught:
        ReceiptStore(Path("a\x00b.sqlite"))
    assert caught.value.code == "path"

    missing_key = tmp_path / "nokey.sqlite"
    connection = sqlite3.connect(missing_key)
    connection.execute("CREATE TABLE receipts (correlation_id TEXT, canon BLOB, receipt TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(StoreError) as caught:
        ReceiptStore(missing_key)
    assert caught.value.code == "corrupt"

    wrong_type = tmp_path / "type.sqlite"
    connection = sqlite3.connect(wrong_type)
    connection.execute(
        "CREATE TABLE receipts (correlation_id TEXT PRIMARY KEY, canon TEXT NOT NULL, receipt TEXT NOT NULL)"
    )
    connection.commit()
    connection.close()
    with pytest.raises(StoreError) as caught:
        ReceiptStore(wrong_type)
    assert caught.value.code == "corrupt"

    healthy = ReceiptStore(tmp_path / "ok.sqlite", max_entries=2)
    assert healthy.submit("row", {"k": Decimal("1.0")}).code == "created"
    assert healthy.submit("row", {"k": 1}).code == "duplicate"


def test_literal_unicode_suffix_is_not_part_of_the_literal() -> None:
    with pytest.raises(ScanError) as caught:
        parse_json("true\u0662")
    assert caught.value.code == "trailing_junk"
    with pytest.raises(ScanError) as caught:
        parse_json("[true\u0662]")
    assert caught.value.code == "unexpected"
    assert parse_json("true") is True
