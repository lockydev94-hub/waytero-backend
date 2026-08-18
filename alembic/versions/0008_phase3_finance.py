"""Phase 3 - Finance tables: payments, refunds, wallets, settlements, GST, accounting

Revision ID: 0008_phase3_finance
Revises: 0007_phase3_booking
Create Date: 2026-06-02

Doc Ref: DB Schema Part 7 — Finance (Sections 3-20)
Phase: 3 — Payment / Wallet / Settlement
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_phase3_finance"
down_revision = "0007_phase3_booking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # --- Payments ---
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS payments (
            id BIGSERIAL PRIMARY KEY,
            uuid UUID NOT NULL UNIQUE,
            payment_number VARCHAR(100) UNIQUE NOT NULL,
            master_booking_id BIGINT NOT NULL REFERENCES master_bookings(id),
            customer_id BIGINT NOT NULL REFERENCES customers(id),
            payment_type VARCHAR(50),
            payment_method VARCHAR(50),
            payment_status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
            amount NUMERIC(14,2) NOT NULL,
            gateway_amount NUMERIC(14,2),
            gateway_reference VARCHAR(255),
            gateway_transaction_id VARCHAR(255),
            payment_datetime TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_payment_booking ON payments(master_booking_id)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_payment_status ON payments(payment_status)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_payment_customer ON payments(customer_id)
    """))

    # --- Payment Transactions ---
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS payment_transactions (
            id BIGSERIAL PRIMARY KEY,
            payment_id BIGINT NOT NULL REFERENCES payments(id),
            transaction_type VARCHAR(50),
            gateway_response JSONB,
            transaction_amount NUMERIC(14,2),
            transaction_datetime TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_pay_txn_payment ON payment_transactions(payment_id)
    """))

    # --- Refunds ---
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS refunds (
            id BIGSERIAL PRIMARY KEY,
            uuid UUID NOT NULL UNIQUE,
            payment_id BIGINT NOT NULL REFERENCES payments(id),
            refund_number VARCHAR(100) UNIQUE NOT NULL,
            refund_amount NUMERIC(14,2),
            refund_reason TEXT,
            refund_status VARCHAR(50) DEFAULT 'PENDING',
            gateway_refund_id VARCHAR(255),
            requested_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMP WITH TIME ZONE
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_refund_payment ON refunds(payment_id)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_refund_status ON refunds(refund_status)
    """))

    # --- Wallets ---
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS wallets (
            id BIGSERIAL PRIMARY KEY,
            partner_id BIGINT NOT NULL UNIQUE REFERENCES partners(id),
            wallet_type VARCHAR(50) DEFAULT 'PREPAID',
            available_balance NUMERIC(14,2) DEFAULT 0,
            hold_balance NUMERIC(14,2) DEFAULT 0,
            credit_limit NUMERIC(14,2) DEFAULT 0,
            wallet_status VARCHAR(50) DEFAULT 'ACTIVE',
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_wallet_partner ON wallets(partner_id)
    """))

    # --- Wallet Ledger (immutable) ---
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS wallet_ledger (
            id BIGSERIAL PRIMARY KEY,
            wallet_id BIGINT NOT NULL REFERENCES wallets(id),
            transaction_reference VARCHAR(100),
            reference_type VARCHAR(50),
            debit_amount NUMERIC(14,2) DEFAULT 0,
            credit_amount NUMERIC(14,2) DEFAULT 0,
            balance_after NUMERIC(14,2),
            narration TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_ledger_wallet ON wallet_ledger(wallet_id)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_ledger_ref ON wallet_ledger(transaction_reference)
    """))

    # --- Wallet Recharges ---
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS wallet_recharges (
            id BIGSERIAL PRIMARY KEY,
            wallet_id BIGINT NOT NULL REFERENCES wallets(id),
            payment_id BIGINT NOT NULL REFERENCES payments(id),
            recharge_amount NUMERIC(14,2),
            recharge_datetime TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        );
    """))

    # --- Settlements ---
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS settlements (
            id BIGSERIAL PRIMARY KEY,
            uuid UUID NOT NULL UNIQUE,
            settlement_number VARCHAR(100) UNIQUE NOT NULL,
            partner_id BIGINT NOT NULL REFERENCES partners(id),
            settlement_type VARCHAR(50) DEFAULT 'WEEKLY',
            start_date DATE,
            end_date DATE,
            gross_amount NUMERIC(14,2) DEFAULT 0,
            commission_amount NUMERIC(14,2) DEFAULT 0,
            gst_amount NUMERIC(14,2) DEFAULT 0,
            net_payable_amount NUMERIC(14,2) DEFAULT 0,
            settlement_status VARCHAR(50) DEFAULT 'PENDING',
            settled_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_settlement_partner ON settlements(partner_id)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_settlement_status ON settlements(settlement_status)
    """))

    # --- Settlement Items ---
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS settlement_items (
            id BIGSERIAL PRIMARY KEY,
            settlement_id BIGINT NOT NULL REFERENCES settlements(id),
            booking_id BIGINT NOT NULL,
            booking_type VARCHAR(50),
            gross_amount NUMERIC(14,2),
            commission_amount NUMERIC(14,2),
            net_amount NUMERIC(14,2),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_settlement_item ON settlement_items(settlement_id)
    """))

    # --- GST Invoices ---
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS gst_invoices (
            id BIGSERIAL PRIMARY KEY,
            invoice_number VARCHAR(100) UNIQUE NOT NULL,
            payment_id BIGINT REFERENCES payments(id),
            settlement_id BIGINT REFERENCES settlements(id),
            taxable_amount NUMERIC(14,2),
            cgst_amount NUMERIC(14,2) DEFAULT 0,
            sgst_amount NUMERIC(14,2) DEFAULT 0,
            igst_amount NUMERIC(14,2) DEFAULT 0,
            total_amount NUMERIC(14,2),
            invoice_date DATE,
            pdf_url TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_invoice_number ON gst_invoices(invoice_number)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_invoice_payment ON gst_invoices(payment_id)
    """))

    # --- Chart of Accounts ---
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS chart_of_accounts (
            id BIGSERIAL PRIMARY KEY,
            account_code VARCHAR(50) UNIQUE,
            account_name VARCHAR(255),
            account_type VARCHAR(50),
            parent_account_id BIGINT,
            is_active BOOLEAN DEFAULT TRUE
        );
    """))

    # --- Accounting Ledger (double-entry) ---
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS accounting_ledger (
            id BIGSERIAL PRIMARY KEY,
            transaction_reference VARCHAR(100),
            account_code VARCHAR(50),
            account_name VARCHAR(255),
            debit_amount NUMERIC(14,2) DEFAULT 0,
            credit_amount NUMERIC(14,2) DEFAULT 0,
            narration TEXT,
            transaction_date TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_acct_ledger_ref ON accounting_ledger(transaction_reference)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_acct_ledger_code ON accounting_ledger(account_code)
    """))

    # --- Financial Audit Logs ---
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS financial_audit_logs (
            id BIGSERIAL PRIMARY KEY,
            module_name VARCHAR(100),
            reference_id BIGINT,
            action_type VARCHAR(100),
            old_data JSONB,
            new_data JSONB,
            performed_by UUID,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_fin_audit_module ON financial_audit_logs(module_name)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_fin_audit_ref ON financial_audit_logs(reference_id)
    """))

    # --- Seed default Chart of Accounts ---
    conn.execute(sa.text("""
        INSERT INTO chart_of_accounts (account_code, account_name, account_type, is_active) VALUES
            ('1000', 'Cash', 'ASSET', TRUE),
            ('1100', 'Bank', 'ASSET', TRUE),
            ('1200', 'Customer Receivable', 'ASSET', TRUE),
            ('1300', 'Partner Advance', 'ASSET', TRUE),
            ('2000', 'GST Payable', 'LIABILITY', TRUE),
            ('3000', 'Sales Revenue', 'REVENUE', TRUE),
            ('4000', 'Commission Revenue', 'REVENUE', TRUE),
            ('5000', 'Refund Expense', 'EXPENSE', TRUE)
        ON CONFLICT (account_code) DO NOTHING;
    """))


def downgrade() -> None:
    conn = op.get_bind()
    for t in [
        "financial_audit_logs", "accounting_ledger", "chart_of_accounts",
        "gst_invoices", "settlement_items", "settlements",
        "wallet_recharges", "wallet_ledger", "wallets",
        "refunds", "payment_transactions", "payments",
    ]:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {t} CASCADE"))
