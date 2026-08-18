# ============================================================
# WAYTERO — ADMIN WALLET API
# File: app/modules/admin/wallet_api.py
# Prefix: /admin/wallets
#
# Purpose:
#   Unified wallet management for both Partner wallets and
#   Customer wallets. Admin can:
#     1. List all partner wallets (with search/filter/pagination)
#     2. List all customer wallets (with search/filter/pagination)
#     3. Get full wallet detail + ledger for a partner
#     4. Get full wallet detail + ledger for a customer
#     5. Recharge partner wallet (CASH or UPI — admin-recorded)
#     6. Credit wallet (partner or customer) — manual adjustment
#     7. Debit wallet (partner or customer) — manual adjustment
#     8. Dashboard summary (total balances, recent activity)
#
# Partner wallets:  table = wallets         / wallet_ledger
# Customer wallets: table = customer_wallets / customer_wallet_ledger
#
# NOTE: customers table uses first_name + last_name columns.
#       full_name is a Python @property only — NOT a DB column.
#       All SQL here builds full_name via COALESCE/TRIM/NULLIF.
#
# BRD Doc Ref:
#   BRD Part 6 §128-136 — Partner Wallet Management
#   BRD Part 6 §150     — Manual Adjustments (must be logged)
#   BRD Part 6 §157     — Financial Ledger (immutable, never deleted)
#   BRD Part 6 §159     — Audit Requirements
# ============================================================

from typing import Optional
import math

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles

router = APIRouter()

# ── SQL helper: build full_name expression from first/last ───────────────────
_CUST_FULLNAME_SQL = "NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' || COALESCE(c.last_name,'')), ' ') AS full_name"


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class RechargePartnerWalletRequest(BaseModel):
    partner_id: int
    amount: float = Field(..., gt=0, description="Recharge amount in INR")
    payment_mode: str = Field(..., description="CASH | UPI")
    upi_reference: Optional[str] = Field(
        None, description="UPI transaction ref / UTR number"
    )
    remarks: Optional[str] = Field(None, description="Admin remarks")


class CreditWalletRequest(BaseModel):
    wallet_type: str = Field(..., description="PARTNER | CUSTOMER")
    entity_id: int = Field(..., description="partner_id or customer_id")
    amount: float = Field(..., gt=0)
    cause: str = Field(..., min_length=3, description="Reason/cause for credit")
    remarks: Optional[str] = None


class DebitWalletRequest(BaseModel):
    wallet_type: str = Field(..., description="PARTNER | CUSTOMER")
    entity_id: int = Field(..., description="partner_id or customer_id")
    amount: float = Field(..., gt=0)
    cause: str = Field(..., min_length=3, description="Reason/cause for debit")
    remarks: Optional[str] = None


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


async def _get_partner_wallet(
    db: AsyncSession, partner_id: int, for_update: bool = False
):
    lock = "FOR UPDATE" if for_update else ""
    row = (
        (
            await db.execute(
                text(
                    f"""
            SELECT w.id, w.available_balance, w.hold_balance, w.credit_limit,
                   w.wallet_type, w.wallet_status
            FROM wallets w
            WHERE w.partner_id = :pid
            {lock}
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not row:
        await db.execute(
            text(
                """
                INSERT INTO wallets
                    (partner_id, wallet_type, available_balance, hold_balance,
                     credit_limit, wallet_status, created_at)
                VALUES (:pid, 'PREPAID', 0, 0, 0, 'ACTIVE', NOW())
                ON CONFLICT (partner_id) DO NOTHING
            """
            ),
            {"pid": partner_id},
        )
        await db.commit()
        row = (
            (
                await db.execute(
                    text(
                        f"""
                SELECT id, available_balance, hold_balance, credit_limit,
                       wallet_type, wallet_status
                FROM wallets WHERE partner_id = :pid {lock}
            """
                    ),
                    {"pid": partner_id},
                )
            )
            .mappings()
            .one_or_none()
        )
    return row


async def _get_customer_wallet(
    db: AsyncSession, customer_id: int, for_update: bool = False
):
    lock = "FOR UPDATE" if for_update else ""
    row = (
        (
            await db.execute(
                text(
                    f"""
            SELECT id, available_balance, hold_balance, wallet_status
            FROM customer_wallets
            WHERE customer_id = :cid
            {lock}
        """
                ),
                {"cid": customer_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not row:
        await db.execute(
            text(
                """
                INSERT INTO customer_wallets
                    (customer_id, available_balance, hold_balance,
                     wallet_status, created_at, updated_at)
                VALUES (:cid, 0, 0, 'ACTIVE', NOW(), NOW())
                ON CONFLICT (customer_id) DO NOTHING
            """
            ),
            {"cid": customer_id},
        )
        await db.commit()
        row = (
            (
                await db.execute(
                    text(
                        f"""
                SELECT id, available_balance, hold_balance, wallet_status
                FROM customer_wallets WHERE customer_id = :cid {lock}
            """
                    ),
                    {"cid": customer_id},
                )
            )
            .mappings()
            .one_or_none()
        )
    return row


async def _write_partner_ledger(
    db: AsyncSession,
    wallet_id: int,
    reference: str,
    ref_type: str,
    debit: float,
    credit: float,
    balance_after: float,
    narration: str,
):
    await db.execute(
        text(
            """
            INSERT INTO wallet_ledger
                (wallet_id, transaction_reference, reference_type,
                 debit_amount, credit_amount, balance_after, narration, created_at)
            VALUES (:wid, :ref, :rtype, :deb, :cred, :bal, :nar, NOW())
        """
        ),
        {
            "wid": wallet_id,
            "ref": reference,
            "rtype": ref_type,
            "deb": debit,
            "cred": credit,
            "bal": balance_after,
            "nar": narration,
        },
    )


async def _write_customer_ledger(
    db: AsyncSession,
    wallet_id: int,
    reference: str,
    ref_type: str,
    debit: float,
    credit: float,
    balance_after: float,
    narration: str,
):
    await db.execute(
        text(
            """
            INSERT INTO customer_wallet_ledger
                (customer_wallet_id, transaction_reference, reference_type,
                 debit_amount, credit_amount, balance_after, narration, created_at)
            VALUES (:wid, :ref, :rtype, :deb, :cred, :bal, :nar, NOW())
        """
        ),
        {
            "wid": wallet_id,
            "ref": reference,
            "rtype": ref_type,
            "deb": debit,
            "cred": credit,
            "bal": balance_after,
            "nar": narration,
        },
    )


def _gen_ref(prefix: str) -> str:
    from datetime import date
    import random

    return f"{prefix}-{date.today().strftime('%Y%m%d')}-{random.randint(10000, 99999)}"


# ════════════════════════════════════════════════════════════════
# WALLET DASHBOARD SUMMARY — GET /admin/wallets/summary
# ════════════════════════════════════════════════════════════════


@router.get("/summary", tags=["Wallets"])
async def wallet_summary(db: AsyncSession = Depends(get_db)):
    p_row = (
        (
            await db.execute(
                text(
                    """
        SELECT
            COUNT(*)                                                    AS total_partners,
            COALESCE(SUM(available_balance), 0)                         AS total_available,
            COALESCE(SUM(hold_balance), 0)                              AS total_hold,
            COUNT(*) FILTER (WHERE wallet_status = 'ACTIVE')            AS active_count,
            COUNT(*) FILTER (WHERE wallet_status = 'SUSPENDED')         AS suspended_count
        FROM wallets
    """
                )
            )
        )
        .mappings()
        .one()
    )

    c_row = (
        (
            await db.execute(
                text(
                    """
        SELECT
            COUNT(*)                            AS total_customers,
            COALESCE(SUM(available_balance), 0) AS total_available,
            COALESCE(SUM(hold_balance), 0)      AS total_hold
        FROM customer_wallets
    """
                )
            )
        )
        .mappings()
        .one()
    )

    recent_partner = (
        (
            await db.execute(
                text(
                    """
        SELECT wl.id, wl.transaction_reference, wl.reference_type,
               wl.debit_amount, wl.credit_amount, wl.balance_after,
               wl.narration, wl.created_at,
               p.business_name, p.owner_name, p.id AS partner_id
        FROM wallet_ledger wl
        JOIN wallets w ON w.id = wl.wallet_id
        JOIN partners p ON p.id = w.partner_id
        ORDER BY wl.created_at DESC
        LIMIT 5
    """
                )
            )
        )
        .mappings()
        .all()
    )

    recent_customer = (
        (
            await db.execute(
                text(
                    f"""
        SELECT cwl.id, cwl.transaction_reference, cwl.reference_type,
               cwl.debit_amount, cwl.credit_amount, cwl.balance_after,
               cwl.narration, cwl.created_at,
               {_CUST_FULLNAME_SQL}, c.id AS customer_id
        FROM customer_wallet_ledger cwl
        JOIN customer_wallets cw ON cw.id = cwl.customer_wallet_id
        JOIN customers c ON c.id = cw.customer_id
        ORDER BY cwl.created_at DESC
        LIMIT 5
    """
                )
            )
        )
        .mappings()
        .all()
    )

    return {
        "partner_wallets": {
            "total_partners": int(p_row["total_partners"]),
            "total_available": float(p_row["total_available"]),
            "total_hold": float(p_row["total_hold"]),
            "active_count": int(p_row["active_count"]),
            "suspended_count": int(p_row["suspended_count"]),
        },
        "customer_wallets": {
            "total_customers": int(c_row["total_customers"]),
            "total_available": float(c_row["total_available"]),
            "total_hold": float(c_row["total_hold"]),
        },
        "recent_partner_activity": [
            {
                "id": r["id"],
                "partner_id": r["partner_id"],
                "partner_name": r["business_name"] or r["owner_name"],
                "ref": r["transaction_reference"],
                "ref_type": r["reference_type"],
                "debit": float(r["debit_amount"] or 0),
                "credit": float(r["credit_amount"] or 0),
                "balance_after": float(r["balance_after"] or 0),
                "narration": r["narration"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in recent_partner
        ],
        "recent_customer_activity": [
            {
                "id": r["id"],
                "customer_id": r["customer_id"],
                "customer_name": r["full_name"] or "—",
                "ref": r["transaction_reference"],
                "ref_type": r["reference_type"],
                "debit": float(r["debit_amount"] or 0),
                "credit": float(r["credit_amount"] or 0),
                "balance_after": float(r["balance_after"] or 0),
                "narration": r["narration"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in recent_customer
        ],
    }


# ════════════════════════════════════════════════════════════════
# LIST PARTNER WALLETS — GET /admin/wallets/partners
# ════════════════════════════════════════════════════════════════


@router.get("/partners", tags=["Wallets"])
async def list_partner_wallets(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str = Query("", description="Search by name / mobile / code"),
    status: str = Query("", description="Filter by wallet_status: ACTIVE | SUSPENDED"),
    db: AsyncSession = Depends(get_db),
):
    where_clauses = ["1=1"]
    params: dict = {}

    if search:
        where_clauses.append(
            """
            (p.business_name ILIKE :s OR p.owner_name ILIKE :s
             OR p.mobile ILIKE :s OR p.partner_code ILIKE :s)
        """
        )
        params["s"] = f"%{search}%"

    if status:
        where_clauses.append("w.wallet_status = :status")
        params["status"] = status

    where = " AND ".join(where_clauses)

    total = (
        (
            await db.execute(
                text(
                    f"""
            SELECT COUNT(*) FROM wallets w
            JOIN partners p ON p.id = w.partner_id
            WHERE {where}
        """
                ),
                params,
            )
        ).scalar()
        or 0
    )

    offset = (page - 1) * page_size
    rows = (
        (
            await db.execute(
                text(
                    f"""
            SELECT w.id AS wallet_id, w.available_balance, w.hold_balance,
                   w.credit_limit, w.wallet_type, w.wallet_status,
                   w.created_at AS wallet_created,
                   p.id AS partner_id, p.partner_code, p.partner_type,
                   p.business_name, p.owner_name, p.mobile,
                   p.status AS partner_status
            FROM wallets w
            JOIN partners p ON p.id = w.partner_id
            WHERE {where}
            ORDER BY w.available_balance DESC, p.id ASC
            LIMIT :lim OFFSET :off
        """
                ),
                {**params, "lim": page_size, "off": offset},
            )
        )
        .mappings()
        .all()
    )

    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(int(total) / page_size) if total > 0 else 1,
        "items": [
            {
                "wallet_id": r["wallet_id"],
                "partner_id": r["partner_id"],
                "partner_code": r["partner_code"],
                "partner_type": r["partner_type"],
                "partner_name": r["business_name"] or r["owner_name"],
                "mobile": r["mobile"],
                "partner_status": r["partner_status"],
                "wallet_type": r["wallet_type"],
                "wallet_status": r["wallet_status"],
                "available_balance": float(r["available_balance"] or 0),
                "hold_balance": float(r["hold_balance"] or 0),
                "credit_limit": float(r["credit_limit"] or 0),
                "total_balance": float(
                    (r["available_balance"] or 0) + (r["hold_balance"] or 0)
                ),
            }
            for r in rows
        ],
    }


# ════════════════════════════════════════════════════════════════
# LIST CUSTOMER WALLETS — GET /admin/wallets/customers
# ════════════════════════════════════════════════════════════════


@router.get("/customers", tags=["Wallets"])
async def list_customer_wallets(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str = Query("", description="Search by name / mobile / email"),
    db: AsyncSession = Depends(get_db),
):
    # Auto-create missing wallets before listing so every registered
    # customer is visible (idempotent — ON CONFLICT DO NOTHING).
    await db.execute(
        text(
            """
        INSERT INTO customer_wallets
            (customer_id, available_balance, hold_balance,
             wallet_status, created_at, updated_at)
        SELECT c.id, 0, 0, 'ACTIVE', NOW(), NOW()
        FROM   customers c
        WHERE  NOT EXISTS (
            SELECT 1 FROM customer_wallets cw WHERE cw.customer_id = c.id
        )
        ON CONFLICT (customer_id) DO NOTHING
    """
        )
    )
    await db.commit()

    where_clauses = ["1=1"]
    params: dict = {}

    if search:
        where_clauses.append(
            """
            (c.first_name ILIKE :s OR c.last_name ILIKE :s
             OR u.mobile_number ILIKE :s OR u.email ILIKE :s)
        """
        )
        params["s"] = f"%{search}%"

    where = " AND ".join(where_clauses)

    total = (
        (
            await db.execute(
                text(
                    f"""
            SELECT COUNT(*) FROM customer_wallets cw
            JOIN customers c ON c.id = cw.customer_id
            JOIN users u ON u.id = c.user_id
            WHERE {where}
        """
                ),
                params,
            )
        ).scalar()
        or 0
    )

    offset = (page - 1) * page_size
    rows = (
        (
            await db.execute(
                text(
                    f"""
            SELECT cw.id AS wallet_id, cw.customer_id,
                   cw.available_balance, cw.hold_balance,
                   cw.wallet_status, cw.created_at AS wallet_created,
                   {_CUST_FULLNAME_SQL},
                   u.mobile_number AS mobile, u.email
            FROM customer_wallets cw
            JOIN customers c ON c.id = cw.customer_id
            JOIN users u ON u.id = c.user_id
            WHERE {where}
            ORDER BY cw.available_balance DESC, c.id ASC
            LIMIT :lim OFFSET :off
        """
                ),
                {**params, "lim": page_size, "off": offset},
            )
        )
        .mappings()
        .all()
    )

    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(int(total) / page_size) if total > 0 else 1,
        "items": [
            {
                "wallet_id": r["wallet_id"],
                "customer_id": r["customer_id"],
                "customer_name": r["full_name"] or "—",
                "mobile": r["mobile"],
                "email": r["email"],
                "wallet_status": r["wallet_status"],
                "available_balance": float(r["available_balance"] or 0),
                "hold_balance": float(r["hold_balance"] or 0),
                "total_balance": float(
                    (r["available_balance"] or 0) + (r["hold_balance"] or 0)
                ),
            }
            for r in rows
        ],
    }


# ════════════════════════════════════════════════════════════════
# PARTNER WALLET DETAIL + LEDGER — GET /admin/wallets/partners/{id}
# ════════════════════════════════════════════════════════════════


@router.get("/partners/{partner_id}", tags=["Wallets"])
async def get_partner_wallet_detail(
    partner_id: int,
    ledger_page: int = Query(1, ge=1),
    ledger_page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    p_row = (
        (
            await db.execute(
                text(
                    """
            SELECT p.id, p.partner_code, p.partner_type, p.business_name,
                   p.owner_name, p.mobile, p.status AS partner_status
            FROM partners p WHERE p.id = :pid
        """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not p_row:
        raise HTTPException(404, "Partner not found")

    wallet = await _get_partner_wallet(db, partner_id)
    if not wallet:
        raise HTTPException(404, "Wallet not found")

    total_entries = (
        await db.execute(
            text("SELECT COUNT(*) FROM wallet_ledger WHERE wallet_id = :wid"),
            {"wid": wallet["id"]},
        )
    ).scalar() or 0

    offset = (ledger_page - 1) * ledger_page_size
    ledger_rows = (
        (
            await db.execute(
                text(
                    """
            SELECT id, transaction_reference, reference_type,
                   debit_amount, credit_amount, balance_after,
                   narration, created_at
            FROM wallet_ledger
            WHERE wallet_id = :wid
            ORDER BY created_at DESC, id DESC
            LIMIT :lim OFFSET :off
        """
                ),
                {"wid": wallet["id"], "lim": ledger_page_size, "off": offset},
            )
        )
        .mappings()
        .all()
    )

    return {
        "partner": {
            "id": p_row["id"],
            "partner_code": p_row["partner_code"],
            "partner_type": p_row["partner_type"],
            "partner_name": p_row["business_name"] or p_row["owner_name"],
            "mobile": p_row["mobile"],
            "partner_status": p_row["partner_status"],
        },
        "wallet": {
            "id": wallet["id"],
            "wallet_type": wallet["wallet_type"],
            "wallet_status": wallet["wallet_status"],
            "available_balance": float(wallet["available_balance"] or 0),
            "hold_balance": float(wallet["hold_balance"] or 0),
            "credit_limit": float(wallet["credit_limit"] or 0),
            "total_balance": float(
                (wallet["available_balance"] or 0) + (wallet["hold_balance"] or 0)
            ),
        },
        "ledger": {
            "total": int(total_entries),
            "page": ledger_page,
            "page_size": ledger_page_size,
            "total_pages": (
                math.ceil(int(total_entries) / ledger_page_size)
                if total_entries > 0
                else 1
            ),
            "entries": [
                {
                    "id": r["id"],
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


# ════════════════════════════════════════════════════════════════
# CUSTOMER WALLET DETAIL + LEDGER — GET /admin/wallets/customer/{id}
# ════════════════════════════════════════════════════════════════


@router.get("/customer/{customer_id}", tags=["Wallets"])
async def get_customer_wallet_detail(
    customer_id: int,
    ledger_page: int = Query(1, ge=1),
    ledger_page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    c_row = (
        (
            await db.execute(
                text(
                    f"""
            SELECT c.id,
                   {_CUST_FULLNAME_SQL},
                   u.mobile_number AS mobile, u.email
            FROM customers c
            JOIN users u ON u.id = c.user_id
            WHERE c.id = :cid
        """
                ),
                {"cid": customer_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not c_row:
        raise HTTPException(404, "Customer not found")

    wallet = await _get_customer_wallet(db, customer_id)
    if not wallet:
        raise HTTPException(404, "Customer wallet not found")

    total_entries = (
        await db.execute(
            text(
                "SELECT COUNT(*) FROM customer_wallet_ledger WHERE customer_wallet_id = :wid"
            ),
            {"wid": wallet["id"]},
        )
    ).scalar() or 0

    offset = (ledger_page - 1) * ledger_page_size
    ledger_rows = (
        (
            await db.execute(
                text(
                    """
            SELECT id, transaction_reference, reference_type,
                   debit_amount, credit_amount, balance_after,
                   narration, created_at
            FROM customer_wallet_ledger
            WHERE customer_wallet_id = :wid
            ORDER BY created_at DESC, id DESC
            LIMIT :lim OFFSET :off
        """
                ),
                {"wid": wallet["id"], "lim": ledger_page_size, "off": offset},
            )
        )
        .mappings()
        .all()
    )

    return {
        "customer": {
            "id": c_row["id"],
            "name": c_row["full_name"] or "—",
            "mobile": c_row["mobile"],
            "email": c_row["email"],
        },
        "wallet": {
            "id": wallet["id"],
            "wallet_status": wallet["wallet_status"],
            "available_balance": float(wallet["available_balance"] or 0),
            "hold_balance": float(wallet["hold_balance"] or 0),
            "total_balance": float(
                (wallet["available_balance"] or 0) + (wallet["hold_balance"] or 0)
            ),
        },
        "ledger": {
            "total": int(total_entries),
            "page": ledger_page,
            "page_size": ledger_page_size,
            "total_pages": (
                math.ceil(int(total_entries) / ledger_page_size)
                if total_entries > 0
                else 1
            ),
            "entries": [
                {
                    "id": r["id"],
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


# ════════════════════════════════════════════════════════════════
# RECHARGE PARTNER WALLET — POST /admin/wallets/recharge
# ════════════════════════════════════════════════════════════════


@router.post("/recharge", tags=["Wallets"])
async def recharge_partner_wallet(
    payload: RechargePartnerWalletRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
):
    mode = payload.payment_mode.upper()
    if mode not in ("CASH", "UPI"):
        raise HTTPException(400, "payment_mode must be CASH or UPI")

    p = (
        (
            await db.execute(
                text(
                    "SELECT id, business_name, owner_name, email FROM partners WHERE id = :pid"
                ),
                {"pid": payload.partner_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not p:
        raise HTTPException(404, f"Partner #{payload.partner_id} not found")

    wallet = await _get_partner_wallet(db, payload.partner_id, for_update=True)
    if wallet["wallet_status"] == "SUSPENDED":
        raise HTTPException(400, "Wallet is SUSPENDED. Unsuspend before recharging.")

    old_bal = float(wallet["available_balance"] or 0)
    new_bal = round(old_bal + payload.amount, 2)

    ref = _gen_ref(f"RCH-{mode}")
    narration = (
        f"Admin recharge via {mode}: Rs. {payload.amount:,.2f}. "
        + (f"UPI Ref: {payload.upi_reference}. " if payload.upi_reference else "")
        + (payload.remarks or "")
    ).strip()

    await db.execute(
        text("UPDATE wallets SET available_balance = :bal WHERE id = :wid"),
        {"bal": new_bal, "wid": wallet["id"]},
    )
    await _write_partner_ledger(
        db,
        wallet["id"],
        ref,
        f"RECHARGE_{mode}",
        debit=0.0,
        credit=payload.amount,
        balance_after=new_bal,
        narration=narration,
    )
    await db.commit()

    # ── Email: wallet recharged to the partner ──
    try:
        from app.infrastructure.email import send_event_email

        if (p["email"] or "").strip():
            await send_event_email(
                db,
                event_type="wallet_recharged",
                to_email=p["email"].strip(),
                to_name=p["business_name"] or p["owner_name"] or None,
                context={
                    "name": p["business_name"] or p["owner_name"] or "partner",
                    "wallet_type": "partner",
                    "message": (
                        f"Your WayTero partner wallet has been recharged via {mode}."
                    ),
                    "details": [
                        ("Reference", ref),
                        ("Payment mode", mode),
                        ("Previous balance", f"₹{old_bal:,.2f}"),
                        ("New balance", f"₹{new_bal:,.2f}"),
                    ],
                    "amount": payload.amount,
                    "amount_label": "Recharged amount",
                },
                related_type="PARTNER_WALLET",
                related_id=payload.partner_id,
            )
    except Exception:  # pragma: no cover — email must never break the wallet
        pass

    return {
        "success": True,
        "message": f"Wallet recharged with Rs. {payload.amount:,.2f} via {mode}.",
        "partner_name": p["business_name"] or p["owner_name"],
        "previous_balance": old_bal,
        "recharged_amount": payload.amount,
        "new_balance": new_bal,
        "reference": ref,
    }


# ════════════════════════════════════════════════════════════════
# CREDIT WALLET — POST /admin/wallets/credit
# ════════════════════════════════════════════════════════════════


@router.post("/credit", tags=["Wallets"])
async def credit_wallet(
    payload: CreditWalletRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
):
    wtype = payload.wallet_type.upper()
    if wtype not in ("PARTNER", "CUSTOMER"):
        raise HTTPException(400, "wallet_type must be PARTNER or CUSTOMER")

    ref = _gen_ref("CREDIT-ADJ")
    narration = f"Admin credit adjustment — {payload.cause}. " + (payload.remarks or "")

    if wtype == "PARTNER":
        p = (
            (
                await db.execute(
                    text(
                        "SELECT id, business_name, owner_name, email FROM partners WHERE id = :id"
                    ),
                    {"id": payload.entity_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if not p:
            raise HTTPException(404, "Partner not found")

        wallet = await _get_partner_wallet(db, payload.entity_id, for_update=True)
        old_bal = float(wallet["available_balance"] or 0)
        new_bal = round(old_bal + payload.amount, 2)

        await db.execute(
            text("UPDATE wallets SET available_balance = :bal WHERE id = :wid"),
            {"bal": new_bal, "wid": wallet["id"]},
        )
        await _write_partner_ledger(
            db,
            wallet["id"],
            ref,
            "CREDIT_ADJUSTMENT",
            debit=0.0,
            credit=payload.amount,
            balance_after=new_bal,
            narration=narration,
        )
        entity_name = p["business_name"] or p["owner_name"]
        entity_email = (p["email"] or "").strip()
        entity_is_partner = True

    else:  # CUSTOMER
        c = (
            (
                await db.execute(
                    text(
                        f"""
                SELECT c.id, {_CUST_FULLNAME_SQL}
                FROM customers c WHERE c.id = :id
            """
                    ),
                    {"id": payload.entity_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if not c:
            raise HTTPException(404, "Customer not found")

        wallet = await _get_customer_wallet(db, payload.entity_id, for_update=True)
        old_bal = float(wallet["available_balance"] or 0)
        new_bal = round(old_bal + payload.amount, 2)

        await db.execute(
            text(
                "UPDATE customer_wallets SET available_balance = :bal, updated_at = NOW() WHERE id = :wid"
            ),
            {"bal": new_bal, "wid": wallet["id"]},
        )
        await _write_customer_ledger(
            db,
            wallet["id"],
            ref,
            "CREDIT_ADJUSTMENT",
            debit=0.0,
            credit=payload.amount,
            balance_after=new_bal,
            narration=narration,
        )
        entity_name = c["full_name"] or "—"
        entity_email = ""
        entity_is_partner = False
        c_user = (
            await db.execute(
                text(
                    "SELECT u.email FROM users u JOIN customers c ON c.user_id = u.id WHERE c.id = :cid"
                ),
                {"cid": payload.entity_id},
            )
        ).scalar()
        entity_email = (c_user or "").strip()

    await db.commit()

    # ── Email: wallet credited (partner or customer) ──
    try:
        from app.infrastructure.email import send_event_email

        if entity_email:
            await send_event_email(
                db,
                event_type="wallet_recharged",
                to_email=entity_email,
                to_name=entity_name if entity_name != "—" else None,
                context={
                    "name": entity_name if entity_name != "—" else "there",
                    "wallet_type": "partner" if entity_is_partner else "customer",
                    "message": (
                        f"₹{payload.amount:,.2f} has been credited to your WayTero "
                        "wallet by the WayTero team."
                    ),
                    "details": [
                        ("Reference", ref),
                        ("Reason", payload.cause),
                        ("Previous balance", f"₹{old_bal:,.2f}"),
                        ("New balance", f"₹{new_bal:,.2f}"),
                    ],
                    "amount": payload.amount,
                    "amount_label": "Credited amount",
                },
                related_type=(
                    "PARTNER_WALLET" if entity_is_partner else "CUSTOMER_WALLET"
                ),
                related_id=payload.entity_id,
            )
    except Exception:  # pragma: no cover — email must never break the wallet
        pass

    return {
        "success": True,
        "message": f"Rs. {payload.amount:,.2f} credited to {wtype} wallet of {entity_name}.",
        "entity_name": entity_name,
        "previous_balance": old_bal,
        "credited_amount": payload.amount,
        "new_balance": new_bal,
        "reference": ref,
    }


# ════════════════════════════════════════════════════════════════
# DEBIT WALLET — POST /admin/wallets/debit
# ════════════════════════════════════════════════════════════════


@router.post("/debit", tags=["Wallets"])
async def debit_wallet(
    payload: DebitWalletRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
):
    wtype = payload.wallet_type.upper()
    if wtype not in ("PARTNER", "CUSTOMER"):
        raise HTTPException(400, "wallet_type must be PARTNER or CUSTOMER")

    ref = _gen_ref("DEBIT-ADJ")
    narration = f"Admin debit adjustment — {payload.cause}. " + (payload.remarks or "")

    if wtype == "PARTNER":
        p = (
            (
                await db.execute(
                    text(
                        "SELECT id, business_name, owner_name FROM partners WHERE id = :id"
                    ),
                    {"id": payload.entity_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if not p:
            raise HTTPException(404, "Partner not found")

        wallet = await _get_partner_wallet(db, payload.entity_id, for_update=True)
        old_bal = float(wallet["available_balance"] or 0)
        if payload.amount > old_bal:
            raise HTTPException(
                400,
                f"Insufficient balance. Available: Rs. {old_bal:,.2f} | "
                f"Requested debit: Rs. {payload.amount:,.2f}",
            )
        new_bal = round(old_bal - payload.amount, 2)

        await db.execute(
            text("UPDATE wallets SET available_balance = :bal WHERE id = :wid"),
            {"bal": new_bal, "wid": wallet["id"]},
        )
        await _write_partner_ledger(
            db,
            wallet["id"],
            ref,
            "DEBIT_ADJUSTMENT",
            debit=payload.amount,
            credit=0.0,
            balance_after=new_bal,
            narration=narration,
        )
        entity_name = p["business_name"] or p["owner_name"]

    else:  # CUSTOMER
        c = (
            (
                await db.execute(
                    text(
                        f"""
                SELECT c.id, {_CUST_FULLNAME_SQL}
                FROM customers c WHERE c.id = :id
            """
                    ),
                    {"id": payload.entity_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if not c:
            raise HTTPException(404, "Customer not found")

        wallet = await _get_customer_wallet(db, payload.entity_id, for_update=True)
        old_bal = float(wallet["available_balance"] or 0)
        if payload.amount > old_bal:
            raise HTTPException(
                400,
                f"Insufficient balance. Available: Rs. {old_bal:,.2f} | "
                f"Requested debit: Rs. {payload.amount:,.2f}",
            )
        new_bal = round(old_bal - payload.amount, 2)

        await db.execute(
            text(
                "UPDATE customer_wallets SET available_balance = :bal, updated_at = NOW() WHERE id = :wid"
            ),
            {"bal": new_bal, "wid": wallet["id"]},
        )
        await _write_customer_ledger(
            db,
            wallet["id"],
            ref,
            "DEBIT_ADJUSTMENT",
            debit=payload.amount,
            credit=0.0,
            balance_after=new_bal,
            narration=narration,
        )
        entity_name = c["full_name"] or "—"

    await db.commit()

    return {
        "success": True,
        "message": f"Rs. {payload.amount:,.2f} debited from {wtype} wallet of {entity_name}.",
        "entity_name": entity_name,
        "previous_balance": old_bal,
        "debited_amount": payload.amount,
        "new_balance": new_bal,
        "reference": ref,
    }
