"""Hard bounds. Bool is rejected so ``True`` cannot sneak in as ``1``."""

from __future__ import annotations

from dataclasses import dataclass


DEPTH_CEILING = 64


def _bound(name: str, value: object, lo: int, hi: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(name)
    if value < lo or value > hi:
        raise ValueError(name)


@dataclass(frozen=True)
class Limits:
    """Caps for one parse or one canonical write.

    ``max_depth`` is the maximum number of nested containers.
    Scalars are allowed when it is ``0``. A root array is not.
    """

    max_depth: int = 32
    max_string: int = 4096
    max_keys: int = 64
    max_items: int = 64
    max_nodes: int = 256
    max_number_literal: int = 64
    max_exponent: int = 20
    max_chars: int = 65536

    def __post_init__(self) -> None:
        _bound("max_depth", self.max_depth, 0, DEPTH_CEILING)
        _bound("max_string", self.max_string, 0, 1_000_000)
        _bound("max_keys", self.max_keys, 0, 100_000)
        _bound("max_items", self.max_items, 0, 100_000)
        _bound("max_nodes", self.max_nodes, 1, 1_000_000)
        _bound("max_number_literal", self.max_number_literal, 1, 10_000)
        _bound("max_exponent", self.max_exponent, 0, 100_000)
        _bound("max_chars", self.max_chars, 1, 5_000_000)
