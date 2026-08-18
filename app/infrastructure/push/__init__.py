# ============================================================
# WAY TERO — FIREBASE ADMIN SDK WRAPPER
# File: app/infrastructure/push/firebase_client.py
# Doc Ref: BRD Part 7 §155 — FCM push channel
#
# Single shared `firebase_admin` app. Initialised lazily on first use
# from the api_integrations row with service_type='FIREBASE'.
#
# Admin pastes the full Firebase Admin SDK JSON (the file Google gives
# from Project Settings -> Service Accounts -> Generate new private key)
# into Settings -> Notifications. We store the JSON as-is in
# api_integrations.configuration so it round-trips perfectly, and use
# firebase_admin's `credentials.Certificate(dict)` API.
#
# If no FIREBASE row is configured, or it can't be parsed, every send
# call returns False and logs — callers must handle the no-op case.
# ============================================================

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("waytero.firebase")

# Lazy imports — firebase_admin is heavy and may be missing on dev
# machines that only run unit tests.
try:
    import firebase_admin
    from firebase_admin import credentials, messaging

    # `firebase_admin.auth` is a submodule that's not auto-imported by
    # `import firebase_admin`; it needs to be pulled in explicitly before
    # we can call `firebase_admin.auth.verify_id_token` etc.
    import firebase_admin.auth  # noqa: F401

    _FIREBASE_AVAILABLE = True
except Exception as exc:  # pragma: no cover - import-time guard
    _FIREBASE_AVAILABLE = False
    logger.warning("firebase_admin import failed: %s", exc)


_lock = threading.Lock()
_initialised_app_name: str | None = None


async def _load_firebase_config(db: AsyncSession) -> dict[str, Any] | None:
    """Read the FIREBASE row from api_integrations and return its JSON dict."""
    # Imported here to avoid a circular import at module load time
    # (admin.models imports Base, which is fine — but keeping this local
    # makes the file easy to test in isolation).
    from app.modules.admin.models import ApiIntegration

    row = (
        await db.execute(
            select(ApiIntegration).where(
                ApiIntegration.service_type == "FIREBASE",
                ApiIntegration.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if not row or not row.configuration:
        return None
    cfg = row.configuration
    if isinstance(cfg, dict) and cfg:
        return cfg
    return None


async def _ensure_app(db: AsyncSession) -> bool:
    """Lazy-init the firebase app. Returns True if a usable app exists."""
    global _initialised_app_name
    if not _FIREBASE_AVAILABLE:
        return False
    with _lock:
        if _initialised_app_name and _initialised_app_name in firebase_admin._apps:
            return True
        cfg = await _load_firebase_config(db)
        if not cfg:
            return False
        try:
            cred = credentials.Certificate(cfg)
            app = firebase_admin.initialize_app(cred, name="waytero-fcm")
            _initialised_app_name = app.name
            logger.info("firebase.initialised project_id=%s", cfg.get("project_id"))
            return True
        except Exception as exc:
            logger.error("firebase.initialise_failed err=%s", exc)
            return False


def reset_for_testing() -> None:
    """Drop the cached firebase app — only used by tests when the FIREBASE
    config row is inserted mid-test."""
    global _initialised_app_name
    with _lock:
        if _FIREBASE_AVAILABLE and _initialised_app_name in firebase_admin._apps:
            try:
                firebase_admin.delete_app(firebase_admin.get_app(_initialised_app_name))
            except Exception:  # pragma: no cover
                pass
        _initialised_app_name = None


async def send_fcm_to_tokens(
    db: AsyncSession,
    tokens: list[str],
    title: str,
    body: str,
    data: dict[str, str] | None = None,
) -> dict[str, int]:
    """
    Best-effort multicast. Returns counts of success / failure / total.

    `data` must be str->str (FCM requirement). We str() everything that
    isn't already a string.

    If Firebase isn't configured we silently no-op and return
    {'success': 0, 'failure': 0, 'skipped': len(tokens)}.
    """
    if not tokens:
        return {"success": 0, "failure": 0, "skipped": 0}
    if not await _ensure_app(db):
        return {"success": 0, "failure": 0, "skipped": len(tokens)}

    safe_data = {k: str(v) for k, v in (data or {}).items()}
    msg = messaging.MulticastMessage(
        notification=messaging.Notification(title=title, body=body),
        data=safe_data or None,
        tokens=tokens,
    )

    try:
        resp = messaging.send_each_for_multicast(msg)
        return {
            "success": int(resp.success_count),
            "failure": int(resp.failure_count),
            "skipped": 0,
        }
    except Exception as exc:  # pragma: no cover - FCM transport failures
        logger.error("firebase.send_failed err=%s", exc)
        return {"success": 0, "failure": len(tokens), "skipped": 0}


# `asyncio` is referenced for callers that want to `await send_fcm_to_tokens`
# without importing asyncio themselves. Keep the unused symbol so
# `import firebase_client as f; await f.send_fcm_to_tokens(...)` doesn't
# trigger a "no awaitable" lint.
_ = asyncio


# ============================================================
# Firebase ID-token verification (web sign-in → backend JWT bridge)
# Doc Ref: Customer Web Google sign-in flow
#
# Used by auth.api.firebase_sign_in. The web client calls Firebase Web SDK
# signInWithPopup(GoogleAuthProvider), then POSTs the user's ID token here.
# We verify it with the Admin SDK and hand back the user's identity claims
# (uid, email, name, picture). The auth service then finds-or-creates the
# local customer user.
#
# Raises a runtime exception if the project isn't configured (admin has
# not yet pasted the Admin SDK JSON into Settings → Notifications). The
# auth service translates that to a 503 / "auth provider unavailable".
# ============================================================
class FirebaseAuthUnavailableError(RuntimeError):
    """Raised when Firebase Admin SDK isn't initialised (FIREBASE row missing)."""


class InvalidFirebaseIdTokenError(ValueError):
    """Raised when the supplied ID token fails signature/audience/expiry checks."""


async def verify_firebase_id_token(db: AsyncSession, id_token: str) -> dict[str, Any]:
    """Verify a Firebase ID token and return the decoded claims dict.

    Returns a dict with at least: uid, email (may be None), name (may be None),
    picture (may be None), email_verified (bool).
    """
    if not _FIREBASE_AVAILABLE:
        raise FirebaseAuthUnavailableError(
            "firebase_admin SDK not installed on this server"
        )
    if not await _ensure_app(db):
        raise FirebaseAuthUnavailableError(
            "Firebase Admin SDK is not configured. "
            "Paste the service-account JSON in Settings → Notifications."
        )

    # _ensure_app() initialises a named app ("waytero-fcm") so it can coexist
    # with any default app another module might want to create. We must hand
    # that exact app to verify_id_token — without it the SDK raises
    # "The default Firebase app does not exist".
    assert _initialised_app_name is not None  # guaranteed by _ensure_app=True
    app = firebase_admin.get_app(_initialised_app_name)

    try:
        # firebase_admin.auth.verify_id_token is sync (network call); run in
        # default threadpool so we don't block the event loop.
        claims: dict[str, Any] = await asyncio.get_running_loop().run_in_executor(
            None, firebase_admin.auth.verify_id_token, id_token, app
        )
    except firebase_admin.auth.ExpiredIdTokenError as exc:
        raise InvalidFirebaseIdTokenError("Firebase ID token expired") from exc
    except firebase_admin.auth.InvalidIdTokenError as exc:
        raise InvalidFirebaseIdTokenError("Firebase ID token is invalid") from exc
    except Exception as exc:  # pragma: no cover - network/transport surprises
        raise InvalidFirebaseIdTokenError(
            f"Firebase verification failed: {exc}"
        ) from exc

    return {
        "uid": claims.get("uid") or claims.get("user_id") or "",
        "email": claims.get("email"),
        "name": claims.get("name"),
        "picture": claims.get("picture"),
        "email_verified": bool(claims.get("email_verified", False)),
    }
