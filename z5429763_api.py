import csv
import io
import json
import math
import os
import re
import sqlite3
from datetime import date
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, g, request, send_file
from flask_restx import Api, Resource, fields
from werkzeug.exceptions import HTTPException, Unauthorized
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.datastructures import FileStorage

DATABASE = os.environ.get("AVIATION_DB", "aviation.db")
if Path(DATABASE).is_absolute() or ".." in Path(DATABASE).parts or DATABASE == ":memory:":
    raise ValueError("AVIATION_DB must be a relative persistent SQLite file path")
app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=10 * 1024 * 1024, ERROR_404_HELP=False)
api = Api(
    app,
    version="1.0",
    title="Aviation Data API",
    doc="/",
    description=(
        "HTTP Basic authentication: use Swagger Authorize with admin/admin or user/user. "
        "Passwords are stored as salted scrypt hashes; account status is checked on every request. "
        "Use HTTPS outside local development. Only admins manage accounts and import data. "
        "Travel plans are private even from other admins. Dates use ISO YYYY-MM-DD; "
        "future means later than the server local calendar date. Imports are atomic and "
        "reject deletion of routes referenced by plans. Search is literal, case-insensitive "
        "substring matching; fuzzy matching is deliberately avoided for precise aviation codes."
    ),
    authorizations={"basicAuth": {"type": "basic"}},
    security="basicAuth",
)
error_model = api.model("Error", {"message": fields.String(required=True)})
links_model = api.model(
    "Links",
    {
        "self": fields.String,
        "routes": fields.String,
        "airlines": fields.String,
        "airline": fields.String,
        "origin_airport": fields.String,
        "destination_airport": fields.String,
    },
)
account_model = api.model(
    "Account",
    {
        "id": fields.Integer,
        "username": fields.String,
        "role": fields.String,
        "active": fields.Boolean,
    },
)
account_create = api.model(
    "CreateAccount",
    {
        "username": fields.String(
            required=True, description="Unique, 1–80 characters; no colons or control characters"
        ),
        "password": fields.String(required=True, description="1–256 characters"),
    },
)
account_patch = api.model("AccountStatus", {"active": fields.Boolean(required=True)})
SCHEMAS = {
    "airports": "airport_id,name,city,country,iata_code,icao_code,latitude,longitude,altitude,timezone".split(
        ","
    ),
    "airlines": "airline_id,name,iata_code,icao_code,country".split(","),
    "routes": "route_id,airline_id,airline_iata,origin_airport_id,origin_iata,destination_airport_id,destination_iata,codeshare,stops,equipment".split(
        ","
    ),
}
ID_KEYS = {"airports": "airport_id", "airlines": "airline_id", "routes": "route_id"}
models = {}
for kind, columns in SCHEMAS.items():
    props = {}
    for col in columns:
        cls = fields.Integer if col.endswith("_id") or col == "stops" else fields.String
        if col in ("latitude", "longitude", "altitude"):
            cls = fields.Float
        props[col] = cls()
    props["links"] = fields.Nested(links_model)
    models[kind] = api.model(kind.title() + "Record", props)
route_details = api.inherit(
    "RouteDetails",
    models["routes"],
    {
        "airline": fields.Nested(models["airlines"]),
        "origin_airport": fields.Nested(models["airports"]),
        "destination_airport": fields.Nested(models["airports"]),
    },
)
plan_input = api.model(
    "PlanInput",
    {
        "name": fields.String(required=True),
        "departure_date": fields.String(required=True, example="2027-01-01"),
        "return_date": fields.String(required=True, example="2027-01-07"),
        "route_ids": fields.List(fields.Integer, description="Unique route IDs; defaults to []"),
    },
)
plan_patch = api.model(
    "PlanPatch",
    {
        "name": fields.String,
        "departure_date": fields.String,
        "return_date": fields.String,
        "route_ids": fields.List(fields.Integer),
    },
)
plan_model = api.model(
    "Plan",
    {
        "id": fields.Integer,
        "name": fields.String,
        "departure_date": fields.String,
        "return_date": fields.String,
        "route_ids": fields.List(fields.Integer),
        "routes": fields.List(fields.Nested(route_details)),
        "links": fields.Raw,
    },
)
plan_list = api.model("Plans", {"items": fields.List(fields.Nested(plan_model))})
account_list = api.model("Accounts", {"items": fields.List(fields.Nested(account_model))})
import_result = api.model(
    "ImportResult",
    {
        "imported": fields.Raw(description="Dataset names mapped to imported row counts"),
    },
)


def db():
    """One SQLite connection per request, foreign keys always enabled."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def initialise():
    with app.app_context():
        connection = db()
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','user')),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
        );
        CREATE TABLE IF NOT EXISTS airports (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS airlines (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
            airline_id INTEGER NOT NULL REFERENCES airlines(id),
            origin_id INTEGER NOT NULL REFERENCES airports(id),
            destination_id INTEGER NOT NULL REFERENCES airports(id)
        );
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            name TEXT NOT NULL, departure_date TEXT NOT NULL, return_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plan_routes (
            plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
            route_id INTEGER NOT NULL REFERENCES routes(id),
            PRIMARY KEY(plan_id, route_id)
        );
        CREATE INDEX IF NOT EXISTS plans_owner ON plans(owner_id, departure_date);
        """)
        # Seed only once: deleting the default user must remain effective on restart.
        connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY)")
        if not connection.execute("SELECT 1 FROM metadata WHERE key='seeded'").fetchone():
            for username, role in [("admin", "admin"), ("user", "user")]:
                connection.execute(
                    "INSERT INTO accounts(username,password_hash,role) VALUES (?,?,?)",
                    (username, generate_password_hash(username), role),
                )
            connection.execute("INSERT INTO metadata VALUES ('seeded')")
        connection.commit()


def fail(status, message):
    if status == 401:
        raise Unauthorized(description=message)
    api.abort(status, message)


@api.errorhandler(HTTPException)
def http_error(error):
    headers = {"WWW-Authenticate": 'Basic realm="Aviation API"'} if error.code == 401 else {}
    return {"message": error.description}, error.code, headers


@api.errorhandler(sqlite3.IntegrityError)
def integrity_error(_error):
    return {"message": "Operation conflicts with existing data"}, 409


def protected(admin=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            auth = request.authorization
            account = None
            if auth and auth.type.lower() == "basic" and auth.username and auth.password:
                account = (
                    db()
                    .execute("SELECT * FROM accounts WHERE username=?", (auth.username,))
                    .fetchone()
                )
            if account is None or not check_password_hash(account["password_hash"], auth.password):
                fail(401, "Valid HTTP Basic credentials are required")
            if not account["active"]:
                fail(403, "Account is deactivated")
            if admin and account["role"] != "admin":
                fail(403, "Administrator access is required")
            g.account = account
            return fn(*args, **kwargs)

        return wrapped

    return decorate


def responses(fn):
    for code, message in [
        (400, "Invalid input"),
        (401, "Missing or invalid credentials"),
        (403, "Deactivated account or insufficient privileges"),
        (404, "Resource not found or not owned"),
        (409, "Data conflict"),
    ]:
        fn = api.response(code, message, error_model)(fn)
    return fn


def body(allowed, required=()):
    data = request.get_json()
    if not isinstance(data, dict) or set(data) - set(allowed):
        fail(400, "Expected a JSON object containing only documented fields")
    if set(required) - set(data):
        fail(400, "Missing required fields: " + ", ".join(sorted(set(required) - set(data))))
    return data


def text(value, label, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        fail(400, f"{label} must be nonempty text of at most {limit} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        fail(400, f"{label} must contain valid Unicode text")
    return value.strip()


def account_public(row):
    return {
        key: bool(row[key]) if key == "active" else row[key]
        for key in ("id", "username", "role", "active")
    }


@api.route("/accounts")
class Accounts(Resource):
    @api.doc(description="Admin only. List all accounts without credentials.")
    @api.response(200, "Accounts", account_list)
    @responses
    @protected(admin=True)
    def get(self):
        """List accounts."""
        return {
            "items": [account_public(r) for r in db().execute("SELECT * FROM accounts ORDER BY id")]
        }

    @api.doc(description="Admin only. Create an active User; additional admins are not permitted.")
    @api.expect(account_create)
    @api.response(201, "User created", account_model)
    @responses
    @protected(admin=True)
    def post(self):
        """Create a User account."""
        data = body(("username", "password"), ("username", "password"))
        username = text(data["username"], "username", 80)
        if ":" in username or any(ord(char) < 32 or ord(char) == 127 for char in data["username"]):
            fail(400, "username must not contain colons or control characters")
        text(data["password"], "password")
        password = data["password"]
        with db():
            cursor = db().execute(
                "INSERT INTO accounts(username,password_hash,role) VALUES (?,?,'user')",
                (username, generate_password_hash(password)),
            )
        row = db().execute("SELECT * FROM accounts WHERE id=?", (cursor.lastrowid,)).fetchone()
        return account_public(row), 201, {"Location": f'/accounts/{row["id"]}'}


@api.route("/accounts/<int:account_id>")
@api.param("account_id", "Account identifier")
class Account(Resource):
    def find(self, account_id):
        if account_id > 2**63 - 1:
            fail(404, "Account not found")
        row = db().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            fail(404, "Account not found")
        return row

    @api.doc(description="Admin only. Retrieve account details without password/hash.")
    @api.response(200, "Account", account_model)
    @responses
    @protected(admin=True)
    def get(self, account_id):
        """Retrieve an account."""
        return account_public(self.find(account_id))

    @api.doc(
        description="Admin only. Admin cannot be deactivated. Status takes effect immediately."
    )
    @api.expect(account_patch)
    @api.response(200, "Updated account", account_model)
    @responses
    @protected(admin=True)
    def patch(self, account_id):
        """Activate or deactivate a User."""
        row = self.find(account_id)
        data = body(("active",), ("active",))
        if type(data["active"]) is not bool:
            fail(400, "active must be a boolean")
        if row["role"] == "admin" and not data["active"]:
            fail(409, "Admin cannot be deactivated")
        with db():
            db().execute("UPDATE accounts SET active=? WHERE id=?", (data["active"], account_id))
        return account_public(self.find(account_id))

    @api.doc(description="Admin only. Delete a User and their plans. Admin cannot be deleted.")
    @api.response(204, "Deleted")
    @responses
    @protected(admin=True)
    def delete(self, account_id):
        """Delete a User account."""
        if self.find(account_id)["role"] == "admin":
            fail(409, "Admin cannot be deleted")
        with db():
            db().execute("DELETE FROM accounts WHERE id=?", (account_id,))
        return "", 204


def parse_csv(upload, kind):
    try:
        raw = upload.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(raw, newline=""), strict=True)
        if (
            reader.fieldnames is None
            or len(reader.fieldnames) != len(SCHEMAS[kind])
            or set(reader.fieldnames) != set(SCHEMAS[kind])
        ):
            fail(400, f'{kind}: expected columns {", ".join(SCHEMAS[kind])}')
        records = {}
        for line, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                fail(400, f"{kind} row {line}: incorrect number of columns")
            row = {k: v.strip() for k, v in row.items()}
            for key, value in row.items():
                if key.endswith("_id") or key == "stops":
                    if (
                        not re.fullmatch(r"[0-9]{1,19}", value)
                        or int(value) > 2**63 - 1
                        or (int(value) < 1 and key != "stops")
                    ):
                        fail(
                            400,
                            f"{kind} row {line}: {key} must be a positive integer (stops may be zero)",
                        )
                    row[key] = int(value)
                elif key in ("latitude", "longitude", "altitude"):
                    try:
                        row[key] = float(value)
                    except ValueError:
                        fail(400, f"{kind} row {line}: {key} must be numeric")
                    if not math.isfinite(row[key]):
                        fail(400, f"{kind} row {line}: {key} must be finite")
                elif key != "codeshare" and not value:
                    fail(400, f"{kind} row {line}: {key} cannot be empty")
            if kind == "airports":
                if not -90 <= row["latitude"] <= 90 or not -180 <= row["longitude"] <= 180:
                    fail(400, f"{kind} row {line}: invalid geographic coordinates")
                try:
                    ZoneInfo(row["timezone"])
                except (ZoneInfoNotFoundError, ValueError):
                    fail(400, f"{kind} row {line}: unknown timezone")
            code_fields = {
                "iata_code": 3 if kind == "airports" else 2,
                "icao_code": 4 if kind == "airports" else 3,
            }
            if kind == "routes":
                code_fields = {"airline_iata": 2, "origin_iata": 3, "destination_iata": 3}
                if row["codeshare"] not in ("", "Y"):
                    fail(400, f"{kind} row {line}: codeshare must be empty or Y")
            for key, size in code_fields.items():
                if not re.fullmatch(r"[A-Z0-9]{%d}" % size, row[key]):
                    fail(400, f"{kind} row {line}: invalid {key}")
            identifier = row[ID_KEYS[kind]]
            if identifier in records:
                fail(400, f"{kind} row {line}: duplicate identifier {identifier}")
            records[identifier] = row
        return records
    except (UnicodeError, csv.Error) as error:
        fail(400, f"{kind}: malformed UTF-8 CSV: {error}")


def all_records(kind):
    return {
        r["id"]: json.loads(r["payload"]) for r in db().execute(f"SELECT id,payload FROM {kind}")
    }


upload_parser = api.parser()
for dataset in SCHEMAS:
    upload_parser.add_argument(
        dataset, type=FileStorage, location="files", help=f"{dataset}.csv (UTF-8)"
    )


@api.route("/imports")
class Imports(Resource):
    @api.doc(
        description=(
            "Admin only. Upload any nonempty subset of airports, airlines, routes files. "
            "Each supplied dataset replaces its old records, including header-only files. "
            "All files and the combined resulting references/codes are checked before commit. "
            "Unknown multipart fields and repeated files are rejected. Returns 409 if any "
            "resulting route is dangling/inconsistent or a plan would lose a route. "
            "Unspecified datasets and all data on failure remain unchanged."
        )
    )
    @api.expect(upload_parser)
    @api.response(200, "Imported counts", import_result)
    @api.response(413, "Request exceeds 10 MiB", error_model)
    @responses
    @protected(admin=True)
    def post(self):
        """Atomically replace aviation datasets."""
        if (
            not request.files
            or set(request.files) - set(SCHEMAS)
            or request.form
            or any(len(request.files.getlist(k)) != 1 for k in request.files)
        ):
            fail(400, "Upload one file per dataset using airports, airlines and/or routes fields")
        uploaded = {kind: parse_csv(file, kind) for kind, file in request.files.items()}
        connection = db()
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            combined = {kind: uploaded.get(kind, all_records(kind)) for kind in SCHEMAS}
            for route in combined["routes"].values():
                references = [
                    ("airlines", "airline_id", "airline_iata"),
                    ("airports", "origin_airport_id", "origin_iata"),
                    ("airports", "destination_airport_id", "destination_iata"),
                ]
                for kind, id_key, code_key in references:
                    target = combined[kind].get(route[id_key])
                    if target is None or target["iata_code"] != route[code_key]:
                        fail(
                            409,
                            f'Route {route["route_id"]}: inconsistent or missing {id_key}/{code_key}',
                        )
            used = {r[0] for r in connection.execute("SELECT DISTINCT route_id FROM plan_routes")}
            if used - combined["routes"].keys():
                fail(409, "Import would remove routes referenced by existing travel plans")
            # Defer FK checks until the complete replacement has been written.
            connection.execute("PRAGMA defer_foreign_keys = ON")
            for kind in ("routes", "airlines", "airports"):
                connection.execute(f"DELETE FROM {kind}")
            for kind in ("airports", "airlines", "routes"):
                for identifier, record in combined[kind].items():
                    values = (identifier, json.dumps(record))
                    if kind == "routes":
                        connection.execute(
                            "INSERT INTO routes VALUES (?,?,?,?,?)",
                            values
                            + (
                                record["airline_id"],
                                record["origin_airport_id"],
                                record["destination_airport_id"],
                            ),
                        )
                    else:
                        connection.execute(f"INSERT INTO {kind} VALUES (?,?)", values)
        return {"imported": {k: len(v) for k, v in uploaded.items()}}


def record(kind, identifier):
    if identifier > 2**63 - 1:
        fail(404, "Aviation record not found")
    row = db().execute(f"SELECT payload FROM {kind} WHERE id=?", (identifier,)).fetchone()
    if row is None:
        fail(404, f"{kind.title()} record not found")
    result = json.loads(row["payload"])
    links = {"self": f"/{kind}/{identifier}"}
    if kind == "airports":
        links.update(
            routes=f"/airports/{identifier}/routes", airlines=f"/airports/{identifier}/airlines"
        )
    elif kind == "airlines":
        links["routes"] = f"/airlines/{identifier}/routes"
    else:
        links.update(
            airline=f'/airlines/{result["airline_id"]}',
            origin_airport=f'/airports/{result["origin_airport_id"]}',
            destination_airport=f'/airports/{result["destination_airport_id"]}',
        )
    result["links"] = links
    return result


def expanded_route(identifier):
    result = record("routes", identifier)
    result.update(
        airline=record("airlines", result["airline_id"]),
        origin_airport=record("airports", result["origin_airport_id"]),
        destination_airport=record("airports", result["destination_airport_id"]),
    )
    return result


def positive_query(name, default, maximum=None):
    raw = request.args.get(name, str(default))
    if not re.fullmatch(r"[1-9][0-9]{0,18}", raw):
        fail(400, f"{name} must be a positive integer")
    value = int(raw)
    if maximum and value > maximum:
        fail(400, f"{name} must be at most {maximum}")
    return value


def browse(kind, filters=None):
    allowed = {"q", "page", "per_page"}
    if kind == "routes":
        allowed |= {"airline_id", "origin_airport_id", "destination_airport_id"}
    if set(request.args) - allowed:
        fail(400, "Unknown query parameter")
    filters = dict(filters or {})
    for key in allowed - {"q", "page", "per_page"}:
        if key in request.args:
            value = positive_query(key, 1)
            if key in filters and filters[key] != value:
                fail(400, f"{key} conflicts with resource path")
            filters[key] = value
    page = positive_query("page", 1)
    size = positive_query("per_page", 20, 100)
    query = request.args.get("q", "").casefold()
    matches = []
    for identifier, item in sorted(all_records(kind).items()):
        if any(item.get(k) != v for k, v in filters.items()):
            continue
        if query and not any(query in str(v).casefold() for v in item.values()):
            continue
        matches.append(identifier)
    return {
        "items": [record(kind, i) for i in matches[(page - 1) * size : page * size]],
        "total": len(matches),
        "page": page,
        "per_page": size,
    }


browse_params = {
    "q": "Literal case-insensitive substring search over record fields",
    "page": {"description": "Page number", "type": "integer", "minimum": 1, "default": 1},
    "per_page": {
        "description": "Page size",
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "default": 20,
    },
}


def register_dataset(kind):
    page_model = api.model(
        kind.title() + "Page",
        {
            "items": fields.List(fields.Nested(models[kind])),
            "total": fields.Integer,
            "page": fields.Integer,
            "per_page": fields.Integer,
        },
    )
    params = dict(browse_params)
    if kind == "routes":
        params.update(
            {
                k: {
                    "description": "Optional exact numeric ID filter",
                    "type": "integer",
                    "minimum": 1,
                }
                for k in ("airline_id", "origin_airport_id", "destination_airport_id")
            }
        )

    class Collection(Resource):
        @api.doc(
            id=f"list_{kind}",
            description=f"All active accounts. Browse {kind} with stable ID ordering.",
            params=params,
        )
        @api.response(200, "Paginated records", page_model)
        @responses
        @protected()
        def get(self):
            """Browse and search aviation data."""
            return browse(kind)

    class Detail(Resource):
        @api.doc(
            id=f"get_{kind}",
            description=f"All active accounts. Retrieve {kind} details and relationship links.",
        )
        @api.response(200, "Record", route_details if kind == "routes" else models[kind])
        @responses
        @protected()
        def get(self, identifier):
            """Retrieve an aviation record."""
            return expanded_route(identifier) if kind == "routes" else record(kind, identifier)

    api.add_resource(Collection, f"/{kind}", endpoint=kind)
    api.add_resource(Detail, f"/{kind}/<int:identifier>", endpoint=kind + "_detail")

    if kind != "routes":

        class RelatedRoutes(Resource):
            @api.doc(
                id=f"list_{kind}_routes",
                description=(
                    "All active accounts. Airport relationship returns departing routes; "
                    "airline relationship returns operated routes."
                ),
                params=params,
            )
            @api.response(
                200,
                "Paginated routes",
                api.models.get("RoutesPage")
                or api.model(
                    "RoutesPage",
                    {
                        "items": fields.List(fields.Nested(models["routes"])),
                        "total": fields.Integer,
                        "page": fields.Integer,
                        "per_page": fields.Integer,
                    },
                ),
            )
            @responses
            @protected()
            def get(self, identifier):
                """Browse related routes."""
                record(kind, identifier)
                key = "origin_airport_id" if kind == "airports" else "airline_id"
                return browse("routes", {key: identifier})

        api.add_resource(
            RelatedRoutes, f"/{kind}/<int:identifier>/routes", endpoint=kind + "_routes"
        )


for dataset in ("routes", "airports", "airlines"):
    register_dataset(dataset)


@api.route("/airports/<int:identifier>/airlines")
class AirportAirlines(Resource):
    @api.doc(
        description="All active accounts. Airlines operating departures from this airport.",
        params=browse_params,
    )
    @api.response(200, "Airlines", api.models["AirlinesPage"])
    @responses
    @protected()
    def get(self, identifier):
        """Browse airlines operating from an airport."""
        record("airports", identifier)
        ids = {
            r["airline_id"]
            for r in all_records("routes").values()
            if r["origin_airport_id"] == identifier
        }
        result = browse("airlines")
        # Filter before pagination rather than filtering a previously paginated page.
        page, size = result["page"], result["per_page"]
        query = request.args.get("q", "").casefold()
        matches = [record("airlines", i) for i in sorted(ids)]
        matches = [
            r
            for r in matches
            if not query or any(query in str(v).casefold() for k, v in r.items() if k != "links")
        ]
        return {
            "items": matches[(page - 1) * size : page * size],
            "total": len(matches),
            "page": page,
            "per_page": size,
        }


def owned_plan(identifier):
    if identifier > 2**63 - 1:
        fail(404, "Travel plan not found")
    row = (
        db()
        .execute("SELECT * FROM plans WHERE id=? AND owner_id=?", (identifier, g.account["id"]))
        .fetchone()
    )
    if row is None:
        fail(404, "Travel plan not found")
    return row


def plan_public(row, expanded=False):
    result = {key: row[key] for key in ("id", "name", "departure_date", "return_date")}
    result["route_ids"] = [
        r[0]
        for r in db().execute(
            "SELECT route_id FROM plan_routes WHERE plan_id=? ORDER BY route_id", (row["id"],)
        )
    ]
    result["links"] = {
        "self": f'/plans/{row["id"]}',
        "routes": [f"/routes/{i}" for i in result["route_ids"]],
    }
    if expanded:
        result["routes"] = [expanded_route(i) for i in result["route_ids"]]
    return result


def validate_plan(data, previous=None):
    result = {k: previous[k] for k in ("name", "departure_date", "return_date")} if previous else {}
    result.update(data)
    result["name"] = text(result.get("name"), "name", 200)
    for key in ("departure_date", "return_date"):
        value = result.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            fail(400, f"{key} must be a valid YYYY-MM-DD date")
        try:
            date.fromisoformat(value)
        except ValueError:
            fail(400, f"{key} must be a valid YYYY-MM-DD date")
    if result["return_date"] < result["departure_date"]:
        fail(400, "return_date must not be earlier than departure_date")
    if "route_ids" in result:
        ids = result["route_ids"]
        if (
            not isinstance(ids, list)
            or any(type(i) is not int or not 0 < i <= 2**63 - 1 for i in ids)
            or len(ids) != len(set(ids))
        ):
            fail(400, "route_ids must be a list of unique positive integers")
        if any(not db().execute("SELECT 1 FROM routes WHERE id=?", (i,)).fetchone() for i in ids):
            fail(400, "route_ids contains an unknown route")
    return result


@api.route("/plans")
class Plans(Resource):
    @api.doc(
        description="All active accounts. Return only your own plans, sorted by departure date then ID."
    )
    @api.response(200, "Private plans", plan_list)
    @responses
    @protected()
    def get(self):
        """List your private travel plans."""
        return {
            "items": [
                plan_public(r)
                for r in db().execute(
                    "SELECT * FROM plans WHERE owner_id=? ORDER BY departure_date,id",
                    (g.account["id"],),
                )
            ]
        }

    @api.doc(
        description="All active accounts. Create a private plan. Names need not be unique; routes form a set."
    )
    @api.expect(plan_input)
    @api.response(201, "Created plan", plan_model)
    @responses
    @protected()
    def post(self):
        """Create a private travel plan."""
        data = body(
            ("name", "departure_date", "return_date", "route_ids"),
            ("name", "departure_date", "return_date"),
        )
        with db():
            db().execute("BEGIN IMMEDIATE")
            data = validate_plan(data)
            cursor = db().execute(
                "INSERT INTO plans(owner_id,name,departure_date,return_date) VALUES (?,?,?,?)",
                (g.account["id"], data["name"], data["departure_date"], data["return_date"]),
            )
            identifier = cursor.lastrowid
            db().executemany(
                "INSERT INTO plan_routes VALUES (?,?)",
                [(identifier, i) for i in data.get("route_ids", [])],
            )
        return plan_public(owned_plan(identifier)), 201, {"Location": f"/plans/{identifier}"}


@api.route("/plans/<int:identifier>")
class Plan(Resource):
    @api.doc(
        description="Owner only, including for admins. Includes full route/airline/airport details."
    )
    @api.response(200, "Private plan", plan_model)
    @responses
    @protected()
    def get(self, identifier):
        """Retrieve your private travel plan."""
        return plan_public(owned_plan(identifier), expanded=True)

    @api.doc(
        description="Owner only. Update supplied fields; route_ids replaces the complete route set atomically."
    )
    @api.expect(plan_patch)
    @api.response(200, "Updated plan", plan_model)
    @responses
    @protected()
    def patch(self, identifier):
        """Update your private travel plan."""
        data = body(("name", "departure_date", "return_date", "route_ids"))
        if not data:
            fail(400, "Provide at least one field to update")
        with db():
            db().execute("BEGIN IMMEDIATE")
            data = validate_plan(data, owned_plan(identifier))
            db().execute(
                "UPDATE plans SET name=?,departure_date=?,return_date=? WHERE id=?",
                (data["name"], data["departure_date"], data["return_date"], identifier),
            )
            if "route_ids" in data:
                db().execute("DELETE FROM plan_routes WHERE plan_id=?", (identifier,))
                db().executemany(
                    "INSERT INTO plan_routes VALUES (?,?)",
                    [(identifier, i) for i in data["route_ids"]],
                )
        return plan_public(owned_plan(identifier))

    @api.doc(
        description="Owner only. Remove the plan and its route memberships, leaving aviation data unchanged."
    )
    @api.response(204, "Deleted")
    @responses
    @protected()
    def delete(self, identifier):
        """Delete your private travel plan."""
        with db():
            owned_plan(identifier)
            db().execute("DELETE FROM plans WHERE id=?", (identifier,))
        return "", 204


@api.route("/plans/<int:identifier>/routes/<int:route_id>")
class PlanRoute(Resource):
    @api.doc(description="Owner only. Idempotently add an existing route to your plan.")
    @api.response(200, "Updated plan", plan_model)
    @responses
    @protected()
    def put(self, identifier, route_id):
        """Add a route to your plan."""
        with db():
            db().execute("BEGIN IMMEDIATE")
            owned_plan(identifier)
            record("routes", route_id)
            db().execute("INSERT OR IGNORE INTO plan_routes VALUES (?,?)", (identifier, route_id))
        return plan_public(owned_plan(identifier))

    @api.doc(
        description="Owner only. Idempotently remove membership; the route itself is preserved."
    )
    @api.response(204, "Membership removed")
    @responses
    @protected()
    def delete(self, identifier, route_id):
        """Remove a route from your plan."""
        with db():
            owned_plan(identifier)
            if route_id > 2**63 - 1:
                return "", 204
            db().execute(
                "DELETE FROM plan_routes WHERE plan_id=? AND route_id=?", (identifier, route_id)
            )
        return "", 204


@api.route("/plans/export")
class Export(Resource):
    @api.doc(
        description=(
            "Owner only. format=json downloads all your plans ordered by departure date, "
            "including full route, airline and airport details. format=png returns an inline "
            "timeline of at most five nearest future plans. Future excludes today; PNG returns "
            "404 when none exist. Equal dates are ordered by plan ID. No other account's plans are exported."
        ),
        params={"format": {"description": "Export format (default json)", "enum": ["json", "png"]}},
    )
    @api.response(200, "JSON attachment (Plans model) or inline PNG image", plan_list)
    @api.produces(["application/json", "image/png"])
    @responses
    @protected()
    def get(self):
        """Export your plans as JSON or a future-plan timeline."""
        if set(request.args) - {"format"}:
            fail(400, "Unknown query parameter")
        output = request.args.get("format", "json")
        if output not in ("json", "png"):
            fail(400, "format must be json or png")
        rows = (
            db()
            .execute(
                "SELECT * FROM plans WHERE owner_id=? ORDER BY departure_date,id",
                (g.account["id"],),
            )
            .fetchall()
        )
        if output == "json":
            data = {"items": [plan_public(r, expanded=True) for r in rows]}
            return send_file(
                io.BytesIO(json.dumps(data, ensure_ascii=False).encode("utf-8")),
                mimetype="application/json",
                as_attachment=True,
                download_name="travel_plans.json",
            )
        rows = [r for r in rows if r["departure_date"] > date.today().isoformat()][:5]
        if not rows:
            fail(404, "No future travel plans to visualise")
        from matplotlib.figure import Figure
        from matplotlib import dates as mdates

        figure = Figure(figsize=(10, 2 + len(rows) * 0.65), layout="constrained")
        axes = figure.subplots()
        for index, row in enumerate(rows):
            start = date.fromisoformat(row["departure_date"])
            end = date.fromisoformat(row["return_date"])
            axes.plot([start, end], [index, index], marker="o", linewidth=7)
            axes.annotate(
                f"{start.isoformat()} – {end.isoformat()}",
                (mdates.date2num(start), index),
                xytext=(0, 10),
                textcoords="offset points",
                fontsize=8,
            )
        axes.set_yticks(range(len(rows)), [r["name"] for r in rows])
        axes.invert_yaxis()
        # Bound the axis and its ticks explicitly: automatic expansion of a
        # same-day trip near year 9999 can otherwise generate year-10000 ticks.
        first = min(mdates.date2num(date.fromisoformat(row["departure_date"])) for row in rows)
        last = max(mdates.date2num(date.fromisoformat(row["return_date"])) for row in rows)
        padding = max(1, math.ceil((last - first) * 0.05))
        lower = max(mdates.date2num(date.min), first - padding)
        upper = min(mdates.date2num(date.max), last + padding)
        tick_count = min(6, int(upper - lower) + 1)
        ticks = sorted(
            {round(lower + (upper - lower) * i / (tick_count - 1)) for i in range(tick_count)}
        )
        axes.set_xlim(lower, upper)
        axes.set_xticks(ticks)
        axes.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        axes.set_title("Your next travel plans")
        axes.set_xlabel("Travel dates")
        axes.grid(axis="x", alpha=0.3)
        figure.autofmt_xdate()
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png")
        buffer.seek(0)
        return send_file(buffer, mimetype="image/png", download_name="travel_plans.png")


initialise()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
