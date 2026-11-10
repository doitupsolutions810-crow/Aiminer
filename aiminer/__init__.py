"""Fail-closed canonical intake miner."""

from aiminer.canon import canonical_bytes, canonicalize
from aiminer.corpus import build_cases
from aiminer.errors import (
    CANON_CODES,
    INTAKE_RESULT_CODES,
    SCAN_CODES,
    STORE_ERROR_CODES,
    STORE_RESULT_CODES,
    AiminerError,
    CanonError,
    ScanError,
    StoreError,
)
from aiminer.ids import check_label, check_type
from aiminer.intake import Decision, Intake
from aiminer.limits import DEPTH_CEILING, Limits
from aiminer.scan import parse_json
from aiminer.store import ReceiptStore, Stored

__version__ = "0.1.0"

__all__ = [
    "CANON_CODES",
    "DEPTH_CEILING",
    "INTAKE_RESULT_CODES",
    "SCAN_CODES",
    "STORE_ERROR_CODES",
    "STORE_RESULT_CODES",
    "AiminerError",
    "CanonError",
    "Decision",
    "Intake",
    "Limits",
    "ReceiptStore",
    "ScanError",
    "StoreError",
    "Stored",
    "__version__",
    "build_cases",
    "canonical_bytes",
    "canonicalize",
    "check_label",
    "check_type",
    "parse_json",
]
