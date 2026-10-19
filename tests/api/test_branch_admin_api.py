# ============================================================
# WAYTERO — BRANCH ADMIN API TESTS
# File: tests/api/test_branch_admin_api.py
# Doc Ref: plan B7; app/modules/admin/branch_api.py
#
# The register is read by every staff role (a branch account needs its own name
# for the topbar, a super admin needs the whole list for the switcher) and written
# by one. These tests are deliberately read-only against the development
# database: the rules that *refuse* a write are proven here, and the statements a
# successful write would run are proven in
# tests/unit/test_branch_admin_service.py — which also means no test ever
# registers, closes or deletes an office the owner is using.
# ============================================================

import pytest

BRANCHES = "/api/v1/admin/branches"


def _rows(client) -> list[dict]:
    response = client.get(BRANCHES)
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize(
    "who",
    ["client_super_admin", "client_hq_admin", "client_cuttack_cco"],
)
def test_every_office_is_readable_from_any_staff_account(request, who) -> None:
    client = request.getfixturevalue(who)
    rows = _rows(client)
    # The switcher and the branch column both render from this list, so a branch
    # account is not shown only its own row — it needs the other names to label
    # the audit trail it reads.
    assert len(rows) >= 2


def test_the_list_names_the_city_each_office_is_in(client_staff) -> None:
    rows = _rows(client_staff)
    # No invented labels: every office answers with the master-data city it
    # points at, and the head office comes first because that is the office that
    # owns the work nobody has placed yet.
    assert all(row["city_name"] for row in rows)
    assert rows[0]["is_headquarters"] is True


def test_the_register_is_closed_to_a_partner(client) -> None:
    assert client.get(BRANCHES).status_code == 403


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("post", {"json": {"name": "Rogue Office", "code": "RGX", "city_id": 5}}),
        ("patch", {"json": {"name": "Renamed By A Manager"}}),
        ("delete", {}),
    ],
)
def test_registering_or_editing_an_office_is_the_master_admins_alone(
    client_hq_admin, method, kwargs
) -> None:
    # The head-office manager is the most powerful account that is still scoped
    # to one city: opening an office would move their own account out from under
    # every rule that bounds them.
    client = client_hq_admin
    path = BRANCHES if method == "post" else f"{BRANCHES}/1"
    assert getattr(client, method)(path, **kwargs).status_code == 403


def test_a_city_already_has_its_office(client_super_admin) -> None:
    taken = next(row for row in _rows(client_super_admin) if row["is_headquarters"])
    before = len(_rows(client_super_admin))

    response = client_super_admin.post(
        BRANCHES,
        json={
            "name": "Second Bhubaneswar Office",
            "code": "ZZCITYTEST",
            "city_id": taken["city_id"],
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "BRANCH_CITY_TAKEN"
    # Refused means nothing was written — not a half-created office.
    assert len(_rows(client_super_admin)) == before


def test_a_new_office_needs_a_real_city(client_super_admin) -> None:
    response = client_super_admin.post(
        BRANCHES,
        json={"name": "Office Nowhere", "code": "ZZNWCITY", "city_id": 999999999},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert "active city" in body["message"]


def test_there_is_one_head_office(client_super_admin) -> None:
    rows = _rows(client_super_admin)
    head = [row for row in rows if row["is_headquarters"]]
    other = [row for row in rows if not row["is_headquarters"]]
    if not head or not other:
        pytest.skip("this database has no head office to collide with")

    response = client_super_admin.patch(
        f"{BRANCHES}/{other[0]['id']}", json={"is_headquarters": True}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "BRANCH_HEADQUARTERS_EXISTS"
    # The refusal left the office exactly as it was.
    assert other[0]["is_headquarters"] is False


def test_one_office_can_be_renamed_and_stays_the_same_office(client_super_admin) -> None:
    row = next(r for r in _rows(client_super_admin) if not r["is_headquarters"])
    response = client_super_admin.patch(
        f"{BRANCHES}/{row['id']}", json={"name": row["name"]}
    )
    assert response.status_code == 200
    body = response.json()
    # A PATCH that sends only the name must not close the office or drop its
    # address — closing one signs its whole staff out.
    assert body["is_active"] == row["is_active"]
    assert body["address"] == row["address"]
    assert body["city_id"] == row["city_id"]


def test_an_office_that_does_not_exist_says_so(client_super_admin) -> None:
    assert client_super_admin.get(f"{BRANCHES}/999999").status_code == 404


def test_a_manager_reading_one_office_gets_the_row_not_a_scope_error(
    client_cuttack_cco,
) -> None:
    rows = _rows(client_cuttack_cco)
    response = client_cuttack_cco.get(f"{BRANCHES}/{rows[0]['id']}")
    # Branch administration is a register, not a queue: reading a name is not
    # reading another office's bookings, so the scope layer does not hide it.
    assert response.status_code == 200
    assert response.json()["id"] == rows[0]["id"]
