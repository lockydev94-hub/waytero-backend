#!/usr/bin/env python3
# ============================================================
# WAYTERO — EXPORT THE PAGE BODIES FOR CUSTOMER-WEB
# File: scripts/export_page_fallbacks.py
#
# Writes `Frontend/customer-web/src/content/pageBodies.ts` from
# `scripts/legal_page_content.PAGE_BODIES`, so a page's built-in fallback and
# the row the seeder inserts are the same bytes rather than the same
# intentions. Both are needed: the database answers when it is up, and the
# literal answers when an admin has unpublished the body or the API is down.
#
# Run it after editing PAGE_BODIES, from the Backend directory:
#
#     python -m scripts.export_page_fallbacks
#
# Manual by design — a build step that writes into another repository would
# fail as a surprise somewhere it is not wanted.
# ============================================================

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.legal_page_content import PAGE_BODIES  # noqa: E402

TARGET = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        os.pardir,
        "Frontend",
        "customer-web",
        "src",
        "content",
        "pageBodies.ts",
    )
)

HEADER = """\
/* ============================================================
   BUILT-IN PAGE BODIES — GENERATED FILE, DO NOT EDIT.

   Produced by Backend/scripts/export_page_fallbacks.py from
   Backend/scripts/legal_page_content.PAGE_BODIES, which is also what
   scripts.seed_legal_content inserts into system_configurations under
   LEGAL_PAGE_{SLUG}. Edit the copy there and re-run the exporter; the two
   have to move together, because a page renders whichever of the two it is
   standing on and the reader cannot tell them apart.

   A page uses this only when its database row is missing, unpublished, has
   no sections, or the API is unreachable — see src/lib/pageBody.ts. The
   dates are dropped in that case: a fallback has no "last updated" anyone
   can stand behind.
   ============================================================ */

import type { LegalPage } from "@/services/publicCms";

export const PAGE_BODIES: Record<string, LegalPage> = """


def main() -> int:
    by_slug = {page["slug"]: page for page in PAGE_BODIES.values()}
    payload = json.dumps(by_slug, indent=2, ensure_ascii=False)
    body = payload.rstrip() + ";\n"
    with open(TARGET, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(HEADER + body)
    print(f"wrote {len(by_slug)} page bodies to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
