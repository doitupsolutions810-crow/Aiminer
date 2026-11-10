"""Deterministic fuzz and a stateful soak. Seed 0x41494D52 reproduces failures."""

from __future__ import annotations

import threading

from aiminer import INTAKE_RESULT_CODES, Intake, ReceiptStore

SEED = 0x41494D52
TOKEN = "soak-token"
BASE = b'{"id":"edge","type":"event","payload":{"n":1,"s":"ok","flag":true}}'


def _lcg(seed: int):
    value = seed & 0x7FFFFFFF
    while True:
        value = (1103515245 * value + 12345) & 0x7FFFFFFF
        yield value


def _mutate(raw: bytes, rnd) -> bytes:
    buf = bytearray(raw)
    op = next(rnd) % 5
    if op == 0 and buf:
        buf[next(rnd) % len(buf)] = next(rnd) % 256
    elif op == 1:
        buf.insert(next(rnd) % (len(buf) + 1), next(rnd) % 256)
    elif op == 2 and buf:
        del buf[next(rnd) % len(buf)]
    elif op == 3 and buf:
        start = next(rnd) % len(buf)
        buf.extend(buf[start : start + 1 + (next(rnd) % 4)])
    else:
        buf = bytearray(b"\x00" * (4000 + (next(rnd) % 8))) + buf
    return bytes(buf)


def test_fuzz_never_escapes_and_obeys_the_auth_ladder() -> None:
    rnd = _lcg(SEED)
    failures: list[str] = []
    for index in range(800):
        body = _mutate(BASE, rnd)
        try:
            if next(rnd) % 5 == 0:
                denied = Intake(token=TOKEN).submit(body, "wrong")
                if denied.code != ("too_large" if len(body) > 4096 else "unauthorized") or denied.boot is not None:
                    failures.append(f"{index}:denied:{denied.code}")
                    continue
            gate = Intake(token=TOKEN, boot="aa" * 16)
            decision = gate.submit(body, TOKEN)
        except Exception as exc:
            failures.append(f"{index}:{type(exc).__name__}:{exc}")
            continue
        if decision.code not in INTAKE_RESULT_CODES or not isinstance(decision.ok, bool):
            failures.append(f"{index}:code:{decision.code}")
            continue
        if len(body) > 4096:
            if decision.code != "too_large" or decision.boot is not None or decision.ok:
                failures.append(f"{index}:size:{decision.code}")
            continue
        if decision.boot != gate.boot:
            failures.append(f"{index}:boot")
            continue
        if decision.ok:
            fresh = Intake(token=TOKEN, boot="bb" * 16).submit(body, TOKEN)
            if (not fresh.ok) or fresh.canonical != decision.canonical or decision.code != "accepted":
                failures.append(f"{index}:unstable:{decision.code}")
            elif decision.duplicate or decision.receipt is None:
                failures.append(f"{index}:shape")
        elif decision.receipt is not None or decision.canonical is not None or decision.duplicate:
            failures.append(f"{index}:leak")
    assert failures == [], failures[:8]


def test_stateful_soak_replays_and_conflicts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = ReceiptStore(tmp_path / "soak.sqlite")
    gate = Intake(token=TOKEN, store=store, boot="cc" * 16)
    receipts: list[str] = []
    for index in range(300):
        body = (
            '{"id":"soak-%d","type":"event.item","payload":{"n":%d,"text":"%s"}}'
            % (index, index, "x" * (index % 17))
        ).encode()
        decision = gate.submit(body, TOKEN)
        assert decision.code == "accepted"
        receipts.append(decision.receipt or "")
        assert gate.submit(body, "nope").code == "unauthorized"
    assert store.count() == 300
    assert len(set(receipts)) == 300
    for index in range(300):
        # Equivalent number and key order, plus insignificant space.
        body = (
            '{ "payload": { "text": "%s", "n": %d.0 }, "id": "soak-%d", "type": "event.item" }'
            % ("x" * (index % 17), index, index)
        ).encode()
        decision = gate.submit(body, TOKEN)
        assert decision.duplicate and decision.receipt == receipts[index]
    for index in range(0, 300, 7):
        body = (
            '{"id":"soak-%d","type":"event.item","payload":{"n":%d,"text":"changed"}}'
            % (index, index)
        ).encode()
        assert gate.submit(body, TOKEN).code == "conflict"
        original = (
            '{"id":"soak-%d","type":"event.item","payload":{"n":%d,"text":"%s"}}'
            % (index, index, "x" * (index % 17))
        ).encode()
        assert gate.submit(original, TOKEN).receipt == receipts[index]
    assert store.count() == 300

    # A second process-shaped open sees the same rows.
    revived = Intake(token=TOKEN, store=ReceiptStore(tmp_path / "soak.sqlite"), boot="dd" * 16)
    assert revived.submit(
        b'{"id":"soak-0","type":"event.item","payload":{"n":0,"text":""}}',
        TOKEN,
    ).duplicate


def test_threaded_intake_single_winner(tmp_path) -> None:  # type: ignore[no-untyped-def]
    gate = Intake(token=TOKEN, store=ReceiptStore(tmp_path / "race.sqlite"), boot="ee" * 16)
    body_a = b'{"id":"only","type":"event","payload":{"n":1.0}}'
    body_b = b'{"id":"only","type":"event","payload":{"n":2}}'
    barrier = threading.Barrier(24)
    outcomes: list[tuple[str, str | None]] = []
    lock = threading.Lock()

    def work(body: bytes) -> None:
        barrier.wait(5)
        decision = gate.submit(body, TOKEN)
        with lock:
            outcomes.append((decision.code, decision.receipt))

    threads = [threading.Thread(target=work, args=(body_a if index % 2 == 0 else body_b,)) for index in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    created = [item for item in outcomes if item[0] == "accepted"]
    assert len(created) == 1
    winner = created[0][1]
    for code, receipt in outcomes:
        if code == "accepted":
            assert receipt == winner
        elif code == "duplicate":
            assert receipt == winner
        else:
            assert code == "conflict" and receipt is None
    assert sum(1 for code, _ in outcomes if code == "duplicate") + 1 == sum(
        1 for code, receipt in outcomes if receipt == winner
    )
