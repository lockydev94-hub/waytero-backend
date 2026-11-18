# ============================================================
# WAYTERO — TRIP TYPE GUARD TESTS
# File: tests/unit/test_trip_guard.py
# Doc Ref: BRD Part 3 §35 (fare formula); SUPPLIER_CAB_PRICE_MASTER_V1 §4.6
#
# The rules that decide whether a booking type is even the right product for a
# pair of places. The database-facing part (city resolution) is exercised by the
# live smoke tests; these cover the arithmetic that must not drift:
#   • the ETA is distance / the type's average speed, never zero
#   • stops add legs instead of being ignored
#   • a city name inside a longer place name is matched on a word boundary
# ============================================================

from app.modules.booking.services.trip_guard import (
    ResolvedPoint,
    _city_in_text,
    estimate_duration_minutes,
    route_distance_km,
)
from app.modules.booking.services.trip_guard import _City


def _point(name, lat=None, lng=None):
    return ResolvedPoint(name=name, latitude=lat, longitude=lng)


def test_eta_uses_the_trip_types_average_speed():
    # 42 km at the outstation average of 42 km/h is exactly one hour.
    assert estimate_duration_minutes(42, "OUTSTATION") == 60
    # The same distance in the city is slower, so it takes longer.
    assert estimate_duration_minutes(42, "LOCAL") > 60


def test_eta_is_never_zero_for_a_tiny_trip():
    assert estimate_duration_minutes(0.5, "LOCAL") == 5


def test_eta_is_absent_without_a_distance():
    assert estimate_duration_minutes(None, "LOCAL") is None
    assert estimate_duration_minutes(0, "LOCAL") is None


def test_stops_add_their_legs():
    # Bhubaneswar → Konark → Puri is longer than Bhubaneswar → Puri, which is
    # the whole reason a stop cannot be a display-only field.
    direct = route_distance_km(
        [_point("A", 20.2961, 85.8245), _point("B", 19.8135, 85.8312)]
    )
    with_stop = route_distance_km(
        [
            _point("A", 20.2961, 85.8245),
            _point("Konark", 19.8876, 86.0945),
            _point("B", 19.8135, 85.8312),
        ]
    )
    assert direct is not None and with_stop is not None
    assert with_stop > direct


def test_route_distance_is_unmeasured_when_a_point_has_no_coordinates():
    # A typed stop has a name but no pin. The guard must say "not measured"
    # rather than price a partial route as if it were the whole trip.
    assert (
        route_distance_km(
            [_point("A", 20.29, 85.82), _point("Konark"), _point("B", 19.81, 85.83)]
        )
        is None
    )


def test_city_is_matched_on_a_word_boundary():
    cities = [
        _City(id=1, name="Bhubaneswar", latitude=None, longitude=None),
        _City(id=2, name="Puri", latitude=None, longitude=None),
        _City(id=3, name="Purulia", latitude=None, longitude=None),
    ]
    # "Bhubaneswar Airport" is in Bhubaneswar, not a city called "Airport".
    assert _city_in_text(cities, "Bhubaneswar Airport").id == 1
    # Puri must not resolve to Purulia just because Purulia is longer.
    assert _city_in_text(cities, "Puri").id == 2
    assert _city_in_text(cities, "Konark") is None
