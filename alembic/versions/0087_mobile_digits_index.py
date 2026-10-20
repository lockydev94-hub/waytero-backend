"""0087_mobile_digits_index — one phone number, one account, found by ten digits

Revision ID: 0087
Revises: 0086

Doc Ref: Round 15 plan C2 (phone-desk lookup); BRD Part 6 §129 (customer care).

Why:
  The support desk opens a call by typing the customer's mobile. `/lookup`
  compared that string against `users.mobile_number` exactly, and the column
  stores whatever whoever wrote it typed: `9876543210`, `+919876543210`,
  `09876543210` are the same person to a customer and different strings to
  Postgres. The answer to a number with a `+91` in it was "not found", and what
  happens after the desk believes that is a second account for an existing
  customer — booking history split across two profiles, coupons that are
  per-customer no longer counting the same person, and a wallet nobody can
  reconcile.

  So the lookup matches the last ten digits of the stored value, computed
  `app/shared/utilities/mobile.py:mobile_match_sql`. That expression is not
  index-usable as written by a `btree(mobile_number)` scan, and the desk hits it
  on every incoming call, which is the one path that must not get slower as the
  customer table grows. This index makes the ten digits as seekable as the raw
  string was.

  `idx_users_mobile_digits` is non-unique on purpose. `users_mobile_number_key`
  already enforces uniqueness on the raw string, and tightening it to the
  digits would be a change of contract on a column every user type writes:
  Firebase sign-ins store a `fb_<hex>` placeholder mobile that no phone number
  will ever collide with, and a unique digits index would put a synthetic
  placeholder in the way of a real customer's registration with a 500 instead of
  the 409 the desk can act on. Registering a duplicate is prevented by the
  digit check in `register_customer_by_admin`, which can explain itself; a
  constraint cannot.

  Nothing is backfilled or rewritten. Rows keep the exact string they were
  created with — reformatting live customer data to satisfy a search the
  platform used to get wrong is a bigger change than adding one index.

Idempotent: CREATE INDEX IF NOT EXISTS.
"""

from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_users_mobile_digits
            ON users (right(regexp_replace(mobile_number, '[^0-9]', '', 'g'), 10))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_users_mobile_digits")
