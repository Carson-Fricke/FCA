"""Session helpers for Python consumers of the market lake.

Coverage classification and gap planning are native Rust; this package keeps
only the calendar helpers that survive the phase-6 deletion (see
``lake-rust-rewrite-plan.md``).
"""

from lake.coverage.sessions import (
    CoverageSession,
    crypto_sessions,
    equity_sessions,
    sessions_for,
)

__all__ = [
    "CoverageSession",
    "crypto_sessions",
    "equity_sessions",
    "sessions_for",
]
