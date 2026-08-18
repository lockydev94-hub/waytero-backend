from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_otp,
    hash_password,
    verify_password,
)


def test_password_hash_round_trip() -> None:
    hashed_password = hash_password("WayTero@2026")

    assert hashed_password != "WayTero@2026"
    assert verify_password("WayTero@2026", hashed_password) is True
    assert verify_password("WrongPassword@2026", hashed_password) is False


def test_generate_otp_uses_numeric_six_digits() -> None:
    otp = generate_otp()

    assert len(otp) == 6
    assert otp.isdigit()


def test_access_token_round_trip_preserves_auth_context() -> None:
    token = create_access_token(
        {
            "sub": "user-123",
            "role": "PARTNER",
            "roles": ["PARTNER"],
            "permissions": ["auth.self.read"],
            "session_id": "session-123",
        }
    )

    payload = decode_access_token(token)

    assert payload is not None
    assert payload["sub"] == "user-123"
    assert payload["roles"] == ["PARTNER"]
    assert payload["permissions"] == ["auth.self.read"]
    assert payload["type"] == "access"
