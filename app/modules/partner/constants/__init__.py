"""Partner module constants.

Re-exports the predefined list of partner reject reasons so callers
can `from app.modules.partner.constants import REJECT_REASONS`.
"""

from app.modules.partner.constants.reject_reasons import (
    REJECT_REASONS,
    REJECT_REASON_CODES,
    SYSTEM_REJECT_REASON_CODES,
    SYSTEM_REJECT_REASON_TIMEOUT,
    SYSTEM_REJECT_REASON_ADMIN_REASSIGN,
    BREAKDOWN_REASONS,
    BREAKDOWN_REASON_CODES,
)

__all__ = [
    "REJECT_REASONS",
    "REJECT_REASON_CODES",
    "SYSTEM_REJECT_REASON_CODES",
    "SYSTEM_REJECT_REASON_TIMEOUT",
    "SYSTEM_REJECT_REASON_ADMIN_REASSIGN",
    "BREAKDOWN_REASONS",
    "BREAKDOWN_REASON_CODES",
]
