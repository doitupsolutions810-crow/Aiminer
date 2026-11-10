"""Envelope, auth order, replay, and Unicode identity edges."""

from __future__ import annotations

import hashlib
import unicodedata

import pytest

from aiminer import Decision, Intake, Limits, ReceiptStore

TOKEN = "lab-token"
BODY = b'{"id":"job-1","type":"event","payload":{"n":1.0}}'
PRETTY = b'{ "type" : "event" , "payload" : { "n" : 1 }, "id" : "job-1" }'


def _gate(**kwargs: object) -> Intake:
    values = {"token": TOKEN}
    values.update(kwargs)
    return Intake(**values)  # type: ignore[arg-type]


def test_readme_replay_ignores_spacing_key_order_and_number_form() -> None:
    gate = _gate()
    first = gate.submit(BODY, TOKEN)
    second = gate.submit(PRETTY, TOKEN)
    assert first.ok and first.code == "accepted" and first.duplicate is False
    assert second.duplicate and second.receipt == first.receipt
    assert second.canonical == first.canonical
    assert hashlib.sha256(first.canonical.encode("utf-8")).hexdigest() == first.receipt
    assert first.boot == second.boot
    third = gate.submit(b'{"id":"job-1","type":"event","payload":{"n":1e0}}', TOKEN)
    assert third.duplicate and third.receipt == first.receipt


def test_rejection_order_is_stable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class Counting(Intake):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self.checks = 0

        def _token_ok(self, token: object) -> bool:
            self.checks += 1
            return super()._token_ok(token)

    store = ReceiptStore(tmp_path / "ledger.sqlite")
    tight = Counting(token=TOKEN, max_bytes=len(BODY), store=store, boot="ab" * 16)
    assert tight.submit("nope", "wrong").code == "body_type"  # type: ignore[arg-type]
    assert tight.checks == 0
    huge = b" " * (len(BODY) + 1)
    assert tight.submit(huge, "wrong").code == "too_large"
    assert tight.submit(huge, TOKEN).code == "too_large"
    assert tight.checks == 0
    assert tight.submit(huge, TOKEN).boot is None
    decision = tight.submit(BODY, TOKEN)
    assert decision.code == "accepted" and decision.boot == tight.boot
    assert tight.checks == 1
    assert store.count() == 1

    gate = Counting(token=TOKEN, max_bytes=4096, store=store, boot="cd" * 16)
    assert gate.submit(b"\xff\xfe", "wrong").code == "unauthorized"
    assert gate.submit(b"\xff\xfe", TOKEN).code == "utf8"
    assert gate.submit(b"\xc0\xaf", TOKEN).code == "utf8"
    assert gate.submit(b"\x80", TOKEN).code == "utf8"
    assert gate.submit(b"\xed\xa0\x80", TOKEN).code == "utf8"
    assert gate.submit(b"\xef\xbb\xbf" + BODY, TOKEN).code == "bom"
    assert gate.submit(BODY[:8] + b"\x00" + BODY[8:], TOKEN).code == "forbidden_char"
    assert gate.submit(b"[]", TOKEN).code == "envelope"
    assert gate.submit(b"null", TOKEN).code == "envelope"
    assert gate.submit(b'"x"', TOKEN).code == "envelope"
    assert gate.submit(b"1", TOKEN).code == "envelope"
    assert gate.submit(b"true", TOKEN).code == "envelope"
    assert store.count() == 1


def test_failed_auth_and_garbage_do_not_consume_the_id() -> None:
    gate = _gate()
    assert gate.submit(BODY, "wrong").code == "unauthorized"
    assert gate.submit(BODY, "wrong").boot is None
    assert gate.submit(b'{"id":"job-1","type":"event","payload":1}{"x":1}', TOKEN).code == "trailing_junk"
    assert gate.submit(b'{"id":"job-1","type":"event","payload":1,"x":1}', TOKEN).code == "unknown_field"
    assert gate.submit(b'{"id":"job-1","type":"event"}', TOKEN).code == "missing_field"
    assert gate.submit(b'{"id":"job-1","type":"event","payload":1,"boot":"nope"}', TOKEN).code == "unknown_field"
    first = gate.submit(BODY, TOKEN)
    assert first.code == "accepted"
    assert gate.submit(BODY, "secret ").code == "unauthorized"
    replay = gate.submit(BODY, TOKEN)
    assert replay.duplicate and replay.receipt == first.receipt


def test_token_is_exact_including_space_and_unicode_form() -> None:
    spaced = _gate(token="secret ")
    body = b'{"id":"s","type":"event","payload":1}'
    assert spaced.submit(body, "secret").code == "unauthorized"
    assert spaced.submit(body, "secret ").code == "accepted"
    assert spaced.submit(body, "secret\n").code == "unauthorized"
    composed = _gate(token="\u00e9")
    assert composed.submit(body, "e\u0301").code == "unauthorized"
    assert composed.submit(body, "\u00e9").code == "accepted"
    assert composed.submit(body, "\ud800").code == "unauthorized"
    assert _gate().submit(body, None).code == "unauthorized"  # type: ignore[arg-type]
    assert _gate().submit(body, 1).code == "unauthorized"  # type: ignore[arg-type]
    assert isinstance(_gate().submit(bytearray(body), TOKEN), Decision)


def test_constructor_edges() -> None:
    with pytest.raises(ValueError):
        Intake(token="")
    with pytest.raises(ValueError):
        Intake(token=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Intake(token="\ud800")
    for bad in (True, 0, -1, 5_000_001):
        with pytest.raises(ValueError):
            Intake(token="t", max_bytes=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Intake(token="t", boot="A" * 32)
    with pytest.raises(ValueError):
        Intake(token="t", boot="ab")
    gate = _gate()
    assert len(gate.boot) == 32
    assert gate.submit(BODY, TOKEN).boot == gate.boot
    assert _gate(boot="cd" * 16).boot == "cd" * 16


def test_id_and_type_edges() -> None:
    gate = Intake(token=TOKEN, max_bytes=8000, limits=Limits(max_string=400, max_chars=8000))

    def send(body: bytes) -> str:
        return gate.submit(body, TOKEN).code

    assert send(b'{"id":"","type":"event","payload":1}') == "id_empty"
    assert send(b'{"id":" ","type":"event","payload":1}') == "id_chars"
    assert send(b'{"id":"a b","type":"event","payload":1}') == "id_chars"
    assert send(b'{"id":"a\\u0000b","type":"event","payload":1}') == "id_chars"
    assert send(b'{"id":"a\\u200bb","type":"event","payload":1}') == "id_chars"
    assert send(b'{"id":"a\\u202eb","type":"event","payload":1}') == "id_chars"
    assert send(b'{"id":"a\\u00a0b","type":"event","payload":1}') == "id_chars"
    assert send(b'{"id":".","type":"event","payload":1}') == "id_reserved"
    assert send(b'{"id":"..","type":"event","payload":1}') == "id_reserved"
    assert send(b'{"id":"...","type":"event","payload":1}') == "accepted"
    assert send(b'{"id":"a/b","type":"event","payload":1}') == "id_chars"
    assert send(b'{"id":"a\\\\b","type":"event","payload":1}') == "id_chars"
    assert send(b'{"id":1,"type":"event","payload":1}') == "id_type"
    assert send(b'{"id":null,"type":"event","payload":1}') == "id_type"
    assert send(b'{"id":true,"type":"event","payload":1}') == "id_type"
    assert send(('{"id":"%s","type":"event","payload":1}' % ("e" * 128)).encode()) == "accepted"
    assert send(('{"id":"%s","type":"event","payload":1}' % ("e" * 129)).encode()) == "id_length"
    long_bad = ("e\u0301" * 64) + "e"
    assert len(long_bad) == 129
    assert send(('{"id":"%s","type":"event","payload":1}' % long_bad).encode()) == "id_length"
    form_bad = "e\u0301" * 64
    assert len(form_bad) == 128
    assert unicodedata.normalize("NFC", form_bad) != form_bad
    assert send(('{"id":"%s","type":"event","payload":1}' % form_bad).encode()) == "id_form"
    assert send('{"id":"e\\u0301","type":"event","payload":1}'.encode()) == "id_form"
    assert send('{"id":"\\u00e9","type":"event","payload":1}'.encode()) == "accepted"
    assert send(b'{"id":"\\u00e9","type":"Event","payload":1}') == "type_form"
    assert send(b'{"id":"t","type":"","payload":1}') == "type_form"
    assert send(b'{"id":"t2","type":1,"payload":1}') == "type_type"
    assert send(b'{"id":"t3","type":"event.type:ok-1","payload":null}') == "accepted"
    assert send(b'{"id":"t4","type":".event","payload":[]}') == "type_form"
    exact = "a" * 64
    assert send(('{"id":"t5","type":"%s","payload":true}' % exact).encode()) == "accepted"
    assert send(('{"id":"t6","type":"%s","payload":false}' % ("a" * 65)).encode()) == "type_form"
    accented = "é" * 128
    assert send(('{"id":"%s","type":"event","payload":1}' % accented).encode()) == "accepted"


def test_identity_is_not_compatibility_folded() -> None:
    gate = _gate()
    ligature = "\ufb01"
    assert unicodedata.normalize("NFC", ligature) == ligature
    assert unicodedata.normalize("NFKC", ligature) == "fi"
    assert gate.submit(('{"id":"%s","type":"event","payload":1}' % ligature).encode(), TOKEN).code == "accepted"
    other = gate.submit(b'{"id":"fi","type":"event","payload":2}', TOKEN)
    assert other.code == "accepted"
    ohm = "\u2126"
    assert unicodedata.normalize("NFC", ohm) == "\u03a9"
    assert gate.submit(('{"id":"%s","type":"event","payload":1}' % ohm).encode(), TOKEN).code == "id_form"
    fullwidth = gate.submit(
        '{"id":"\uff0f","type":"event","payload":1}'.encode(),
        TOKEN,
    )
    assert fullwidth.code == "accepted"
    assert gate.submit('{"id":"/","type":"event","payload":1}'.encode(), TOKEN).code == "id_chars"
    decomposed = gate.submit(b'{"id":"plain","type":"event","payload":"e\\u0301"}', TOKEN)
    composed = gate.submit(b'{"id":"plain","type":"event","payload":"\\u00e9"}', TOKEN)
    assert decomposed.code == "accepted"
    assert composed.duplicate and composed.receipt == decomposed.receipt
    collapse = gate.submit(b'{"id":"mix","type":"event","payload":{"\\u00e9":1,"e\\u0301":2}}', TOKEN)
    assert collapse.code == "key_collapse"
    nul = gate.submit(b'{"id":"nul","type":"event","payload":"\\u0000"}', TOKEN)
    assert nul.code == "accepted"
    assert "\\u0000" in nul.canonical


def test_conflict_preserves_the_first_and_hides_its_receipt() -> None:
    gate = _gate()
    original = b'{"id":"c","type":"event","payload":{"n":1}}'
    changed = b'{"id":"c","type":"event","payload":{"n":2}}'
    first = gate.submit(original, TOKEN)
    clash = gate.submit(changed, TOKEN)
    assert clash.code == "conflict" and clash.ok is False
    assert clash.receipt is None and clash.canonical is None and clash.boot == gate.boot
    replay = gate.submit(original, TOKEN)
    assert replay.duplicate and replay.receipt == first.receipt
    assert gate.submit(changed, "nope").code == "unauthorized"


def test_homoglyph_ids_do_not_collide() -> None:
    gate = _gate()
    latin = gate.submit('{"id":"a","type":"event","payload":1}'.encode(), TOKEN)
    cyrillic = gate.submit('{"id":"а","type":"event","payload":2}'.encode(), TOKEN)
    assert latin.code == "accepted"
    assert cyrillic.code == "accepted"
    assert latin.receipt != cyrillic.receipt


def test_store_and_memory_receipts_match_and_survive_a_new_boot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    memory = _gate(boot="ab" * 16).submit(BODY, TOKEN)
    store = ReceiptStore(tmp_path / "ledger.sqlite")
    first = Intake(token=TOKEN, store=store, boot="cd" * 16)
    disk = first.submit(BODY, TOKEN)
    assert disk.receipt == memory.receipt
    assert disk.canonical == memory.canonical
    assert disk.boot != memory.boot
    again = Intake(token=TOKEN, store=store, boot="ef" * 16)
    replay = again.submit(PRETTY, TOKEN)
    assert replay.duplicate and replay.receipt == disk.receipt
    assert replay.boot == "ef" * 16
    clash = again.submit(b'{"id":"job-1","type":"event","payload":2}', TOKEN)
    assert clash.code == "conflict"
    assert again.submit(BODY, TOKEN).duplicate


def test_byte_cap_padding_and_memoryview() -> None:
    core = b'{"id":"p","type":"event","payload":1}'
    gate = Intake(token=TOKEN, max_bytes=len(core) + 3, boot="12" * 16)
    assert gate.submit(b" " * 3 + core, TOKEN).code == "accepted"
    assert gate.submit(b" " * 4 + core, TOKEN).code == "too_large"
    assert gate.submit(memoryview(core), TOKEN).code == "body_type"  # type: ignore[arg-type]
