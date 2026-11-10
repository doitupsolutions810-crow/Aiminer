"""Label rules shared by event ids and correlation ids.

Precedence, first match wins: type, empty, length, chars, form, reserved.
Length is counted in Unicode code points. Compatibility folding (NFKC) is
not applied, so lookalike characters stay distinct. Normalization form C is
required so a composed character and its decomposed pair cannot alias.
"""

from __future__ import annotations

import re
import unicodedata

_TYPE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_RESERVED = frozenset({".", ".."})


def check_label(value: object, *, what: str) -> str | None:
    if not isinstance(value, str):
        return f"{what}_type"
    if value == "":
        return f"{what}_empty"
    if len(value) > 128:
        return f"{what}_length"
    for ch in value:
        category = unicodedata.category(ch)
        if category[0] in {"C", "Z"} or ch in "/\\":
            return f"{what}_chars"
    if unicodedata.normalize("NFC", value) != value:
        return f"{what}_form"
    if value in _RESERVED:
        return f"{what}_reserved"
    return None


def check_type(value: object) -> str | None:
    if not isinstance(value, str):
        return "type_type"
    if _TYPE.fullmatch(value) is None:
        return "type_form"
    return None
