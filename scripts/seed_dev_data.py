#!/usr/bin/env python3
# ============================================================
# WAYTERO — DEVELOPMENT DATA SEEDER (one entry point)
# File: scripts/seed_dev_data.py
# Run: python -m scripts.seed_dev_data            (from Backend/)
#      python -m scripts.seed_dev_data --with-demo
#
# Runs the idempotent seeders a fresh dev database needs after
# `alembic upgrade head`. Every seeder is safe to re-run: none of them
# overwrites a row an admin has already edited.
#
# Reference data (roles, cities, service types, vehicle categories,
# pricing/GST system_configurations, CMS section variants) is seeded by
# the migrations themselves — it is not repeated here.
#
# Demo content is opt-in. This installation sells CAB and TOUR only, so the
# chain is: create the demo partner with those two grants (the way the admin
# dashboard does), then let the supply seeders write under it. Hotels and
# offers are intentionally absent — HOTEL is not a granted service here, and
# an offer requires an uploaded image_url, which the admin owns.
# ============================================================

import argparse
import os
import subprocess
import sys

# Windows consoles default to cp1252; the em dashes below would otherwise
# raise UnicodeEncodeError on print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (module, description) — order matters: the admin must exist before the
# wallet/legal seeders reference it, and CMS rows are created by 0044.
CORE_SEEDERS = [
    ("scripts.seed_admin", "SUPER_ADMIN login (9000000001 / Admin@WayTero1)"),
    ("scripts.seed_cms_defaults", "CMS header/footer defaults"),
    ("scripts.seed_legal_content", "Legal page bodies"),
    ("scripts.seed_wallets", "Wallets for every partner/customer"),
]

DEMO_SEEDERS = [
    # Must come first: every other supply seeder checks for an ACTIVE partner
    # holding the matching partner_services grant, and a fresh DB has none.
    ("scripts.seed_demo_partner", "Demo partner (ACTIVE, CAB + TOUR grants)"),
    ("scripts.seed_odisha_tours", "Demo tour packages (10, under the TOUR grant)"),
    ("scripts.seed_odisha_blogs", "Demo blog posts (needs an admin user)"),
    ("scripts.seed_cab_routes_odisha", "Popular cab routes (Odisha corridors)"),
    ("scripts.seed_demo_fleet", "Cab fleet: 4 vehicles + 4 drivers, assigned"),
    ("scripts.seed_homepage_demo_content", "Homepage bands (services, stats, partners)"),
]


def run_seeder(module: str, description: str, optional: bool) -> bool:
    """Run one seeder in its own interpreter. Returns True on success."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", module],
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] {module} — {description}")
        return False

    if result.returncode == 0:
        print(f"  [OK]   {module} — {description}")
        return True

    label = "SKIP" if optional else "FAIL"
    print(f"  [{label}] {module} — {description}")
    # Only the tail matters: these scripts print the failing precondition
    # last (e.g. "no ACTIVE partner holds a TOUR grant").
    tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
    for line in tail:
        print(f"         {line}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the WayTero dev database")
    parser.add_argument(
        "--with-demo",
        action="store_true",
        help="Also seed demo supply (hotels, tours, blogs, offers, cab routes).",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  WayTero — seeding development data")
    print("=" * 60)

    failures = 0
    for module, description in CORE_SEEDERS:
        if not run_seeder(module, description, optional=False):
            failures += 1

    if args.with_demo:
        print("\n  Demo content:")
        for module, description in DEMO_SEEDERS:
            run_seeder(module, description, optional=True)

    print("=" * 60)
    if failures:
        print(f"  {failures} core seeder(s) failed — see above.")
        return 1
    print("  Development data is ready.")
    print("  Admin login: 9000000001 / Admin@WayTero1")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
