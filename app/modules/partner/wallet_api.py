# ============================================================
# WAYTERO — PARTNER WALLET API
# File: app/modules/partner/wallet_api.py
# Prefix: /partners/me/wallet
#
# Authenticated partner reads their own wallet balance,
# ledger history (paginated), and summary stats.
# No mutations — top-ups are admin-initiated only.
#
# Doc Ref: BRD Part 6 §128-136
# ============================================================

import math
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_partner

router = APIRouter()


async def _resolve_partner_id(db: AsyncSession, user_uuid: str) -> int:
    row = (
        (
            await db.execute(
                text("SELECT id FROM partners WHERE user_id = :uid"),
                {"uid": user_uuid},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not row:
        from fastapi import HTTPException

        raise HTTPException(404, "Partner profile not found for this user.")
    return int(row["id"])


# ════════════════════════════════════════════════════════════════
# MY WALLET — GET /partners/me/wallet
# Full wallet detail: balance, stats, paginated ledger
# ════════════════════════════════════════════════════════════════


@router.get("/me/wallet", tags=["Partner Wallet"])
async def get_my_wallet(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    ref_type: str = Query(
        "ALL",
        description="ALL | SETTLEMENT | SETTLEMENT_DEBIT | COMMISSION_DEBIT | MANUAL_CREDIT | MANUAL_DEBIT | RECHARGE",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_partner()),
):
    """
    Partner's own wallet: balance card + paginated ledger.
    Optionally filter by reference_type.
    """
    partner_id = await _resolve_partner_id(db, current_user["sub"])

    # Wallet row
    wallet = (
        (
            await db.execute(
                text(
                    """
            SELECT id, wallet_type, wallet_status,
                   available_balance, hold_balance, credit_limit,
                   created_at
            FROM wallets WHERE partner_id = :pid
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one_or_none()
    )

    if not wallet:
        # Return empty state — wallet may not be provisioned yet
        return {
            "wallet": None,
            "summary": {
                "total_credited": 0.0,
                "total_debited": 0.0,
                "settlement_credits": 0.0,
                "commission_debits": 0.0,
                "recharge_credits": 0.0,
                "ledger_count": 0,
            },
            "ledger": {
                "total": 0,
                "page": page,
                "page_size": page_size,
                "total_pages": 1,
                "entries": [],
            },
        }

    wallet_id = int(wallet["id"])

    # Aggregate stats from ledger
    stats = (
        (
            await db.execute(
                text(
                    """
            SELECT
                COALESCE(SUM(credit_amount), 0)                                              AS total_credited,
                COALESCE(SUM(debit_amount), 0)                                               AS total_debited,
                COALESCE(SUM(credit_amount) FILTER (WHERE reference_type = 'SETTLEMENT'), 0) AS settlement_credits,
                COALESCE(SUM(debit_amount)  FILTER (WHERE reference_type IN ('SETTLEMENT_DEBIT','COMMISSION_DEBIT')), 0) AS commission_debits,
                COALESCE(SUM(credit_amount) FILTER (WHERE reference_type LIKE 'RECHARGE%'), 0) AS recharge_credits,
                COUNT(*)                                                                      AS ledger_count
            FROM wallet_ledger WHERE wallet_id = :wid
        """
                ),
                {"wid": wallet_id},
            )
        )
        .mappings()
        .one()
    )

    # Ledger (filtered)
    if ref_type == "ALL":
        where_type = ""
    elif ref_type == "RECHARGE":
        where_type = "AND reference_type LIKE 'RECHARGE%'"
    else:
        where_type = "AND reference_type = :rtype"
    total_entries = (
        await db.execute(
            text(
                f"SELECT COUNT(*) FROM wallet_ledger WHERE wallet_id = :wid {where_type}"
            ),
            {"wid": wallet_id, "rtype": ref_type},
        )
    ).scalar() or 0

    offset = (page - 1) * page_size
    ledger_rows = (
        (
            await db.execute(
                text(
                    f"""
            SELECT id, transaction_reference, reference_type,
                   debit_amount, credit_amount, balance_after,
                   narration, created_at
            FROM wallet_ledger
            WHERE wallet_id = :wid {where_type}
            ORDER BY created_at DESC, id DESC
            LIMIT :lim OFFSET :off
        """
                ),
                {"wid": wallet_id, "rtype": ref_type, "lim": page_size, "off": offset},
            )
        )
        .mappings()
        .all()
    )

    return {
        "wallet": {
            "id": wallet_id,
            "wallet_type": wallet["wallet_type"],
            "wallet_status": wallet["wallet_status"],
            "available_balance": float(wallet["available_balance"] or 0),
            "hold_balance": float(wallet["hold_balance"] or 0),
            "credit_limit": float(wallet["credit_limit"] or 0),
            "created_at": (
                wallet["created_at"].isoformat() if wallet["created_at"] else None
            ),
        },
        "summary": {
            "total_credited": float(stats["total_credited"] or 0),
            "total_debited": float(stats["total_debited"] or 0),
            "settlement_credits": float(stats["settlement_credits"] or 0),
            "commission_debits": float(stats["commission_debits"] or 0),
            "recharge_credits": float(stats["recharge_credits"] or 0),
            "ledger_count": int(stats["ledger_count"] or 0),
        },
        "ledger": {
            "total": int(total_entries),
            "page": page,
            "page_size": page_size,
            "total_pages": (
                math.ceil(int(total_entries) / page_size) if total_entries > 0 else 1
            ),
            "entries": [
                {
                    "id": int(r["id"]),
                    "ref": r["transaction_reference"],
                    "ref_type": r["reference_type"],
                    "debit": float(r["debit_amount"] or 0),
                    "credit": float(r["credit_amount"] or 0),
                    "balance_after": float(r["balance_after"] or 0),
                    "narration": r["narration"],
                    "created_at": (
                        r["created_at"].isoformat() if r["created_at"] else None
                    ),
                }
                for r in ledger_rows
            ],
        },
    }
