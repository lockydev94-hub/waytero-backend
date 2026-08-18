def test_send_otp_endpoint(client) -> None:
    response = client.post("/api/v1/auth/send-otp", json={"mobile": "9876543210"})

    assert response.status_code == 200
    assert response.json()["mobile_number"] == "9876543210"


def test_customer_login_endpoint(client) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"mobile": "9876543210", "otp": "123456"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["user_type"] == "CUSTOMER"


def test_driver_login_endpoint(client) -> None:
    response = client.post(
        "/api/v1/auth/driver/login",
        json={"mobile": "9876543210", "otp": "123456"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["user_type"] == "DRIVER"


def test_partner_password_login_endpoint(client) -> None:
    response = client.post(
        "/api/v1/auth/partner/login",
        json={"mobile": "9876543210", "password": "WayTero@2026"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["user_type"] == "PARTNER"


def test_refresh_endpoint(client) -> None:
    response = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": "refresh-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["access_token"] == "refreshed::refresh-token"
    assert payload["roles"] == ["PARTNER"]


def test_session_listing_endpoint(client) -> None:
    response = client.get("/api/v1/auth/sessions")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["sessions"]) == 1
    assert payload["sessions"][0]["device_name"] == "Chrome"


def test_password_reset_flow_endpoints_exist(client) -> None:
    forgot_response = client.post(
        "/api/v1/auth/forgot-password",
        json={"identifier": "ops@waytero.com"},
    )
    reset_response = client.post(
        "/api/v1/auth/reset-password",
        json={
            "reset_token": "reset-token",
            "otp": "123456",
            "new_password": "WayTero@2026",
            "confirm_password": "WayTero@2026",
        },
    )

    assert forgot_response.status_code == 200
    assert reset_response.status_code == 200


def test_me_endpoint_returns_profile(client) -> None:
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["roles"] == ["PARTNER"]
