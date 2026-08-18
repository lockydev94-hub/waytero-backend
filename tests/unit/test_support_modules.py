import pytest

from app.core.dependencies import require_permission, require_roles
from app.core.exceptions import (
    AuthenticationException,
    DuplicateResourceException,
    FinancialException,
    PermissionDeniedException,
    ResourceNotFoundException,
    ServiceUnavailableException,
    ValidationException,
)
from app.shared.responses.base import error_response, success_response


@pytest.mark.asyncio
async def test_require_roles_allows_matching_role() -> None:
    checker = require_roles("ADMIN", "SUPER_ADMIN")

    payload = await checker({"role": "ADMIN", "roles": ["ADMIN"]})

    assert payload["role"] == "ADMIN"


@pytest.mark.asyncio
async def test_require_permission_rejects_missing_permission() -> None:
    checker = require_permission("portal.admin")

    with pytest.raises(Exception):
        await checker({"permissions": ["auth.self.read"]})


def test_duplicate_resource_exception_with_field() -> None:
    exc = DuplicateResourceException("User", field="mobile")

    assert exc.status_code == 409
    assert "mobile" in exc.message


def test_core_exceptions_expose_expected_status_codes() -> None:
    not_found = ResourceNotFoundException("Booking", "WT-1")
    validation = ValidationException("Invalid payload")
    permission_denied = PermissionDeniedException()
    authentication = AuthenticationException()
    unavailable = ServiceUnavailableException("sms")
    financial = FinancialException("Insufficient balance")

    assert not_found.status_code == 404
    assert validation.status_code == 400
    assert permission_denied.status_code == 403
    assert authentication.status_code == 401
    assert unavailable.status_code == 503
    assert financial.status_code == 422


def test_response_helpers_produce_standard_shape() -> None:
    assert success_response("ok", {"id": 1}) == {
        "success": True,
        "message": "ok",
        "data": {"id": 1},
    }
    assert error_response("bad", code="ERR") == {
        "success": False,
        "message": "bad",
        "errors": [],
        "code": "ERR",
    }
