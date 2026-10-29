"""REST-client tests: start the API first, then pytest z1234567_tests.py.

Each test declares its requirement set and purpose. Fixtures create isolated
accounts, restore the supplied aviation data and clean up via public HTTP APIs.
No application imports, direct database access or test-order assumptions.
"""

import csv
import io
import uuid
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path

import pytest
import requests

BASE = "http://127.0.0.1:5000"
ADMIN = ("admin", "admin")
ROOT = Path(__file__).parent


def call(method, path, auth=ADMIN, **kwargs):
    return requests.request(method, BASE + path, auth=auth, timeout=30, **kwargs)


def upload(names=("airports", "airlines", "routes"), overrides=None, auth=ADMIN):
    with ExitStack() as stack:
        files = {}
        for name in names:
            if overrides and name in overrides:
                files[name] = (name + ".csv", overrides[name], "text/csv")
            else:
                files[name] = (
                    name + ".csv",
                    stack.enter_context(open(ROOT / (name + ".csv"), "rb")),
                    "text/csv",
                )
        return call("POST", "/imports", auth=auth, files=files)


def csv_text(kind, mutate):
    with open(ROOT / (kind + ".csv"), newline="") as file:
        reader = csv.DictReader(file)
        columns = reader.fieldnames
        rows = list(reader)
    mutate(rows)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def assert_error(response, status, contains=None):
    assert response.status_code == status, response.text
    assert isinstance(response.json()["message"], str) and response.json()["message"]
    if contains:
        assert contains.lower() in response.json()["message"].lower()


@pytest.fixture
def actors():
    """Isolate every test with two temporary accounts and the sample datasets."""
    assert upload().status_code == 200
    created = []
    try:
        for _ in range(2):
            username = "test_" + uuid.uuid4().hex
            auth = (username, "test-password")
            response = call("POST", "/accounts", json={"username": username, "password": auth[1]})
            assert response.status_code == 201, response.text
            created.append((response.json()["id"], auth))
        yield created
    finally:
        for identifier, _auth in created:
            response = call("DELETE", f"/accounts/{identifier}")
            assert response.status_code == 204, response.text
        assert upload().status_code == 200


def new_plan(auth, **overrides):
    data = {
        "name": "Sydney holiday",
        "departure_date": (date.today() + timedelta(days=10)).isoformat(),
        "return_date": (date.today() + timedelta(days=15)).isoformat(),
        "route_ids": [1],
    }
    data.update(overrides)
    response = call("POST", "/plans", auth=auth, json=data)
    assert response.status_code == 201, response.text
    assert response.headers["Location"] == f'/plans/{response.json()["id"]}'
    return response.json()


def test_swagger():
    """RS1–5: Swagger exposes authentication, models and all public resources."""
    assert call("GET", "/", auth=None).status_code == 200
    response = call("GET", "/swagger.json", auth=None)
    assert response.status_code == 200
    spec = response.json()
    assert spec["securityDefinitions"]["basicAuth"]["type"] == "basic"
    for path in [
        "/accounts",
        "/imports",
        "/airports",
        "/airlines",
        "/routes",
        "/plans",
        "/plans/export",
    ]:
        assert path in spec["paths"]
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method in ("get", "post", "patch", "put", "delete"):
                assert operation.get("summary"), (path, method)
                assert operation.get("description"), (path, method)
                assert "401" in operation["responses"]


@pytest.mark.parametrize("auth", [None, ("admin", "wrong"), ("missing", "wrong")])
def test_authentication(auth):
    """RS1: Missing/invalid credentials yield JSON 401 and a Basic challenge."""
    response = call("GET", "/plans", auth=auth)
    assert_error(response, 401)
    assert response.headers["WWW-Authenticate"].startswith("Basic")


def test_default_accounts():
    """RS1: Both seeded accounts authenticate and no password/hash is disclosed."""
    response = call("GET", "/accounts")
    assert response.status_code == 200
    accounts = response.json()["items"]
    assert any(a["username"] == "admin" and a["role"] == "admin" for a in accounts)
    assert any(a["username"] == "user" and a["active"] for a in accounts)
    assert all(set(a) == {"id", "username", "role", "active"} for a in accounts)
    assert call("GET", "/plans", auth=("user", "user")).status_code == 200


def test_account_lifecycle(actors):
    """RS1: Admin retrieves, deactivates, reactivates and deletes a User immediately."""
    identifier, auth = actors[0]
    response = call("GET", f"/accounts/{identifier}")
    assert response.json()["username"] == auth[0]
    assert (
        call("PATCH", f"/accounts/{identifier}", json={"active": False}).json()["active"] is False
    )
    assert_error(call("GET", "/plans", auth=auth), 403, "deactivated")
    assert call("PATCH", f"/accounts/{identifier}", json={"active": True}).json()["active"] is True
    assert call("GET", "/plans", auth=auth).status_code == 200
    extra = call(
        "POST", "/accounts", json={"username": "delete_" + uuid.uuid4().hex, "password": "p"}
    ).json()
    assert call("DELETE", f'/accounts/{extra["id"]}').status_code == 204
    assert_error(call("GET", f'/accounts/{extra["id"]}'), 404)


def test_admin_protection(actors):
    """RS1: The unique Admin cannot be removed/deactivated; role escalation is rejected."""
    admin = next(a for a in call("GET", "/accounts").json()["items"] if a["role"] == "admin")
    assert_error(call("DELETE", f'/accounts/{admin["id"]}'), 409)
    assert_error(call("PATCH", f'/accounts/{admin["id"]}', json={"active": False}), 409)
    assert_error(
        call("POST", "/accounts", json={"username": "evil", "password": "p", "role": "admin"}), 400
    )


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/accounts"),
        ("POST", "/accounts"),
        ("GET", "/accounts/1"),
        ("PATCH", "/accounts/1"),
        ("DELETE", "/accounts/1"),
        ("POST", "/imports"),
    ],
)
def test_user_restrictions(actors, method, path):
    """RS1–2: Active ordinary Users cannot manage accounts or import data."""
    assert_error(call(method, path, auth=actors[0][1], json={}), 403, "administrator")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"username": "", "password": "p"},
        {"username": "x", "password": ""},
        {"username": 123, "password": "p"},
    ],
)
def test_invalid_account(actors, payload):
    """RS1: Reject incomplete, empty and incorrectly typed account input."""
    assert_error(call("POST", "/accounts", json=payload), 400)


def test_duplicate_account_and_status(actors):
    """RS1: Duplicate usernames conflict and account status must be a JSON boolean."""
    identifier, auth = actors[0]
    assert_error(call("POST", "/accounts", json={"username": auth[0], "password": "p"}), 409)
    assert_error(call("PATCH", f"/accounts/{identifier}", json={"active": "false"}), 400)


@pytest.mark.parametrize(
    "names",
    [
        ("airports",),
        ("airlines",),
        ("routes",),
        ("airports", "airlines"),
        ("airports", "airlines", "routes"),
    ],
)
def test_independent_imports(actors, names):
    """RS2: Any subset can be replaced independently against existing valid references."""
    response = upload(names)
    assert response.status_code == 200, response.text
    assert set(response.json()["imported"]) == set(names)
    for kind in ("airports", "airlines", "routes"):
        with open(ROOT / (kind + ".csv")) as file:
            expected = len(list(csv.DictReader(file)))
        assert call("GET", "/" + kind).json()["total"] == expected


@pytest.mark.parametrize(
    "problem", ["schema", "number", "duplicate", "nan", "code", "short", "utf8", "quote"]
)
def test_invalid_import_atomic(actors, problem):
    """RS2: Invalid schemas/types/IDs/CSV reject the whole multi-file transaction."""
    mutations = {
        "schema": b"wrong,header\n1,2\n",
        "number": csv_text("airports", lambda rows: rows[0].update(airport_id="1.5")),
        "duplicate": csv_text("airports", lambda rows: rows.append(dict(rows[0]))),
        "nan": csv_text("airports", lambda rows: rows[0].update(latitude="nan")),
        "code": csv_text("airports", lambda rows: rows[0].update(iata_code="toolong")),
        "short": b",".join(
            k.encode()
            for k in [
                "airport_id",
                "name",
                "city",
                "country",
                "iata_code",
                "icao_code",
                "latitude",
                "longitude",
                "altitude",
                "timezone",
            ]
        )
        + b"\n1,Sydney\n",
        "utf8": b"\xff\xfe",
        "quote": b'"unterminated',
    }
    before = call("GET", "/airports/1").json()
    response = upload(
        ("airports", "airlines"),
        {
            "airports": mutations[problem],
            "airlines": csv_text("airlines", lambda rows: rows[0].update(name="Must roll back")),
        },
    )
    assert_error(response, 400)
    assert call("GET", "/airports/1").json() == before
    assert call("GET", "/airlines/1").json()["name"] != "Must roll back"


@pytest.mark.parametrize(
    "field,value",
    [
        ("airline_id", "9999"),
        ("origin_airport_id", "9999"),
        ("destination_airport_id", "9999"),
        ("origin_iata", "ZZZ"),
        ("destination_iata", "ZZZ"),
        ("airline_iata", "ZZ"),
    ],
)
def test_import_reference_conflicts(actors, field, value):
    """RS2: Missing foreign IDs and inconsistent ID/IATA pairs return 409 atomically."""
    before = call("GET", "/routes/1").json()
    broken = csv_text("routes", lambda rows: rows[0].update({field: value}))
    assert_error(upload(("routes",), {"routes": broken}), 409)
    assert call("GET", "/routes/1").json() == before


def test_combined_import(actors):
    """RS2: References are checked against combined new datasets, not old records."""
    airports = csv_text("airports", lambda rows: rows[0].update(iata_code="ZZZ"))

    def change_routes(rows):
        for row in rows:
            for prefix in ("origin", "destination"):
                if row[prefix + "_airport_id"] == "1":
                    row[prefix + "_iata"] = "ZZZ"

    routes = csv_text("routes", change_routes)
    assert_error(upload(("airports",), {"airports": airports}), 409)
    assert (
        upload(("airports", "routes"), {"airports": airports, "routes": routes}).status_code == 200
    )
    assert call("GET", "/airports/1").json()["iata_code"] == "ZZZ"
    assert call("GET", "/routes/1").json()["origin_iata"] == "ZZZ"


def test_replace_and_plan_reference(actors):
    """RS2/4: Replacement removes old rows but cannot remove a route used by a plan."""
    plan = new_plan(actors[0][1])
    reduced = csv_text("routes", lambda rows: rows.pop(0))
    assert_error(upload(("routes",), {"routes": reduced}), 409, "plans")
    assert call("DELETE", f'/plans/{plan["id"]}', auth=actors[0][1]).status_code == 204
    assert upload(("routes",), {"routes": reduced}).status_code == 200
    assert_error(call("GET", "/routes/1"), 404)


def test_invalid_multipart(actors):
    """RS2: Reject absent files, unknown fields and repeated dataset attachments."""
    assert_error(call("POST", "/imports"), 400)
    assert_error(call("POST", "/imports", files={"unknown": ("x.csv", "x")}), 400)
    data = (ROOT / "airports.csv").read_bytes()
    assert_error(
        call(
            "POST", "/imports", files=[("airports", ("a.csv", data)), ("airports", ("b.csv", data))]
        ),
        400,
    )


def test_exploration(actors):
    """RS3: Detail/search/pagination preserve fields and expose traversable relationships."""
    auth = actors[0][1]
    airport = call("GET", "/airports/1", auth=auth).json()
    assert airport["iata_code"] == "SYD"
    response = call("GET", "/airports?q=sYdNeY&per_page=1", auth=auth)
    assert response.status_code == 200
    assert response.json()["items"][0]["airport_id"] == 1
    assert response.json()["per_page"] == 1
    assert call("GET", airport["links"]["routes"], auth=auth).json()["total"] > 0
    airlines = call("GET", airport["links"]["airlines"], auth=auth).json()["items"]
    assert airlines and "iata_code" in airlines[0]
    airline = call("GET", "/airlines/1", auth=auth).json()
    assert all(
        r["airline_id"] == 1
        for r in call("GET", airline["links"]["routes"], auth=auth).json()["items"]
    )
    route = call("GET", "/routes/1", auth=auth).json()
    assert route["origin_airport"]["airport_id"] == route["origin_airport_id"]
    assert route["airline"]["airline_id"] == route["airline_id"]
    assert call("GET", "/routes?origin_airport_id=1", auth=auth).json()["total"] > 0
    assert call("GET", "/airports?q=no_such_airport", auth=auth).json()["items"] == []
    p1 = call("GET", "/airports?per_page=1&page=1", auth=auth).json()["items"]
    p2 = call("GET", "/airports?per_page=1&page=2", auth=auth).json()["items"]
    assert p1[0]["airport_id"] != p2[0]["airport_id"]


@pytest.mark.parametrize(
    "path",
    [
        "/airports?page=0",
        "/airports?per_page=101",
        "/routes?airline_id=x",
        "/airports?page=1.5",
        "/airports?unknown=x",
    ],
)
def test_invalid_search(actors, path):
    """RS3: Reject invalid pagination, route filters and undocumented query fields."""
    assert_error(call("GET", path, auth=actors[0][1]), 400)


@pytest.mark.parametrize(
    "path",
    [
        "/airports/999999",
        "/airlines/999999",
        "/routes/999999",
        "/airports/999999/routes",
        "/airlines/999999/routes",
    ],
)
def test_missing_resources(actors, path):
    """RS3: Missing resources and parent relationships return structured 404."""
    assert_error(call("GET", path, auth=actors[0][1]), 404)


def test_plan_lifecycle(actors):
    """RS4: Owner creates, retrieves, edits, adds/removes routes and deletes a plan."""
    auth = actors[0][1]
    plan = new_plan(auth)
    path = f'/plans/{plan["id"]}'
    response = call("GET", path, auth=auth)
    assert response.status_code == 200
    assert response.json()["routes"][0]["route_id"] == 1
    assert call("PATCH", path, auth=auth, json={"name": "Updated", "route_ids": [2]}).json()[
        "route_ids"
    ] == [2]
    assert call("GET", path, auth=auth).json()["name"] == "Updated"
    for _ in range(2):
        assert call("PUT", path + "/routes/1", auth=auth).json()["route_ids"] == [1, 2]
    assert call("DELETE", path + "/routes/2", auth=auth).status_code == 204
    assert call("GET", path, auth=auth).json()["route_ids"] == [1]
    assert any(p["id"] == plan["id"] for p in call("GET", "/plans", auth=auth).json()["items"])
    assert call("DELETE", path, auth=auth).status_code == 204
    assert_error(call("GET", path, auth=auth), 404)


@pytest.mark.parametrize("intruder_index", [1, "admin"])
def test_plan_privacy(actors, intruder_index):
    """RS4/5: Other Users and Admin cannot read/change/delete/export someone else's plan."""
    owner = actors[0][1]
    intruder = ADMIN if intruder_index == "admin" else actors[1][1]
    plan = new_plan(owner)
    path = f'/plans/{plan["id"]}'
    for method, suffix, kwargs in [
        ("GET", "", {}),
        ("PATCH", "", {"json": {"name": "stolen"}}),
        ("DELETE", "", {}),
        ("PUT", "/routes/2", {}),
        ("DELETE", "/routes/1", {}),
    ]:
        assert_error(call(method, path + suffix, auth=intruder, **kwargs), 404)
    for endpoint in ("/plans", "/plans/export"):
        items = call("GET", endpoint, auth=intruder).json()["items"]
        assert plan["id"] not in [p["id"] for p in items]
    assert call("GET", path, auth=owner).json()["name"] == "Sydney holiday"


@pytest.mark.parametrize(
    "changes",
    [
        {"departure_date": "bad"},
        {"departure_date": "2027-02-30"},
        {"departure_date": "20270101"},
        {"return_date": "2000-01-01"},
        {"name": ""},
        {"route_ids": [999999]},
        {"route_ids": [1, 1]},
        {"route_ids": [True]},
        {"route_ids": "1"},
        {"unknown": 1},
    ],
)
def test_invalid_plan(actors, changes):
    """RS4: Validate dates, names and route sets; failed creation leaves no partial plan."""
    auth = actors[0][1]
    data = {
        "name": "Invalid",
        "departure_date": "2027-01-01",
        "return_date": "2027-01-02",
        "route_ids": [1],
    }
    data.update(changes)
    assert_error(call("POST", "/plans", auth=auth, json=data), 400)
    assert call("GET", "/plans", auth=auth).json()["items"] == []


def test_patch_atomicity_and_same_names(actors):
    """RS4: Failed patches preserve the plan; duplicate names and same-day travel are valid."""
    auth = actors[0][1]
    plan = new_plan(auth, departure_date="2027-01-01", return_date="2027-01-01")
    same = new_plan(auth)
    assert same["name"] == plan["name"]
    path = f'/plans/{plan["id"]}'
    assert_error(
        call("PATCH", path, auth=auth, json={"name": "bad update", "route_ids": [999999]}), 400
    )
    assert call("GET", path, auth=auth).json()["name"] == plan["name"]
    assert_error(call("PATCH", path, auth=auth, json={"departure_date": "2028-01-01"}), 400)
    assert_error(call("PUT", path + "/routes/999999", auth=auth), 404)


def test_json_export(actors):
    """RS5: JSON download is sorted, private and contains complete nested aviation records."""
    auth = actors[0][1]
    later = new_plan(auth, departure_date="2030-01-01", return_date="2030-01-02")
    earlier = new_plan(auth, departure_date="2027-01-01", return_date="2027-01-02")
    new_plan(actors[1][1], name="Secret")
    response = call("GET", "/plans/export?format=json", auth=auth)
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/json")
    assert "attachment" in response.headers["Content-Disposition"]
    items = response.json()["items"]
    assert [p["id"] for p in items] == [earlier["id"], later["id"]]
    route = items[0]["routes"][0]
    for key in ("airline", "origin_airport", "destination_airport"):
        assert isinstance(route[key]["name"], str)
    assert route["origin_airport"]["iata_code"] == route["origin_iata"]
    assert all(p["name"] != "Secret" for p in items)


def test_png_export(actors):
    """RS5: PNG returns a valid image for future plans and rejects unsupported formats."""
    auth = actors[0][1]
    for offset in range(1, 8):
        new_plan(
            auth,
            name=f"Trip {offset}",
            departure_date=(date.today() + timedelta(days=offset)).isoformat(),
            return_date=(date.today() + timedelta(days=offset + 1)).isoformat(),
        )
    response = call("GET", "/plans/export?format=png", auth=auth)
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("image/png")
    from PIL import Image

    image = Image.open(io.BytesIO(response.content))
    assert image.format == "PNG" and image.width == 1000 and image.height == 525
    assert_error(call("GET", "/plans/export?format=xml", auth=auth), 400, "format")


def test_no_future_export(actors):
    """RS5: Empty JSON is valid; today and past plans do not qualify for future PNG."""
    auth = actors[0][1]
    assert call("GET", "/plans/export", auth=auth).json() == {"items": []}
    assert_error(call("GET", "/plans/export?format=png", auth=auth), 404, "future")
    new_plan(auth, departure_date=date.today().isoformat(), return_date=date.today().isoformat())
    new_plan(auth, departure_date="2000-01-01", return_date="2000-01-02")
    assert_error(call("GET", "/plans/export?format=png", auth=auth), 404, "future")
    assert len(call("GET", "/plans/export", auth=auth).json()["items"]) == 2


def test_large_identifiers(actors):
    """RS1–4: Out-of-range SQLite IDs return client errors rather than server failures."""
    huge = 2**64
    for kind in ("accounts", "airports", "airlines", "routes", "plans"):
        assert_error(call("GET", f"/{kind}/{huge}"), 404)
    auth = actors[0][1]
    plan = new_plan(auth)
    assert_error(call("PATCH", f'/plans/{plan["id"]}', auth=auth, json={"route_ids": [huge]}), 400)
    broken = csv_text("airports", lambda rows: rows[0].update(airport_id=str(huge)))
    assert_error(upload(("airports",), {"airports": broken}), 400)
