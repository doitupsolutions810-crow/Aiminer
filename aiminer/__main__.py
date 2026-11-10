"""Run the named corpus without pytest: ``python -m aiminer``."""

from __future__ import annotations

from aiminer.canon import canonicalize
from aiminer.corpus import build_cases
from aiminer.errors import ScanError
from aiminer.limits import Limits
from aiminer.scan import parse_json


def main() -> int:
    failures: list[str] = []
    cases = build_cases()
    for item in cases:
        limits = Limits(**item["limits"]) if item["limits"] else Limits()
        try:
            value = parse_json(item["text"], limits)
        except ScanError as exc:
            if item["code"] != exc.code:
                failures.append(f"{item['name']}: got {exc.code}")
            continue
        except Exception as exc:  # pragma: no cover - surfaced as a corpus failure
            failures.append(f"{item['name']}: {type(exc).__name__}")
            continue
        if item["code"] is not None:
            failures.append(f"{item['name']}: expected {item['code']}")
            continue
        actual = canonicalize(value, limits)
        if actual != item["canon"]:
            failures.append(f"{item['name']}: canon {actual!r}")
            continue
        if canonicalize(parse_json(actual, limits), limits) != actual:
            failures.append(f"{item['name']}: not fixed point")
    if failures:
        print("\n".join(failures[:20]))
        print(f"aiminer: {len(failures)} corpus failure(s)")
        return 1
    print(f"aiminer: {len(cases)} corpus cases ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
