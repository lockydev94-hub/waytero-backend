# ============================================================
# WAY TERO — HOTEL VERIFICATION TRANSITION TESTS
# File: tests/unit/test_hotel_transitions.py
# Doc Ref: BRD Part 4 §75-80, SRS Part 5 §170-176
#
# Every status change funnels through _transition_hotel, so these tests pin two
# things: that the transition map is enforced, and that exactly one audit log
# row is written per change. A bypassed transition is an unauditable one.
# ============================================================

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.exceptions import BusinessException, ValidationException
from app.modules.hotel.constants import (
    ACTION_HOTEL_APPROVED_OWN_RISK,
    ERR_INVALID_TRANSITION,
    HOTEL_STATUS_ACTIVE,
    HOTEL_STATUS_APPROVED,
    HOTEL_STATUS_BLOCKED,
    HOTEL_STATUS_DRAFT,
    HOTEL_STATUS_INACTIVE,
    HOTEL_STATUS_PENDING,
    HOTEL_STATUS_REJECTED,
    HOTEL_STATUS_SUSPENDED,
    HOTEL_STATUS_UNDER_REVIEW,
    VALID_HOTEL_TRANSITIONS,
)
from app.modules.hotel.services import HotelVerificationService

ACTOR = uuid4()


def _hotel(status=HOTEL_STATUS_DRAFT):
    return SimpleNamespace(
        id=1,
        status=status,
        submitted_at=None,
        approved_at=None,
        approved_by=None,
        activated_at=None,
        rejection_reason=None,
        is_own_risk_approved=False,
    )


class _StubDb:
    async def flush(self):
        return None

    async def execute(self, _stmt, _params=None):  # pragma: no cover - unused here
        raise AssertionError("no raw SQL expected in these paths")


def _service(hotel, *, can_submit=True, can_approve=True):
    service = HotelVerificationService(_StubDb())  # type: ignore[arg-type]
    logs: list = []

    async def get_by_id(_hid):
        return hotel

    async def add_log(log):
        logs.append(log)
        return log

    async def compute(_hid):
        return {
            "hotel_id": 1,
            "status": hotel.status,
            "completeness_percent": 100 if can_submit else 60,
            "can_submit": can_submit,
            "can_approve": can_approve,
            "checks": [
                {
                    "key": "images_uploaded",
                    "label": "At least 3 images with one primary",
                    "passed": can_submit,
                    "blocks_submit": True,
                    "blocks_approve": True,
                    "hint": None if can_submit else "2 image(s)",
                },
                {
                    "key": "documents_verified",
                    "label": "All mandatory documents verified",
                    "passed": can_approve,
                    "blocks_submit": False,
                    "blocks_approve": True,
                    "hint": None if can_approve else "Awaiting verification",
                },
            ],
        }

    service.repo.get_by_id = get_by_id  # type: ignore[method-assign]
    service.verification.add_log = add_log  # type: ignore[method-assign]
    service.readiness.compute = compute  # type: ignore[method-assign]
    return service, logs


@pytest.fixture(autouse=True)
def _stub_config(monkeypatch):
    """Own-risk approval is gated on a system_configurations flag. Default it on
    so the tests exercise the justification rule, not the feature switch."""

    async def get_config_bool(_db, _key, default=False):
        return True

    monkeypatch.setattr("app.modules.hotel.services.get_config_bool", get_config_bool)


class TestTransitionMap:
    def test_blocked_is_terminal(self):
        """Unblocking is a data-correction task, not a workflow step."""
        assert VALID_HOTEL_TRANSITIONS[HOTEL_STATUS_BLOCKED] == []

    def test_every_status_has_an_entry(self):
        for targets in VALID_HOTEL_TRANSITIONS.values():
            for target in targets:
                assert target in VALID_HOTEL_TRANSITIONS

    @pytest.mark.asyncio
    async def test_illegal_transition_is_refused_with_the_allowed_set(self):
        hotel = _hotel(HOTEL_STATUS_DRAFT)
        service, logs = _service(hotel)

        with pytest.raises(BusinessException) as exc:
            await service.activate(1, ACTOR)

        assert exc.value.code == ERR_INVALID_TRANSITION
        assert HOTEL_STATUS_PENDING in exc.value.message
        assert hotel.status == HOTEL_STATUS_DRAFT
        assert logs == []

    @pytest.mark.asyncio
    async def test_a_refused_transition_writes_no_audit_row(self):
        hotel = _hotel(HOTEL_STATUS_BLOCKED)
        service, logs = _service(hotel)

        with pytest.raises(BusinessException):
            await service.deactivate(1, ACTOR)
        assert logs == []


class TestSubmit:
    @pytest.mark.asyncio
    async def test_submit_moves_draft_to_pending_and_stamps_the_time(self):
        hotel = _hotel(HOTEL_STATUS_DRAFT)
        service, logs = _service(hotel)

        await service.submit(1, ACTOR)

        assert hotel.status == HOTEL_STATUS_PENDING
        assert hotel.submitted_at is not None
        assert len(logs) == 1
        assert logs[0].from_status == HOTEL_STATUS_DRAFT
        assert logs[0].to_status == HOTEL_STATUS_PENDING

    @pytest.mark.asyncio
    async def test_submit_is_refused_when_readiness_blocks_it(self):
        hotel = _hotel(HOTEL_STATUS_DRAFT)
        service, logs = _service(hotel, can_submit=False)

        with pytest.raises(BusinessException) as exc:
            await service.submit(1, ACTOR)

        # The message names the failing check so the admin is not left guessing.
        assert "At least 3 images with one primary" in exc.value.message
        assert hotel.status == HOTEL_STATUS_DRAFT
        assert logs == []

    @pytest.mark.asyncio
    async def test_resubmitting_after_rejection_clears_the_reason(self):
        hotel = _hotel(HOTEL_STATUS_REJECTED)
        hotel.rejection_reason = "Fire safety certificate expired"
        service, _ = _service(hotel)

        await service.submit(1, ACTOR)

        assert hotel.status == HOTEL_STATUS_PENDING
        assert hotel.rejection_reason is None


class TestApprove:
    @pytest.mark.asyncio
    async def test_approve_stamps_approver_and_time(self):
        hotel = _hotel(HOTEL_STATUS_UNDER_REVIEW)
        service, logs = _service(hotel)

        await service.approve(1, ACTOR, "Documents in order")

        assert hotel.status == HOTEL_STATUS_APPROVED
        assert hotel.approved_by == ACTOR
        assert hotel.approved_at is not None
        assert hotel.is_own_risk_approved is False
        assert len(logs) == 1

    @pytest.mark.asyncio
    async def test_approve_refused_while_documents_are_unverified(self):
        hotel = _hotel(HOTEL_STATUS_UNDER_REVIEW)
        service, _ = _service(hotel, can_approve=False)

        with pytest.raises(BusinessException) as exc:
            await service.approve(1, ACTOR)

        assert "All mandatory documents verified" in exc.value.message
        assert "own-risk" in exc.value.message
        assert hotel.status == HOTEL_STATUS_UNDER_REVIEW


class TestOwnRiskApproval:
    @pytest.mark.asyncio
    async def test_own_risk_bypasses_document_verification(self):
        hotel = _hotel(HOTEL_STATUS_UNDER_REVIEW)
        service, logs = _service(hotel, can_approve=False)

        await service.approve_own_risk(1, ACTOR, "Owner is a known operator")

        assert hotel.status == HOTEL_STATUS_APPROVED
        assert hotel.is_own_risk_approved is True
        assert logs[0].action == ACTION_HOTEL_APPROVED_OWN_RISK

    @pytest.mark.asyncio
    async def test_justification_is_mandatory(self):
        """An unexplained control bypass is indistinguishable from a mistake."""
        hotel = _hotel(HOTEL_STATUS_UNDER_REVIEW)
        service, logs = _service(hotel, can_approve=False)

        with pytest.raises(ValidationException):
            await service.approve_own_risk(1, ACTOR, "   ")

        assert hotel.status == HOTEL_STATUS_UNDER_REVIEW
        assert hotel.is_own_risk_approved is False
        assert logs == []

    @pytest.mark.asyncio
    async def test_justification_is_written_into_the_audit_trail(self):
        hotel = _hotel(HOTEL_STATUS_UNDER_REVIEW)
        service, logs = _service(hotel, can_approve=False)

        await service.approve_own_risk(1, ACTOR, "  Verified in person  ")

        assert logs[0].remarks == "Verified in person"

    @pytest.mark.asyncio
    async def test_own_risk_refused_when_the_feature_flag_is_off(self, monkeypatch):
        async def disabled(_db, _key, default=False):
            return False

        monkeypatch.setattr("app.modules.hotel.services.get_config_bool", disabled)
        hotel = _hotel(HOTEL_STATUS_UNDER_REVIEW)
        service, logs = _service(hotel)

        with pytest.raises(BusinessException) as exc:
            await service.approve_own_risk(1, ACTOR, "Trusted partner")

        assert "HOTEL_AUTO_APPROVE_ENABLED" in exc.value.message
        assert logs == []


class TestReject:
    @pytest.mark.asyncio
    async def test_reject_requires_a_reason(self):
        hotel = _hotel(HOTEL_STATUS_UNDER_REVIEW)
        service, logs = _service(hotel)

        with pytest.raises(ValidationException):
            await service.reject(1, "", ACTOR)
        assert logs == []

    @pytest.mark.asyncio
    async def test_reject_stores_the_reason_on_the_hotel(self):
        hotel = _hotel(HOTEL_STATUS_UNDER_REVIEW)
        service, _ = _service(hotel)

        await service.reject(1, "Trade licence expired", ACTOR)

        assert hotel.status == HOTEL_STATUS_REJECTED
        assert hotel.rejection_reason == "Trade licence expired"


class TestGoLive:
    @pytest.mark.asyncio
    async def test_activate_from_approved(self):
        hotel = _hotel(HOTEL_STATUS_APPROVED)
        service, _ = _service(hotel)

        await service.activate(1, ACTOR)

        assert hotel.status == HOTEL_STATUS_ACTIVE
        assert hotel.activated_at is not None

    @pytest.mark.asyncio
    async def test_reactivating_keeps_the_original_go_live_date(self):
        """activated_at is when the hotel first went live, not when it last
        came back — reporting depends on the difference."""
        hotel = _hotel(HOTEL_STATUS_INACTIVE)
        first = "2026-01-01T00:00:00+00:00"
        hotel.activated_at = first
        service, _ = _service(hotel)

        await service.activate(1, ACTOR)

        assert hotel.status == HOTEL_STATUS_ACTIVE
        assert hotel.activated_at == first

    @pytest.mark.asyncio
    async def test_activate_refused_when_readiness_regressed(self):
        hotel = _hotel(HOTEL_STATUS_APPROVED)
        service, _ = _service(hotel, can_submit=False)

        with pytest.raises(BusinessException):
            await service.activate(1, ACTOR)
        assert hotel.status == HOTEL_STATUS_APPROVED

    @pytest.mark.asyncio
    async def test_suspended_hotel_can_be_reactivated(self):
        hotel = _hotel(HOTEL_STATUS_SUSPENDED)
        service, _ = _service(hotel)

        await service.activate(1, ACTOR)
        assert hotel.status == HOTEL_STATUS_ACTIVE


class TestSuspendAndBlock:
    @pytest.mark.asyncio
    async def test_suspension_requires_remarks(self):
        hotel = _hotel(HOTEL_STATUS_ACTIVE)
        service, logs = _service(hotel)

        with pytest.raises(ValidationException):
            await service.suspend(1, ACTOR, None)
        assert logs == []

    @pytest.mark.asyncio
    async def test_suspend_records_the_reason(self):
        hotel = _hotel(HOTEL_STATUS_ACTIVE)
        service, logs = _service(hotel)

        await service.suspend(1, ACTOR, "Repeated guest complaints")

        assert hotel.status == HOTEL_STATUS_SUSPENDED
        assert logs[0].remarks == "Repeated guest complaints"

    @pytest.mark.asyncio
    async def test_block_requires_remarks_and_is_terminal(self):
        hotel = _hotel(HOTEL_STATUS_ACTIVE)
        service, _ = _service(hotel)

        with pytest.raises(ValidationException):
            await service.block(1, ACTOR, "")

        await service.block(1, ACTOR, "Fraudulent listing")
        assert hotel.status == HOTEL_STATUS_BLOCKED

        # Nothing leads out of BLOCKED.
        with pytest.raises(BusinessException):
            await service.activate(1, ACTOR)

    @pytest.mark.asyncio
    async def test_deactivate_does_not_require_remarks(self):
        hotel = _hotel(HOTEL_STATUS_ACTIVE)
        service, _ = _service(hotel)

        await service.deactivate(1, ACTOR)
        assert hotel.status == HOTEL_STATUS_INACTIVE
