"""
Route wiring for the admin session-policy endpoints (Admin Portal §39).

The rules themselves — password re-entry, attempt counting, the reduce-only
idle time — are proven against the real service in
tests/unit/test_mpin_service.py. What this file pins is the contract the admin
portal consumes: the response envelope, the shape the schema refuses before it
ever reaches the database, and that a PIN never comes back out of an endpoint.
"""
from datetime import datetime

POLICY_KEYS = {
    "has_mpin",
    "has_password",
    "idle_minutes",
    "idle_lock_minutes",
    "idle_default_minutes",
    "daily_logout_hour",
    "timezone",
    "server_now",
    "mpin_locked_until",
    "mpin_attempts_remaining",
    "mpin_min_length",
    "access_token_expires_in",
    "session_started_at",
    "session_expires_at",
}


def test_session_config_returns_the_whole_policy(client) -> None:
    response = client.get("/api/v1/auth/admin/session-config")

    assert response.status_code == 200
    body = response.json()
    assert POLICY_KEYS <= set(body)
    # The client counts the daily-logout hour against this, not its own clock.
    datetime.fromisoformat(body["server_now"].replace("Z", "+00:00"))


def test_mpin_set_is_rejected_before_the_database_for_a_five_digit_pin(client) -> None:
    response = client.post(
        "/api/v1/auth/admin/mpin/set",
        json={"mpin": "12345", "confirm_mpin": "12345", "current_password": "x"},
    )

    assert response.status_code == 422


def test_mpin_set_rejects_two_halves_that_do_not_match(client) -> None:
    response = client.post(
        "/api/v1/auth/admin/mpin/set",
        json={"mpin": "248157", "confirm_mpin": "248158", "current_password": "x"},
    )

    assert response.status_code == 422


def test_mpin_otp_is_sent_to_the_signed_in_admin(client) -> None:
    """No body: the number comes from the session subject, so this route cannot
    be pointed at somebody else's mobile."""
    response = client.post("/api/v1/auth/admin/mpin/otp")

    assert response.status_code == 200
    body = response.json()
    assert body["mobile_number"]
    assert "otp" not in body


def test_mpin_set_returns_the_refreshed_policy_and_never_echoes_the_pin(client) -> None:
    response = client.post(
        "/api/v1/auth/admin/mpin/set",
        json={"mpin": "248157", "confirm_mpin": "248157", "current_password": "x"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["has_mpin"] is True
    assert "248157" not in response.text


def test_mpin_verify_unlocks_without_minting_tokens(client) -> None:
    response = client.post("/api/v1/auth/admin/mpin/verify", json={"mpin": "248157"})

    assert response.status_code == 200
    assert response.json()["data"]["unlocked"] is True
    # A lock is a screen lock, not a session end — nothing new to authenticate with.
    assert "access_token" not in response.text


def test_mpin_remove_accepts_a_body_on_delete(client) -> None:
    client.post(
        "/api/v1/auth/admin/mpin/set",
        json={"mpin": "248157", "confirm_mpin": "248157", "current_password": "x"},
    )

    response = client.request(
        "DELETE", "/api/v1/auth/admin/mpin", json={"current_password": "x"}
    )

    assert response.status_code == 200
    assert response.json()["data"]["has_mpin"] is False


def test_idle_lock_minutes_range_is_enforced_at_the_edge(client) -> None:
    assert client.put(
        "/api/v1/auth/admin/idle-lock-minutes", json={"idle_lock_minutes": 0}
    ).status_code == 422
    assert client.put(
        "/api/v1/auth/admin/idle-lock-minutes", json={"idle_lock_minutes": 241}
    ).status_code == 422


def test_idle_lock_null_inherits_the_default(client) -> None:
    response = client.put(
        "/api/v1/auth/admin/idle-lock-minutes", json={"idle_lock_minutes": None}
    )

    assert response.status_code == 200
    assert response.json()["data"]["idle_minutes"] == 5
