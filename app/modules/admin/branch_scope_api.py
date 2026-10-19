# ============================================================
# WAYTERO — ADMIN BRANCH SCOPE AUDIT
# File: app/modules/admin/branch_scope_api.py
# Doc Ref: app/core/branch_scope_registry.py, app/core/branch_scope_gate.py
# Mounted at /admin/branch-scope (SUPER_ADMIN only — api/router.py)
#
# The punch list for the branch rollout, read from the live route table instead
# of a spreadsheet: what has been reviewed, what has not, and — the part that
# actually bites — which rows currently have no city on them and therefore only
# the head office can see them.
#
# This is a diagnostic surface, not a control. It changes nothing and authorises
# nothing; every number here is a COUNT the owner can re-run by hand.
# ============================================================

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.branch_scope_registry import counts, stale_keys, undeclared_routes
from app.core.database import get_db
from app.core.dependencies import require_roles

router = APIRouter(tags=["Admin Branch Scope"])

# Queue tables that gained a nullable city in migration 0085. A row with no city
# here is not invisible — it is head-office work, by owner decision — but the
# list is what tells the owner how much of the queue a branch manager cannot see
# yet, and therefore how much backfill the next round needs.
_CITY_COLUMNS = (
    "enquiries",
    "callback_requests",
    "quotations",
    "contact_messages",
    "partner_applications",
    "chat_conversations",
    "customer_care_logs",
)

_BRANCHES_SQL = text(
    """
SELECT b.id,
       b.name,
       b.code,
       b.city_id,
       ci.name                       AS city_name,
       b.is_headquarters,
       b.is_active,
       COUNT(u.id)                   AS staff_accounts
  FROM branches b
  LEFT JOIN cities ci ON ci.id = b.city_id
  LEFT JOIN users u
    ON u.branch_id = b.id
   AND u.user_type IN ('ADMIN', 'CCO', 'VERIFICATION_OFFICER', 'FINANCE_MANAGER')
 GROUP BY b.id, b.name, b.code, b.city_id, ci.name, b.is_headquarters, b.is_active
 ORDER BY b.is_headquarters DESC, ci.name NULLS LAST, b.name
"""
)

_SUPER_SQL = text(
    """
SELECT id::text AS user_id,
       TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) AS name,
       email,
       is_active
  FROM users
 WHERE user_type = 'SUPER_ADMIN'
 ORDER BY created_at
"""
)

_CITYLESS_BOOKINGS_SQL = text(
    """
SELECT mb.city_id,
       ci.name        AS city_name,
       COUNT(mb.id)   AS bookings
  FROM master_bookings mb
  JOIN cities ci ON ci.id = mb.city_id
  LEFT JOIN branches b ON b.city_id = mb.city_id
 WHERE b.id IS NULL
 GROUP BY mb.city_id, ci.name
 ORDER BY COUNT(mb.id) DESC
"""
)


def _queue_sql() -> Any:
    arms = [
        f"SELECT '{t}' AS table_name, COUNT(*) AS total, "
        f"COUNT(*) FILTER (WHERE city_id IS NULL) AS without_city FROM {t}"
        for t in _CITY_COLUMNS
    ]
    return text(" UNION ALL ".join(arms) + " ORDER BY table_name")


@router.get(
    "/audit",
    summary="Branch review status: reviewed, unreviewed, and the rows with no city",
    description=(
        "One page for the branch rollout. The registry counts come from the route "
        "table the app is actually serving, so a renamed endpoint shows up here as "
        "unreviewed rather than quietly keeping its old classification. "
        "`undeclared_routes` is the list the next round works through — a branch "
        "account calling any of them is refused. `queue_rows_without_a_city` counts "
        "the leads, chats and care logs nobody has given a city yet, which only the "
        "head office can see today. SUPER_ADMIN only."
    ),
)
async def branch_scope_audit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles("SUPER_ADMIN")),
) -> dict:
    routes = request.app.routes
    registry_counts = counts(routes)
    undeclared = undeclared_routes(routes)
    stale = stale_keys(routes)

    branches = (await db.execute(_BRANCHES_SQL)).mappings().all()
    super_accounts = (await db.execute(_SUPER_SQL)).mappings().all()
    cityless_bookings = (await db.execute(_CITYLESS_BOOKINGS_SQL)).mappings().all()
    queue_rows = (await db.execute(_queue_sql())).mappings().all()

    return {
        "success": True,
        "registry": registry_counts,
        "undeclared_routes": undeclared,
        "stale_registry_entries": stale,
        "branches": [dict(row) for row in branches],
        "super_admin_accounts": {
            "count": len(super_accounts),
            "accounts": [dict(row) for row in super_accounts],
        },
        "bookings_in_cities_without_a_branch": [
            dict(row) for row in cityless_bookings
        ],
        "queue_rows_without_a_city": [dict(row) for row in queue_rows],
    }
