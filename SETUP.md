# WayTero Backend — Developer Setup Guide

## Prerequisites
- Python 3.12+ (3.13 also supported) — must be installed on THIS machine
- Node.js 20+ (for the three frontend apps)
- Docker Desktop running
- Git

---

## One-command start (recommended)

Double-click **`Scripts\setup\Dev-Start.bat`**, or from a terminal:

```powershell
.\Scripts\setup\dev-start.ps1
```

It performs, in order:

1. Checks Docker Desktop
2. Starts PostgreSQL 17 + Redis 7 (MinIO is attempted; skipped if the image is unavailable)
3. Waits for PostgreSQL to report healthy
4. Copies `Backend\.env` from `.env.example` if it is missing
5. Verifies `Backend\.venv` **by running it** — a `.venv` copied from another PC
   points at that PC's Python and is rebuilt automatically, then dependencies
   are installed from `requirements\development.txt`
6. Runs `alembic upgrade head` (creates every table on a fresh database)
7. Seeds development data via `python -m scripts.seed_dev_data`
8. Installs frontend `node_modules` where missing
9. Starts the backend on :8000 and waits for `/health`
10. Starts admin :3000, partner :3001, customer web :3002 and waits for each to serve
11. Opens the browser tabs that answered

Stop everything with **`Scripts\setup\Dev-Stop.bat`** (only WayTero processes are
touched — matched by command line, never kill-all).

---

## Seeded data

`Backend\scripts\seed_dev_data.py` runs these idempotent seeders:

| Seeder | What it creates |
|--------|-----------------|
| `scripts.seed_admin` | SUPER_ADMIN login `9000000001` / `Admin@WayTero1` |
| `scripts.seed_cms_defaults` | CMS header/footer defaults |
| `scripts.seed_legal_content` | Privacy / terms / refund / cookies / instructions page bodies |
| `scripts.seed_wallets` | Wallet row for every partner and customer |

Reference data (8 roles, 15 cities, 3 service types, 7 vehicle categories,
103 `system_configurations`, 18 CMS section variants) is created by the
migrations themselves — no separate script.

Demo website content is opt-in, because every supply seeder requires an
ACTIVE partner holding the matching `partner_services` grant, and a fresh
database has no partner:

```powershell
cd Backend
.venv\Scripts\python.exe -m scripts.seed_dev_data --with-demo
```

With `--with-demo` the following run in order:

| Seeder | What it creates |
|--------|-----------------|
| `scripts.seed_demo_partner` | ACTIVE partner **Maa Travels Pvt. Ltd** with **CAB + TOUR** grants, GST, wallet, rating, verification log |
| `scripts.seed_odisha_tours` | 10 ACTIVE tour packages with day-wise itineraries, inclusions/exclusions and price slabs |
| `scripts.seed_odisha_blogs` | 10 published blog posts with SEO fields |
| `scripts.seed_cab_routes_odisha` | 16 popular cab routes with distance, terms and vehicle-category links |
| `scripts.seed_demo_fleet` | 4 cab vehicles + 4 drivers, assigned and available |
| `scripts.seed_homepage_demo_content` | Homepage service cards, live stats, testimonials, partner names |

Images are deliberately left empty everywhere — the admin uploads them from
the dashboard. This installation sells **Cab + Tour** only, so hotels are not
seeded and the HOTEL grant is not granted.

Demo partner portal login: `9000000002` / `Waytero@15`.

The cab route seeder needs a distance per route. With no GOOGLE_MAPS key in
`api_integrations` it uses the road distances in `MANUAL_DISTANCES` and stores
`distance_source='MANUAL'` — the same thing an admin does by typing a distance
into the route editor.

---

## Admin login

```powershell
POST /api/v1/auth/admin/login/password
{"username": "9000000001", "password": "Admin@WayTero1"}
```

`username` accepts the 10-digit mobile number or the email
(`admin@waytero.dev`). There is no OTP on the password route; the OTP route is
`POST /api/v1/auth/admin/login/otp` after `POST /api/v1/auth/admin/send-otp`
(dev builds return `dev_otp` in the response).

---

## Manual steps (if you prefer)

```powershell
# 1. Infrastructure
cd Infrastructure\docker\development
docker compose up -d db redis

# 2. Virtual environment
cd ..\..\Backend
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements\development.txt

# 3. Schema + seed
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m scripts.seed_dev_data

# 4. API
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Verify: http://localhost:8000/docs

---

## Common errors & fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `did not find executable at 'C:\Users\<other-pc>\...\python.exe'` | `.venv` copied from another PC | Delete `Backend\.venv` — `dev-start.ps1` rebuilds it |
| `docker-compose not found` | Compose v2 only | Use `docker compose` (the scripts detect either) |
| `file access denied` on `/data`, MinIO restarting | Bitnami image runs as uid 1001 over a root-owned volume | Already handled: the compose service sets `user: root` |
| `pull access denied for minio/minio` | That Docker Hub image is no longer pullable (quay.io now needs auth) | Compose uses `bitnamilegacy/minio:latest`; MinIO stays optional in dev |
| `password authentication failed` | DB credentials differ | Keep `Backend\.env` in sync with `docker-compose.yml` (`waytero_user` / `waytero_dev_password`) |
| `UnicodeEncodeError: 'charmap'` in a seeder | Windows console codepage | Seeders reconfigure stdout to UTF-8; run them with the venv Python |
| `user_type_enum already exists` | Partial failed migration | `alembic downgrade base` then `alembic upgrade head` |
| Backend window shows `[Errno 10048]` | Port 8000 held by an old process | Run `Dev-Stop.bat`, then start again |

---

## Reset the database

```powershell
cd Backend
.venv\Scripts\python.exe -m alembic downgrade base
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m scripts.seed_dev_data
```

For a completely empty volume (drops all dev data):

```powershell
cd Infrastructure\docker\development
docker compose down -v
docker compose up -d db redis
```
