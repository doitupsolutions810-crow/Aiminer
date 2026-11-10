# Aiminer

Aiminer is a fail-closed intake miner for one untrusted JSON envelope. It is a library with a long edge-case suite, not an HTTP service.

A body is accepted only when every step below holds. The first failure is the decision code.

1. The body is `bytes` or `bytearray`.
2. Its length is at most `max_bytes` (default 4096). Size is decided before the token is read, and `boot` is not returned for an oversized body.
3. The token matches exactly (`hmac.compare_digest`). It is not trimmed and not Unicode-normalized. A wrong token hides `boot`, including when the bytes are not UTF-8.
4. The bytes are UTF-8, do not start with a BOM, and do not contain a raw NUL.
5. They are one JSON value: no duplicate keys, no trailing junk, no binary floats, and no non-finite numbers such as the `1e309` that `json.loads` turns into infinity. Only ASCII digits are numbers. Fullwidth, Arabic-Indic, and superscript digits are not consumed, so `1２` is not `12` and `١` is not `1`.
6. The value is an object with exactly `id`, `type`, and `payload`.
7. `id` is 1..128 code points, NFC, and free of control, format, and whitespace characters. `/`, `\`, `.`, and `..` are rejected. Compatibility lookalikes are not folded.
8. `type` matches `^[a-z0-9][a-z0-9._:-]{0,63}$`.

The same id and the same canonical value return the original receipt (`duplicate`). The same id with a different canonical value is `conflict` and does not replace the first row. Spacing, object key order, and equivalent numbers (`1`, `1.0`, `1e0`) share a receipt. `-0` is `0`. A number is never rounded to fit a precision; too many significant digits is `number_limit`.

An intake without a store, and each `ReceiptStore`, keeps at most `max_entries` distinct ids (default 4096). A new id past that cap is `too_many_entries`. Replays and conflicts of ids already stored still work and do not free a slot. The ledger table must be the primary-key schema this library creates; a same-named table without that key, with columns in another order, with an extra column, or with a nullable canon or receipt is corrupt. A stored canon that is not a blob, or a receipt that is not the sha256 of that blob, is corrupt. It is not reported as a conflict, and the failed read does not leave the write transaction open. A path containing NUL, or a path that is a directory, is rejected.

Bidirectional controls are rejected in every string (`format_char`): overrides, isolates, and the left-to-right, right-to-left, and Arabic letter marks. A zero-width joiner may appear in a payload and is still illegal in an id. Line and paragraph separators are allowed in strings and escaped in the canonical form. That form, and the plain spelling of a number, cannot exceed `max_chars`. `1e20` is `number_limit` when the spelling would be longer than the cap. A document that fits on the way in and only grows past the cap while it is canonicalized is `too_large`.

Canonical object keys are NFC-normalized and ordered by UTF-16 code units. A composed character and its decomposed pair collapse. If that collapse makes two keys equal, the value is rejected. `max_string` is that normalized length: U+FB2C is three characters after NFC, and the four-code-point decomposition of U+1F82 still fits in a cap of one. A raw surrogate inside a string is `lone_surrogate`. `+1`, a Unicode minus, `1_000`, and `1,2` are not numbers. Nested containers are capped (default depth 32, hard ceiling 64) so a deep value returns `depth` instead of `RecursionError`. Digit runs past the literal cap stop being read. A literal inside the cap is kept exactly; it is not rounded to 28 digits.

```python
from aiminer import Intake

gate = Intake(token="lab-token")
decision = gate.submit(
    b'{"id":"job-1","type":"event","payload":{"n":1.0}}',
    "lab-token",
)
replay = gate.submit(
    b'{ "type" : "event" , "payload" : { "n" : 1 }, "id" : "job-1" }',
    "lab-token",
)
assert decision.ok and replay.duplicate and replay.receipt == decision.receipt
```

A `ReceiptStore` keeps that replay across a new `Intake` and a new `boot`. `boot` is 32 lowercase hex characters and stays constant for one instance.

## Tests

```bash
python -m aiminer
python -m pytest
```

The suite covers a named corpus, every ASCII singleton, a dangerous two-character matrix, exponent and depth caps, canonical width, bidirectional controls, UTF-16 key order, NFC collisions, surrogate pairs, auth-order precedence, exact token bytes, homoglyphs, ledger corruption, and a deterministic fuzz seeded with `0x41494D52`. Concurrent callers share one winner for one correlation id.
