# ============================================================
# WAYTERO — MIGRATION 0067
# SEO-friendly tour package slugs
#
# Tour URLs currently look like
#   /tours/jagannath-dham-bhubaneswar-to-puri-tp-20260913-0001
# because _slug() appended the internal package code. Codes in URLs hurt
# SEO/CTR, so slugs become name-only with a -2/-3 suffix on collision.
# This migration rewrites existing slugs the same way (idempotent).
#
# Old URLs 301-redirect server-side: /tours/[slug] resolves the package
# by slug OR package_code, so links carrying the old code-slug still work
# and are redirected to the new canonical URL in customer-web.
#
# Run: alembic upgrade head
# ============================================================

from sqlalchemy import text

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def _slugify(name: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "tour"


def upgrade() -> None:
    conn = op.get_bind()

    rows = conn.execute(
        text("SELECT id, package_name, slug FROM tour_packages")
    ).fetchall()

    seen: set[str] = set()
    updates: list[tuple[str, int]] = []
    for pid, name, current_slug in rows:
        base = _slugify(name)
        candidate = base
        suffix = 2
        # Idempotent: a slug that already matches the clean form (or its
        # numbered variants claimed by this pass) is kept as-is.
        while candidate in seen or (
            conn.execute(
                text(
                    "SELECT 1 FROM tour_packages WHERE slug = :s AND id != :id LIMIT 1"
                ),
                {"s": candidate, "id": pid},
            ).scalar()
            and candidate != current_slug
            and not candidate.startswith(current_slug)
        ):
            # NOTE: simple uniqueness loop; the startswith guard keeps a row
            # that was already migrated (slug == clean form) untouched.
            candidate = f"{base}-{suffix}"
            suffix += 1
        seen.add(candidate)
        if candidate != current_slug:
            updates.append((candidate, pid))

    for new_slug, pid in updates:
        conn.execute(
            text("UPDATE tour_packages SET slug = :s, updated_at = NOW() WHERE id = :id"),
            {"s": new_slug, "id": pid},
        )

    print(f"migration 0067: rewrote {len(updates)} tour package slug(s)")


def downgrade() -> None:
    # One-way data improvement; nothing to restore.
    pass
