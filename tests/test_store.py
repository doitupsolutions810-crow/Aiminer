"""Ledger durability, corruption, and label precedence."""

from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal

import pytest

from aiminer import Limits, ReceiptStore, StoreError, canonicalize


def test_replay_conflict_and_shared_receipt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "ledger.sqlite"
    store = ReceiptStore(path)
    mode = sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()
    assert mode[0] == "wal"
    first = store.submit("job", {"n": Decimal("1.0")})
    assert first.ok and first.code == "created"
    again = store.submit("job", {"n": Decimal("1")})
    assert again.code == "duplicate" and again.receipt == first.receipt
    other = store.submit("other", {"n": Decimal("1")})
    assert other.code == "created" and other.receipt == first.receipt
    clash = store.submit("job", {"n": Decimal("2")})
    assert clash.code == "conflict" and clash.receipt is None
    assert store.count() == 2
    assert store.submit("job", {"n": 1}).receipt == first.receipt
    folded = store.submit("accent", "e\u0301")
    assert store.submit("accent", "\u00e9").code == "duplicate"
    assert folded.receipt == store.submit("accent", "é").receipt


def test_label_precedence_and_sql_text(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = ReceiptStore(tmp_path / "ledger.sqlite")
    assert store.submit(None, 1).code == "correlation_type"
    assert store.submit(1, 1).code == "correlation_type"
    assert store.submit(True, 1).code == "correlation_type"
    assert store.submit(b"a", 1).code == "correlation_type"
    assert store.submit("", 1).code == "correlation_empty"
    assert store.submit(" ", 1).code == "correlation_chars"
    assert store.submit("a/b", 1).code == "correlation_chars"
    assert store.submit(".", 1).code == "correlation_reserved"
    assert store.submit("..", 1).code == "correlation_reserved"
    assert store.submit("e\u0301", 1).code == "correlation_form"
    assert store.submit("e" * 129, 1).code == "correlation_length"
    nasty = "a';DROP;TABLE;receipts;--"
    assert store.submit(nasty, {"ok": True}).code == "created"
    assert store.submit("still-here", 1).code == "created"
    assert store.count() == 2
    assert store.submit(1.5, 1).code == "correlation_type"  # type: ignore[arg-type]


def test_value_rejections_do_not_write(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = ReceiptStore(tmp_path / "ledger.sqlite")
    assert store.submit("f", 1.2).code == "float"
    loop: list[object] = []
    loop.append(loop)
    assert store.submit("c", loop).code == "cycle"
    shared = {"n": 1}
    assert store.submit("d", {"a": shared, "b": shared}).code == "cycle"
    assert store.submit("ok", {"a": {}, "b": {}}).code == "created"
    assert store.submit("s", "\ud800").code == "lone_surrogate"
    assert store.submit("t", ("a",)).code == "type"
    assert store.count() == 1
    with pytest.raises(ValueError):
        store.submit("n", 1, object())  # type: ignore[arg-type]


def test_corrupt_path_and_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    garbage = tmp_path / "bad.sqlite"
    garbage.write_bytes(b"this is not a database")
    with pytest.raises(StoreError) as caught:
        ReceiptStore(garbage)
    assert caught.value.code == "corrupt"
    wrong = tmp_path / "wrong.sqlite"
    setup = sqlite3.connect(wrong)
    setup.execute("CREATE TABLE receipts (a TEXT)")
    setup.commit()
    setup.close()
    with pytest.raises(StoreError) as caught:
        ReceiptStore(wrong)
    assert caught.value.code == "corrupt"
    with pytest.raises(StoreError) as caught:
        ReceiptStore(tmp_path)
    assert caught.value.code == "path"
    with pytest.raises(StoreError) as caught:
        ReceiptStore(tmp_path / "missing" / "ledger.sqlite")
    assert caught.value.code == "path"
    with pytest.raises(ValueError):
        ReceiptStore("")
    with pytest.raises(ValueError):
        ReceiptStore(1)  # type: ignore[arg-type]
    memory = ReceiptStore(":memory:")
    assert memory.submit("m", 1).code == "created"
    path = tmp_path / "live.sqlite"
    store = ReceiptStore(path)
    created = store.submit("live", [1, Decimal("1.0")])
    store.close()
    store.close()
    with pytest.raises(StoreError) as caught:
        store.submit("live", 1)
    assert caught.value.code == "closed"
    with pytest.raises(StoreError) as caught:
        store.count()
    assert caught.value.code == "closed"
    revived = ReceiptStore(path)
    assert revived.submit("live", [1, 1]).code == "duplicate"
    assert revived.submit("live", [1, 1]).receipt == created.receipt
    with ReceiptStore(tmp_path / "ctx.sqlite") as ctx:
        assert ctx.submit("c", None).code == "created"
    with pytest.raises(StoreError):
        ctx.count()


def test_concurrent_same_and_distinct(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = ReceiptStore(tmp_path / "ledger.sqlite")
    barrier = threading.Barrier(32)
    results: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()

    def same() -> None:
        try:
            barrier.wait(5)
            result = store.submit("same", {"n": 1})
            with lock:
                results.append(result.code + ":" + str(result.receipt))
        except Exception as exc:
            with lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=same) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert errors == []
    assert sum(item.startswith("created:") for item in results) == 1
    assert sum(item.startswith("duplicate:") for item in results) == 31
    assert len(set(results)) == 2

    barrier = threading.Barrier(20)
    distinct_errors: list[str] = []

    def distinct(index: int) -> None:
        try:
            barrier.wait(5)
            for step in range(5):
                result = store.submit(f"id-{index}-{step}", {"n": index, "j": step})
                if result.code != "created":
                    distinct_errors.append(result.code)
        except Exception as exc:
            distinct_errors.append(repr(exc))

    group = [threading.Thread(target=distinct, args=(index,)) for index in range(20)]
    for thread in group:
        thread.start()
    for thread in group:
        thread.join(5)
        assert not thread.is_alive()
    assert distinct_errors == []
    assert store.count() == 1 + 100


def test_concurrent_conflict_has_one_winner(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = ReceiptStore(tmp_path / "ledger.sqlite")
    barrier = threading.Barrier(16)
    codes: list[tuple[str, str | None, int]] = []
    lock = threading.Lock()

    def work(payload: int) -> None:
        barrier.wait(5)
        result = store.submit("race", {"n": payload})
        with lock:
            codes.append((result.code, result.receipt, payload))

    threads = [threading.Thread(target=work, args=(index % 2,)) for index in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    created = [item for item in codes if item[0] == "created"]
    assert len(created) == 1
    winner_payload = created[0][2]
    winner_receipt = created[0][1]
    for code, receipt, payload in codes:
        if code == "created":
            continue
        if payload == winner_payload:
            assert code == "duplicate" and receipt == winner_receipt
        else:
            assert code == "conflict" and receipt is None
    assert store.count() == 1
    assert store.submit("race", {"n": winner_payload}).code == "duplicate"
    assert store.submit("race", {"n": 1 - winner_payload}).code == "conflict"
    assert canonicalize({"n": winner_payload})
