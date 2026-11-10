# Aiminer

Aiminer is a fail-closed intake miner for one untrusted JSON envelope. It is a library with a long edge-case suite, not an HTTP service.

A body is accepted only when every step below holds. The first failure is the decision code.

1. The body is `bytes` or `bytearray`.
2. Its length is at most `max_bytes` (default 4096). Size is decided before the token is read, and `boot` is not returned for an oversized body.
3. The token matches exactly (`hmac.compare_digest`). It is not trimmed and not Unicode-normalized. A wrong token hides `boot`, including when the bytes are not UTF-8.
4. The bytes are UTF-8, do not start with a BOM, and do not contain a raw NUL.
5. They are one JSON value: no duplicate keys, no trailing junk, no binary floats, and no non-finite numbers such as the `1e309` that `json.loads` turns into infinity.
6. The value is an object with exactly `id`, `type`, and `payload`.
7. `id` is 1..128 code points, NFC, and free of control, format, and whitespace characters. `/`, `\`, `.`, and `..` are rejected. Compatibility lookalikes are not folded.
8. `type` matches `^[a-z0-9][a-z0-9._:-]{0,63}$`.

The same id and the same canonical value return the original receipt (`duplicate`). The same id with a different canonical value is `conflict` and does not replace the first row. Spacing, object key order, and equivalent numbers (`1`, `1.0`, `1e0`) share a receipt. `-0` is `0`.

Canonical object keys are NFC-normalized and ordered by UTF-16 code units. A composed character and its decomposed pair collapse. If that collapse makes two keys equal, the value is rejected. Nested containers are capped (default depth 32, hard ceiling 64) so a deep value returns `depth` instead of `RecursionError`. Digit runs past the literal cap stop being read. A literal inside the cap is kept exactly; it is not rounded to 28 digits.

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

The suite covers a named corpus, every ASCII singleton, a dangerous two-character matrix, exponent and depth caps, UTF-16 key order, NFC collisions, surrogate pairs, auth-order precedence, exact token bytes, homoglyphs, ledger corruption, and a deterministic fuzz seeded with `0x41494D52`. Concurrent callers share one winner for one correlation id.
