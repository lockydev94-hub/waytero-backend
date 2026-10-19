"""
ADMIN_* config keys are super-admin-only writes.

/admin/settings is gated require_roles(*ALL_STAFF), so without the prefix guard
a CCO could move ADMIN_DAILY_LOGOUT_HOUR (the hour every admin is force-logged
out) or weaken ADMIN_MPIN_MIN_LENGTH. The guard lives in the service, not the
route, so any future caller inherits it, and it fails closed when the caller is
unknown.
"""
import pytest

from app.core.exceptions import PermissionDeniedException
from app.modules.admin.services import SystemConfigService

_guard = SystemConfigService._reject_admin_policy_write


def test_non_super_admin_cannot_write_admin_policy_key() -> None:
    with pytest.raises(PermissionDeniedException):
        _guard("ADMIN_DAILY_LOGOUT_HOUR", {"roles": ["CCO"]})


def test_missing_caller_fails_closed() -> None:
    with pytest.raises(PermissionDeniedException):
        _guard("ADMIN_MPIN_MIN_LENGTH", None)


def test_super_admin_can_write_admin_policy_key() -> None:
    _guard("ADMIN_IDLE_DEFAULT_MINUTES", {"roles": ["SUPER_ADMIN"]})


def test_other_keys_are_untouched_by_the_guard() -> None:
    # Staff screens legitimately write their own keys (GST flag, support text).
    _guard("GST_ENABLED", {"roles": ["FINANCE_MANAGER"]})
    _guard("SUPPORT_EMAIL", {"roles": ["CCO"]})


def test_role_string_alone_is_enough() -> None:
    # Older JWT payloads carry `role` instead of the `roles` list.
    _guard("ADMIN_MPIN_MAX_ATTEMPTS", {"role": "SUPER_ADMIN"})
    with pytest.raises(PermissionDeniedException):
        _guard("ADMIN_MPIN_MAX_ATTEMPTS", {"role": "PARTNER"})
