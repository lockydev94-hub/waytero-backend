-- ============================================================
-- WayTero — Direct DB Check & Fix for CB-20260802192736-0B2931
-- Run: psql -U waytero_user -d waytero_db -f scripts/fix_cb_direct.sql
-- ============================================================

-- STEP 1: Show current state
SELECT
    cb.id,
    cb.booking_number,
    cb.booking_status,
    cb.payment_mode,
    cb.cash_pending_at,
    cb.invoice_number,
    cb.final_amount,
    mb.payment_status AS master_payment_status
FROM cab_bookings cb
JOIN master_bookings mb ON mb.id = cb.master_booking_id
WHERE cb.booking_number = 'CB-20260802192736-0B2931';

-- STEP 2: Show all timeline events for this booking
SELECT
    bt.event_type,
    bt.event_description,
    bt.event_timestamp
FROM booking_timelines bt
JOIN master_bookings mb ON mb.id = bt.master_booking_id
JOIN cab_bookings cb ON cb.master_booking_id = mb.id
WHERE cb.booking_number = 'CB-20260802192736-0B2931'
ORDER BY bt.event_timestamp DESC;

-- STEP 3: Fix — set payment_mode, cash_pending_at, master payment_status, invoice
-- Only runs if payment_mode is still NULL
DO $$
DECLARE
    v_cb_id        BIGINT;
    v_mb_id        BIGINT;
    v_invoice      TEXT;
    v_max_seq      INT;
    v_new_invoice  TEXT;
BEGIN
    SELECT cb.id, cb.master_booking_id, cb.invoice_number
    INTO   v_cb_id, v_mb_id, v_invoice
    FROM   cab_bookings cb
    WHERE  cb.booking_number = 'CB-20260802192736-0B2931'
      AND  cb.payment_mode IS NULL;

    IF v_cb_id IS NULL THEN
        RAISE NOTICE 'No fix needed — payment_mode is already set for CB-20260802192736-0B2931';
        RETURN;
    END IF;

    -- Fix payment_mode + cash_pending_at on cab_booking
    UPDATE cab_bookings
    SET    payment_mode     = 'CASH',
           cash_pending_at  = 'DRIVER'
    WHERE  id = v_cb_id;

    RAISE NOTICE 'Set payment_mode=CASH, cash_pending_at=DRIVER on cab_booking id=%', v_cb_id;

    -- Fix master_booking payment_status
    UPDATE master_bookings
    SET    payment_status = 'PAID'
    WHERE  id = v_mb_id
      AND  payment_status <> 'PAID';

    RAISE NOTICE 'Set master_booking payment_status=PAID for mb id=%', v_mb_id;

    -- Generate invoice_number if missing
    IF v_invoice IS NULL THEN
        SELECT COALESCE(MAX(CAST(SPLIT_PART(invoice_number, '-', 4) AS INTEGER)), 0)
        INTO   v_max_seq
        FROM   cab_bookings
        WHERE  invoice_number IS NOT NULL
          AND  invoice_number LIKE 'WT-INV-%';

        v_new_invoice := 'WT-INV-' || TO_CHAR(NOW(), 'YYYYMM') || '-' || LPAD((v_max_seq + 1)::TEXT, 5, '0');

        UPDATE cab_bookings
        SET    invoice_number = v_new_invoice
        WHERE  id = v_cb_id;

        RAISE NOTICE 'Generated invoice_number=% for cb id=%', v_new_invoice, v_cb_id;
    ELSE
        RAISE NOTICE 'Invoice already exists: %', v_invoice;
    END IF;

    -- Log repair to timeline
    INSERT INTO booking_timelines
        (master_booking_id, event_type, event_description, event_timestamp)
    VALUES (
        v_mb_id,
        'PAYMENT_STATE_REPAIRED',
        'Manual SQL repair: payment_mode set to CASH, cash_pending_at=DRIVER. '
        'Booking CB-20260802192736-0B2931 had PAYMENT_CASH_COLLECTED in timeline but payment_mode was NULL.',
        NOW()
    );

    RAISE NOTICE 'Timeline event PAYMENT_STATE_REPAIRED inserted.';
    RAISE NOTICE 'Fix complete. Partner portal will now show Collect Payment DISABLED.';
END $$;

-- STEP 4: Confirm final state
SELECT
    cb.booking_number,
    cb.booking_status,
    cb.payment_mode,
    cb.cash_pending_at,
    cb.invoice_number,
    mb.payment_status AS master_payment_status
FROM cab_bookings cb
JOIN master_bookings mb ON mb.id = cb.master_booking_id
WHERE cb.booking_number = 'CB-20260802192736-0B2931';
