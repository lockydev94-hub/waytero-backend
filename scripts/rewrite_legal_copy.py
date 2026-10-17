#!/usr/bin/env python3
# ============================================================
# WAYTERO — GUARDED REWRITE OF THE SEEDED PAGE COPY
# File: scripts/rewrite_legal_copy.py
#
# The five `LEGAL_PAGE_*` rows seeded by `scripts.seed_legal_content` up to
# Batch A describe things the platform does not do: refunds credited
# "instantly", data "encrypted at rest", "PCI-DSS compliant" processors,
# "analytics cookies" the site never sets, partners "bound by data-processing
# agreements", deletion "within 30 days", "track your cab live".
#
# Re-running the seeder cannot fix them — it is `ON CONFLICT DO NOTHING`, so a
# row that exists keeps its text forever. This script upgrades a row to
# `legal_page_content.PAGE_BODIES` ONLY when the stored value is byte-identical
# to `seed_legal_content.ORIGINAL_SEED`, which is how it knows nobody has
# edited it. Anything an admin touched is left alone and reported.
#
# Dry run by default; `--apply` writes.
#
#     python -m scripts.rewrite_legal_copy          # what would change
#     python -m scripts.rewrite_legal_copy --apply  # change it
#
# Run once locally and once on the VPS after deploying. It is deliberately NOT
# wired into lifespan: rewriting published copy is a decision, not a startup
# step.
#
# The replacement text is also what customer-web carries as each page's
# built-in fallback, so the page reads the same whether the row is live,
# unpublished, or the API is down. Change one, change the other — the drift
# check in the round-6 verification diffs the two rendered builds.
# ============================================================

import argparse
import asyncio
import json
import os
import sys

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)  # noqa: E402

from app.core.config import settings  # noqa: E402
from scripts.legal_page_content import PAGE_BODIES  # noqa: E402
from scripts.seed_legal_content import ORIGINAL_SEED  # noqa: E402


async def main(apply: bool) -> int:
    engine = create_async_engine(settings.DATABASE_URL)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    changed = skipped = missing = same = 0
    async with Session() as db:
        for key, new_page in PAGE_BODIES.items():
            stored = (
                await db.execute(
                    text(
                        "SELECT config_value FROM system_configurations WHERE config_key = :k"
                    ),
                    {"k": key},
                )
            ).scalar()
            if stored is None:
                print(
                    f"  {key:<34} MISSING — not seeded yet; the seeder will insert the new copy"
                )
                missing += 1
                continue
            candidate = json.dumps(new_page, ensure_ascii=False)
            if stored == candidate:
                print(f"  {key:<34} UNCHANGED — already carries the rewritten copy")
                same += 1
                continue
            original = json.dumps(ORIGINAL_SEED[key], ensure_ascii=False)
            if stored != original:
                print(
                    f"  {key:<34} SKIPPED — value differs from the original seed (admin-edited)"
                )
                skipped += 1
                continue
            print(f"  {key:<34} {'REWRITE' if apply else 'WOULD REWRITE'}")
            changed += 1
            if apply:
                await db.execute(
                    text(
                        "UPDATE system_configurations "
                        "SET config_value = :v, updated_at = NOW() WHERE config_key = :k"
                    ),
                    {"v": candidate, "k": key},
                )
        if apply:
            await db.commit()
    await engine.dispose()
    print(
        f"\n{'applied' if apply else 'dry run'}: {changed} to rewrite, {same} already current, "
        f"{skipped} admin-edited (left alone), {missing} not seeded."
    )
    if not apply and changed:
        print("Re-run with --apply to write these changes.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Upgrade the seeded LEGAL_PAGE_* copy to scripts.legal_page_content.PAGE_BODIES."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the rewrite. Without it the script only reports.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.apply)))
