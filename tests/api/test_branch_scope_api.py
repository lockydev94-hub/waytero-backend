"""Round 14 — what a branch can see is its own office's rows, and only those.

`tests/unit/test_branch_scope.py` and `test_dashboard_scope.py` already pin the
*predicate*: they build a scope from a stubbed session and assert the clause that
comes out. Neither can say that the SQL the endpoints actually assemble runs
against the real schema, or that the rows already sitting in the owner's database
fall in the right bucket. This file does, one screen at a time, against the same
four views the portal gives a human — the head office, the sub-branch, the super
admin, and the super admin using the switcher.

Read-only on purpose: nothing here inserts, closes or deletes a row, so no test
can leave the owner's data in a different state than it found it. Ground truth
comes from asyncpg over a connection of its own, never through the app's pooled
engine — TestClient owns that event loop for the duration of a test, so a second
client inside the same test would be handed a connection bound to another loop.
That is also why every test below takes exactly one client fixture.

The honest limitation, stated rather than hidden: this database currently holds no
row in the sub-branch's city. A sub-branch caller therefore sees empty lists,
which proves the exclusion (the head office sees N rows, the sub-branch sees none
of them, the platform sees all) but cannot show a sub-branch row surviving in the
sub-branch's own list. That positive case is covered by the unit tests, which can
build a scope around any city they like.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import asyncpg
import pytest

from app.core.config import settings

# Migration 0085's bootstrap offices — the same rows conftest builds payloads from.
# `test_the_two_offices_this_suite_assumes…` re-reads them from the database,
# because an assertion about "the head office" only means something if the
# database agrees which one that is.
HQ_BRANCH_ID, HQ_CITY_ID = 1, 5
CUTTACK_BRANCH_ID, CUTTACK_CITY_ID = 2, 3

API = "/api/v1"
PAGE_SIZE = 100  # the widest page every list endpoint here accepts


def _dsn() -> str:
    return settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql")


def _fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    """One round trip outside the app's engine, on a throwaway loop."""

    async def run() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(_dsn())
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


@dataclass(frozen=True)
class ListSpec:
    """A scoped list screen, and how to ask the database the same question.

    `from_sql`/`city_sql` mirror the production query's own route to the city —
    its own column for the capture and lead queues, its booking for the cab
    queues — so a scope clause pointed at the wrong table shows up as a wrong id
    set rather than as a passing test.
    """

    key: str
    path: str
    from_sql: str
    id_sql: str
    city_sql: str
    # False where production passes allow_unassigned=False: the row cannot have a
    # NULL city, so a LEFT JOIN that misses must not read as head-office work.
    unassigned_to_hq: bool = True
    # True = the screen's row space is exactly this table, so equality is
    # assertable. False = it adds its own status/date predicates, which this
    # suite will not reimplement; containment is still assertable.
    exact: bool = True
    id_keys: tuple[str, ...] = ("id",)


LIST_SPECS: list[ListSpec] = [
    ListSpec(
        key="bookings",
        path=f"{API}/admin/bookings",
        from_sql="master_bookings b",
        id_sql="b.id",
        city_sql="b.city_id",
        unassigned_to_hq=False,
    ),
    ListSpec(
        key="enquiries",
        path=f"{API}/admin/capture/enquiries",
        from_sql="enquiries e",
        id_sql="e.id",
        city_sql="e.city_id",
    ),
    ListSpec(
        key="callbacks",
        path=f"{API}/admin/capture/callbacks",
        from_sql="callback_requests c",
        id_sql="c.id",
        city_sql="c.city_id",
    ),
    ListSpec(
        key="quotations",
        path=f"{API}/admin/capture/quotations",
        from_sql="quotations q",
        id_sql="q.id",
        city_sql="q.city_id",
    ),
    ListSpec(
        key="feedback",
        path=f"{API}/admin/capture/feedback",
        from_sql=(
            "booking_reviews r JOIN master_bookings b ON b.id = r.master_booking_id"
        ),
        id_sql="r.id",
        city_sql="b.city_id",
    ),
    ListSpec(
        key="chat",
        path=f"{API}/admin/chat/conversations",
        from_sql="chat_conversations cc",
        id_sql="cc.id::text",
        city_sql="cc.city_id",
    ),
    ListSpec(
        key="contact-messages",
        path=f"{API}/admin/leads/contact-messages",
        from_sql="contact_messages m",
        id_sql="m.id",
        city_sql="m.city_id",
    ),
    ListSpec(
        key="partner-applications",
        path=f"{API}/admin/leads/partner-applications",
        from_sql="partner_applications pa",
        id_sql="pa.id",
        city_sql="pa.city_id",
    ),
    ListSpec(
        key="customer-care",
        path=f"{API}/admin/customer-care",
        from_sql="customer_care_logs cl",
        id_sql="cl.id",
        city_sql="cl.city_id",
    ),
    ListSpec(
        key="cab-ops-queue",
        path=f"{API}/admin/cab-ops/queue",
        from_sql=(
            "cab_bookings cb JOIN master_bookings b ON b.id = cb.master_booking_id"
        ),
        id_sql="cb.id",
        city_sql="b.city_id",
        unassigned_to_hq=False,
        exact=False,
        id_keys=("id", "cab_booking_id"),
    ),
    ListSpec(
        key="cab-ops-in-trip",
        path=f"{API}/admin/cab-ops/in-trip",
        from_sql=(
            "cab_bookings cb JOIN master_bookings b ON b.id = cb.master_booking_id"
        ),
        id_sql="cb.id",
        city_sql="b.city_id",
        unassigned_to_hq=False,
        exact=False,
        id_keys=("id", "cab_booking_id"),
    ),
]

SPEC_IDS = [s.key for s in LIST_SPECS]


@lru_cache(maxsize=None)
def _ground_truth(spec: ListSpec) -> tuple[tuple[str, int | None], ...]:
    """One query per screen, reused by every test that reads it.

    Safe because this suite writes nothing: the row set cannot change between the
    head-office test and the sub-branch test that compare against it.
    """
    rows = _fetch(
        f"SELECT {spec.id_sql} AS row_id, {spec.city_sql} AS city_id "
        f"FROM {spec.from_sql}"
    )
    return tuple((str(r["row_id"]), r["city_id"]) for r in rows)


def _visible_to(city_id: int, spec: ListSpec, *, include_unassigned: bool) -> set[str]:
    return {
        row_id
        for row_id, city in _ground_truth(spec)
        if city == city_id or (include_unassigned and city is None)
    }


def _hq_view(spec: ListSpec) -> set[str]:
    return _visible_to(HQ_CITY_ID, spec, include_unassigned=spec.unassigned_to_hq)


def _sub_branch_view(spec: ListSpec) -> set[str]:
    return _visible_to(CUTTACK_CITY_ID, spec, include_unassigned=False)


def _all_row_ids(spec: ListSpec) -> set[str]:
    return {row_id for row_id, _ in _ground_truth(spec)}


def _items(client, spec: ListSpec, *, branch_header: int | str | None = None) -> set[str]:
    """The ids on one page of a scoped list, at the widest page the API allows."""
    headers = {} if branch_header is None else {"X-Branch-Id": str(branch_header)}
    response = client.get(spec.path, params={"page_size": PAGE_SIZE}, headers=headers)
    assert response.status_code == 200, (spec.key, response.status_code)
    body = response.json()
    rows = body["items"] if isinstance(body, dict) else body
    assert isinstance(rows, list), (spec.key, type(body).__name__)
    total = body.get("total") if isinstance(body, dict) else len(rows)
    assert total is None or total <= PAGE_SIZE, (
        f"{spec.key} reports {total} rows; this suite reads one page of {PAGE_SIZE}"
    )
    seen = set()
    for row in rows:
        for key in spec.id_keys:
            if key in row:
                seen.add(str(row[key]))
                break
        else:
            pytest.fail(f"{spec.key} row carries none of {spec.id_keys}")
    # An id the database does not know means the wrong column was read, and every
    # comparison after this one would be matching noise against noise.
    assert seen <= _all_row_ids(spec), (
        f"{spec.key} answered with ids that are not rows of {spec.from_sql}"
    )
    return seen


def _require_rows(spec: ListSpec) -> None:
    if not _ground_truth(spec):
        pytest.skip(f"{spec.key} has no rows in this database — nothing to compare")


# ── the two offices the assertions are named after ────────────────────────────
def test_the_two_offices_this_suite_assumes_are_the_ones_in_the_database() -> None:
    by_id = {r["id"]: r for r in _fetch(
        "SELECT id, city_id, is_headquarters, is_active FROM branches"
    )}
    assert by_id, "no branches at all — 0085 has not been applied"
    for branch_id, city_id, is_hq in (
        (HQ_BRANCH_ID, HQ_CITY_ID, True),
        (CUTTACK_BRANCH_ID, CUTTACK_CITY_ID, False),
    ):
        row = by_id.get(branch_id)
        assert row is not None, f"branch {branch_id} is missing"
        assert row["city_id"] == city_id, (
            f"branch {branch_id} sits in city {row['city_id']}, not {city_id}"
        )
        assert row["is_headquarters"] is is_hq
        assert row["is_active"] is True, f"branch {branch_id} is closed"


# ── the lists ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("spec", LIST_SPECS, ids=SPEC_IDS)
def test_the_head_office_list_is_its_own_city_plus_everything_with_no_city_yet(
    client_hq_admin, spec: ListSpec
) -> None:
    _require_rows(spec)
    seen = _items(client_hq_admin, spec)
    if spec.exact:
        assert seen == _hq_view(spec)
    else:
        assert seen <= _hq_view(spec)


@pytest.mark.parametrize("spec", LIST_SPECS, ids=SPEC_IDS)
def test_the_sub_branch_list_is_its_own_city_only(
    client_cuttack_admin, spec: ListSpec
) -> None:
    """The unassigned rows are head-office work, so this list is narrower still."""
    _require_rows(spec)
    seen = _items(client_cuttack_admin, spec)
    if spec.exact:
        assert seen == _sub_branch_view(spec)
    else:
        assert seen <= _sub_branch_view(spec)


@pytest.mark.parametrize("spec", LIST_SPECS, ids=SPEC_IDS)
def test_the_master_admin_without_a_switcher_sees_every_row(
    client_super_admin, spec: ListSpec
) -> None:
    _require_rows(spec)
    seen = _items(client_super_admin, spec)
    if spec.exact:
        assert seen == _all_row_ids(spec)
    else:
        assert seen <= _all_row_ids(spec)


@pytest.mark.parametrize("spec", LIST_SPECS, ids=SPEC_IDS)
def test_the_head_office_view_is_strictly_narrower_than_the_platform(
    client_hq_admin, spec: ListSpec
) -> None:
    """The second half of the proof.

    If a scope clause silently did nothing, the two tests above would both pass —
    every set would equal every other set. This one fails unless the office's own
    view really is smaller than the whole table where the data says it must be.
    """
    _require_rows(spec)
    hidden = _all_row_ids(spec) - _hq_view(spec)
    if not hidden:
        pytest.skip(f"no {spec.key} row sits outside the head office's reach")
    assert len(_items(client_hq_admin, spec)) < len(_all_row_ids(spec))


# ── the switcher ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("spec", LIST_SPECS, ids=SPEC_IDS)
def test_the_switcher_reproduces_an_office_view_for_the_master_admin(
    client_super_admin, spec: ListSpec
) -> None:
    """`X-Branch-Id` is the super admin's view *of* a branch, not a fourth scope:
    choosing an office has to give exactly what that office's own account gets."""
    _require_rows(spec)
    for branch_id, expected in (
        (HQ_BRANCH_ID, _hq_view(spec)),
        (CUTTACK_BRANCH_ID, _sub_branch_view(spec)),
    ):
        seen = _items(client_super_admin, spec, branch_header=branch_id)
        if spec.exact:
            assert seen == expected, (spec.key, branch_id)
        else:
            assert seen <= expected, (spec.key, branch_id)


@pytest.mark.parametrize("spec", [LIST_SPECS[0], LIST_SPECS[1]], ids=["bookings", "enquiries"])
def test_a_branch_account_cannot_borrow_the_switcher(
    client_cuttack_admin, spec: ListSpec
) -> None:
    """The whole attack in one header: a sub-branch manager names the head office
    in their own request. The scope comes from the account's row, so the header is
    ignored rather than honoured — and the head office's own view is bigger."""
    _require_rows(spec)
    plain = _items(client_cuttack_admin, spec)
    borrowed = _items(client_cuttack_admin, spec, branch_header=HQ_BRANCH_ID)
    assert borrowed == plain == _sub_branch_view(spec)
    assert _hq_view(spec) > plain, "the head office sees no more either — nothing was proven"


def test_no_value_of_the_switcher_header_widens_a_branch_account(
    client_cuttack_cco,
) -> None:
    """Every spelling of "show me more" that a hand-edited request can produce.

    The scope comes from the account's own row, so a number is ignored and a
    non-number is refused — but what matters is that none of the answers is the
    platform view. Every enquiry in this database has a NULL city, so a sub-branch
    caller must see none of them while the head office sees all of them; a scope
    that leaked would show up here as a non-empty list.
    """
    spec = LIST_SPECS[1]
    _require_rows(spec)
    hq_view = _hq_view(spec)
    assert hq_view, "no unassigned enquiry exists — the leak this test looks for"
    assert _sub_branch_view(spec) == set()

    for value in (None, "all", "", "0", "-1", str(HQ_BRANCH_ID), "999999"):
        headers = {} if value is None else {"X-Branch-Id": value}
        response = client_cuttack_cco.get(
            spec.path, params={"page_size": PAGE_SIZE}, headers=headers
        )
        assert response.status_code in (200, 404, 422), (value, response.status_code)
        if response.status_code != 200:
            continue
        seen = {str(row["id"]) for row in response.json()["items"]}
        assert seen == set(), (value, sorted(seen))


def test_an_unknown_office_in_the_switcher_is_absent(client_super_admin) -> None:
    response = client_super_admin.get(f"{API}/admin/bookings", headers={"X-Branch-Id": "999999"})
    assert response.status_code == 404


def test_a_switcher_value_that_is_not_an_office_id_is_refused(client_super_admin) -> None:
    response = client_super_admin.get(
        f"{API}/admin/bookings", headers={"X-Branch-Id": "Bhubaneswar"}
    )
    assert response.status_code == 422
    assert "BRANCH_HEADER_INVALID" in response.text


# ── one row, addressed by id ──────────────────────────────────────────────────
@dataclass(frozen=True)
class RowSpec:
    """A by-id screen whose guard reaches the city its own way."""

    key: str
    path_fmt: str
    from_sql: str
    id_sql: str
    city_sql: str
    unassigned_to_hq: bool = True

    @property
    @lru_cache(maxsize=None)
    def truth(self) -> tuple[tuple[str, int | None], ...]:
        rows = _fetch(
            f"SELECT {self.id_sql} AS row_id, {self.city_sql} AS city_id "
            f"FROM {self.from_sql} ORDER BY 1"
        )
        return tuple((str(r["row_id"]), r["city_id"]) for r in rows)

    def ids_visible_to_hq(self) -> list[str]:
        return [
            row_id
            for row_id, city in self.truth
            if city == HQ_CITY_ID or (self.unassigned_to_hq and city is None)
        ]

    def ids_hidden_from_hq(self) -> list[str]:
        visible = set(self.ids_visible_to_hq())
        return [row_id for row_id, _ in self.truth if row_id not in visible]


ROW_SPECS = [
    RowSpec(
        key="booking",
        path_fmt=f"{API}/admin/bookings/{{}}",
        from_sql="master_bookings b",
        id_sql="b.id",
        city_sql="b.city_id",
        unassigned_to_hq=False,
    ),
    RowSpec(
        key="booking notes",
        path_fmt=f"{API}/admin/bookings/{{}}/notes",
        from_sql="master_bookings b",
        id_sql="b.id",
        city_sql="b.city_id",
        unassigned_to_hq=False,
    ),
    RowSpec(
        key="chat conversation",
        path_fmt=f"{API}/admin/chat/conversations/{{}}",
        from_sql="chat_conversations cc",
        id_sql="cc.id::text",
        city_sql="cc.city_id",
    ),
    RowSpec(
        key="partner",
        path_fmt=f"{API}/admin/bookings/resources/partners/{{}}",
        from_sql="partners p",
        id_sql="p.id",
        city_sql="p.city_id",
        unassigned_to_hq=False,
    ),
    RowSpec(
        key="cab booking through its master booking",
        path_fmt=f"{API}/admin/cab-ops/breakdown/{{}}/available-partners",
        from_sql="cab_bookings cb JOIN master_bookings b ON b.id = cb.master_booking_id",
        id_sql="cb.id",
        city_sql="b.city_id",
        unassigned_to_hq=False,
    ),
]

ROW_IDS = [s.key for s in ROW_SPECS]


@pytest.mark.parametrize("spec", ROW_SPECS, ids=ROW_IDS)
def test_a_row_outside_the_callers_office_reads_as_absent(
    client_hq_admin, spec: RowSpec
) -> None:
    """404, never 403: an office may not learn which ids its neighbour holds."""
    hidden = spec.ids_hidden_from_hq()
    if not hidden:
        pytest.skip(f"no {spec.key} row sits outside the head office's reach")
    response = client_hq_admin.get(spec.path_fmt.format(hidden[0]))
    assert response.status_code == 404, (spec.key, response.status_code)


@pytest.mark.parametrize("spec", ROW_SPECS, ids=ROW_IDS)
def test_a_sub_branch_reads_head_office_work_as_absent(
    client_cuttack_admin, spec: RowSpec
) -> None:
    mine = spec.ids_visible_to_hq()
    if not mine:
        pytest.skip(f"no {spec.key} row is the head office's to open")
    response = client_cuttack_admin.get(spec.path_fmt.format(mine[0]))
    assert response.status_code == 404, (spec.key, response.status_code)


@pytest.mark.parametrize("spec", ROW_SPECS, ids=ROW_IDS)
def test_the_office_that_owns_a_row_opens_it(client_hq_admin, spec: RowSpec) -> None:
    """The guard that hides a neighbour's row must not hide your own."""
    mine = spec.ids_visible_to_hq()
    if not mine:
        pytest.skip(f"no {spec.key} row belongs to the head office")
    response = client_hq_admin.get(spec.path_fmt.format(mine[0]))
    assert response.status_code == 200, (spec.key, response.status_code)


@pytest.mark.parametrize("spec", ROW_SPECS, ids=ROW_IDS)
def test_the_master_admin_opens_what_the_scope_hides(
    client_super_admin, spec: RowSpec
) -> None:
    """The other half of the 404 above: the same id answers 200 for an account
    with no branch, so the refusal was the scope and not a missing row."""
    hidden = spec.ids_hidden_from_hq()
    if not hidden:
        pytest.skip(f"no {spec.key} row sits outside the head office's reach")
    response = client_super_admin.get(spec.path_fmt.format(hidden[0]))
    assert response.status_code == 200, (spec.key, response.status_code)


# ── the same numbers, counted for one office ──────────────────────────────────
def _booking_count(where: str, *args: Any) -> int:
    return _fetch(f"SELECT COUNT(*) AS c FROM master_bookings mb WHERE {where}", *args)[0]["c"]


def test_the_bookings_counter_counts_one_offices_bookings(client_hq_admin) -> None:
    stats = client_hq_admin.get(f"{API}/admin/bookings/stats").json()
    assert stats["total"] == _booking_count("mb.city_id = $1", HQ_CITY_ID)


def test_the_bookings_counter_of_an_office_with_no_bookings_is_zero(
    client_cuttack_admin,
) -> None:
    stats = client_cuttack_admin.get(f"{API}/admin/bookings/stats").json()
    assert _booking_count("mb.city_id = $1", CUTTACK_CITY_ID) == 0
    assert stats["total"] == 0


def test_the_master_admin_counter_still_sees_the_rows_no_office_owns(
    client_super_admin,
) -> None:
    """The difference between the platform counter and the head office's is
    exactly the bookings filed in a city that has no office yet — the rows the
    owner has to notice before a third branch exists to own them."""
    whole = client_super_admin.get(f"{API}/admin/bookings/stats").json()["total"]
    in_hq = _booking_count("mb.city_id = $1", HQ_CITY_ID)
    in_cuttack = _booking_count("mb.city_id = $1", CUTTACK_CITY_ID)
    unowned = _booking_count(
        "NOT EXISTS (SELECT 1 FROM branches br WHERE br.city_id = mb.city_id)"
    )
    assert unowned > 0, "every booking has an office — this test would prove nothing"
    # The three views partition the table: this office, that office, and the
    # cities no office covers yet. Summing to `whole` is what says no booking was
    # silently dropped by a scope clause and none was double-counted.
    assert whole == in_hq + in_cuttack + unowned


def test_an_office_with_no_work_in_its_city_gets_no_badges(client_cuttack_admin) -> None:
    """Eleven queue arms, one city with no rows in any of them.

    A non-zero number here is another office's work appearing on this manager's
    sidebar — the leak the per-office cache key exists to stop.
    """
    counts = client_cuttack_admin.get(f"{API}/admin/action-counts").json()
    assert counts, "the badge endpoint returned nothing"
    assert all(value == 0 for value in counts.values()), {
        key: value for key, value in counts.items() if value
    }


def test_the_head_office_badges_are_real_numbers_not_just_zero(client_hq_admin) -> None:
    """The companion claim: those badges are not zero for everybody, so the
    sub-branch's zeros are a scope, not a broken query."""
    counts = client_hq_admin.get(f"{API}/admin/action-counts").json()
    assert sum(counts.values()) > 0, (
        "no badge is non-zero — this database has no queue work to scope"
    )


def test_the_city_comparison_chart_shows_one_bar_for_a_branch(client_hq_admin) -> None:
    """The chart compares offices; an account that owns one has one thing to show."""
    hq_city = _fetch("SELECT name FROM cities WHERE id = $1", HQ_CITY_ID)[0]["name"]
    rows = client_hq_admin.get(f"{API}/admin/dashboard/city-performance").json()
    assert {row["city"] for row in rows} <= {hq_city}


def test_the_city_comparison_chart_shows_no_rain_for_an_office_with_no_bookings(
    client_cuttack_admin,
) -> None:
    rows = client_cuttack_admin.get(f"{API}/admin/dashboard/city-performance").json()
    assert rows == [], [row["city"] for row in rows]


def test_the_office_register_is_readable_by_a_branch_account(client_cuttack_cco) -> None:
    """Names, not rows. The switcher and the branch column both label from this
    list, so hiding the neighbour's name would break the chrome without protecting
    a single record."""
    response = client_cuttack_cco.get(f"{API}/admin/branches")
    assert response.status_code == 200
    assert {row["id"] for row in response.json()} >= {HQ_BRANCH_ID, CUTTACK_BRANCH_ID}
