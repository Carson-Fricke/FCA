"""Path setup exposing the in-memory fake authority to protocol tests."""

from __future__ import annotations

import sys
from pathlib import Path

PROTECTED_TESTS = Path(__file__).resolve().parent
if str(PROTECTED_TESTS) not in sys.path:
    sys.path.insert(0, str(PROTECTED_TESTS))
