# WayTero Backend — Developer Setup Guide

## Prerequisites
- Python 3.13+
- Docker Desktop running
- Git

---

## Step 1 — Start Database & Redis (always run from Infrastructure folder)

```powershell
cd C:\MyWorkspace\WayTero\Infrastructure\docker\development
docker-compose up -d db redis
```

---

## Step 2 — Activate Virtual Environment (always from Backend folder)

```powershell
cd C:\MyWorkspace\WayTero\Backend
.venv\Scripts\Activate.ps1
```

> NOTE: The virtual environment folder is `.venv` (with a dot), NOT `venv`.
> If Activate.ps1 is blocked, run this once in PowerShell as Administrator:
> `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser`

---

## Step 3 — Run Database Migrations

```powershell
# Make sure .venv is activated (you will see (.venv) in your prompt)
cd C:\MyWorkspace\WayTero\Backend
alembic upgrade head
```

---

## Step 4 — Start the API Server

```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## Step 5 — Verify

Open browser: http://localhost:8000/docs

---

## Common Errors & Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `alembic not recognized` | venv not activated | Run `.venv\Scripts\Activate.ps1` first |
| `.\venv\Scripts\activate not recognized` | Wrong folder name | Use `.venv` not `venv` |
| `docker-compose not found` | Wrong directory | Run from `Infrastructure/docker/development/` |
| `password authentication failed` | Wrong DB credentials | Check `.env` matches `docker-compose.yml` |
| `user_type_enum already exists` | Partial failed migration | Run `alembic downgrade base` then `alembic upgrade head` |

---

## Reset Database (if needed)

```powershell
cd C:\MyWorkspace\WayTero\Backend
alembic downgrade base
alembic upgrade head
```
