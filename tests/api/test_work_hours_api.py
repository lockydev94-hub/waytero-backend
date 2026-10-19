"""
Work-hours capture routes (Admin Portal §39, migration 0084).

Two separate contracts, deliberately kept apart:
  * `POST /auth/admin/heartbeat` and `GET /auth/admin/work-hours` are the
    signed-in admin's own pages — any dashboard staff role may call them.
  * `GET /admin/staff-work-hours` is the leave/attendance report and reads
    everybody's rows, so it is gated on SUPER_ADMIN alone.

The arithmetic behind the day row (the platform-timezone bucket, the crash
ceiling, and the "dashboard staff only" rule that lives in the service rather
than on the route) is proven in tests/unit/test_work_hours_service.py; here we
pin the envelope, the client-trust boundary, and who is allowed through.
"""
from datetime import date, timedelta

HEARTBEAT = "/api/v1/auth/admin/heartbeat"
MY_HOURS = "/api/v1/auth/admin/work-hours"
REPORT = "/api/v1/admin/staff-work-hours"


def test_heartbeat_keeps_at_most_a_minute(client_staff) -> None:
    response = client_staff.post(HEARTBEAT, json={"delta_seconds": 3600})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["recorded_seconds"] == 60


def test_heartbeat_passes_an_honest_minute_through(client_staff) -> None:
    response = client_staff.post(HEARTBEAT, json={"delta_seconds": 42})

    assert response.status_code == 200
    assert response.json()["data"]["recorded_seconds"] == 42


def test_heartbeat_rejects_a_zero_or_negative_gap_at_the_edge(client_staff) -> None:
    assert client_staff.post(HEARTBEAT, json={"delta_seconds": 0}).status_code == 422
    assert (
        client_staff.post(HEARTBEAT, json={"delta_seconds": -30}).status_code == 422
    )
    assert client_staff.post(HEARTBEAT, json={}).status_code == 422


def test_my_work_hours_returns_the_report_shape(client_staff) -> None:
    response = client_staff.get(MY_HOURS)

    assert response.status_code == 200
    data = response.json()["data"]
    assert {"items", "period_totals", "date_from", "date_to"} <= set(data)
    assert date.fromisoformat(data["date_to"]) - date.fromisoformat(
        data["date_from"]
    ) == timedelta(days=29)


def test_my_work_hours_window_is_bounded(client_staff) -> None:
    assert client_staff.get(MY_HOURS, params={"days": 0}).status_code == 422
    assert client_staff.get(MY_HOURS, params={"days": 400}).status_code == 422


def test_work_hours_report_refuses_a_non_super_admin(client_staff) -> None:
    today = date.today()
    response = client_staff.get(
        REPORT, params={"date_from": today - timedelta(days=7), "date_to": today}
    )

    assert response.status_code == 403


def test_work_hours_report_refuses_a_non_dashboard_token(client) -> None:
    """The `client` fixture signs in as PARTNER — outside ALL_STAFF, so the
    router-level gate bounces it before the report SQL ever runs."""
    assert client.get(REPORT).status_code == 403


def test_work_hours_report_reads_the_0084_table(client_super_admin) -> None:
    """Not a fake: this runs the report SQL against the real `staff_work_daily`,
    so a renamed column or a drift between the model and the migration fails
    here instead of on the super admin's screen."""
    today = date.today()
    response = client_super_admin.get(
        REPORT, params={"date_from": today - timedelta(days=7), "date_to": today}
    )

    assert response.status_code == 200
    body = response.json()
    assert {
        "items",
        "period_totals",
        "total",
        "page",
        "page_size",
        "date_from",
        "date_to",
    } <= set(body)
    assert body["total"] >= 0
    assert isinstance(body["items"], list)


def test_work_hours_report_rejects_a_reversed_range(client_super_admin) -> None:
    today = date.today()
    response = client_super_admin.get(
        REPORT, params={"date_from": today, "date_to": today - timedelta(days=1)}
    )

    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "INVALID_DATE_RANGE"


def test_work_hours_report_rejects_a_page_size_over_the_cap(
    client_super_admin,
) -> None:
    today = date.today()
    response = client_super_admin.get(
        REPORT,
        params={
            "date_from": today - timedelta(days=7),
            "date_to": today,
            "page_size": 500,
        },
    )

    assert response.status_code == 422
