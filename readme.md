# COMP9321 26T3 Assignment 2 - Aviation Data RESTful API

## Overview
This project is a RESTful API built using **Flask-RESTX**. It supports two types of accounts (Admin and User) to interact with aviation data, including airports, airlines, and routes. The API allows data exploration, management of private travel plans, and data importing/exporting capabilities. 

All endpoints are fully documented and testable via an interactive **Swagger UI**.

## Technical Requirements
- **Language:** Python 3.13 ONLY
- **Framework:** Flask-RESTX
- **Database:** Local SQLite ONLY (automatically created at runtime, persistent, no absolute paths)
- **Dependencies:** Listed in `requirements.txt` (e.g., pandas, matplotlib, requests)

## Files
- `z[your_zid]_api.py`: The main Flask-RESTX API implementation.
- `z[your_zid]_tests.py`: The automated test suite (REST client).
- `airports.csv`, `airlines.csv`, `routes.csv`: Synthetic datasets used for importing data.

## Default Accounts
The system automatically creates the following default accounts on the first run:

| Username | Password | Role  |
|----------|----------|-------|
| admin    | admin    | Admin |
| user     | user     | User  |

## Features (Requirement Sets)
1. **User Management and Access Control:** 
   - Role-based access (Admin and User).
   - Admin can create, delete, activate, and deactivate User accounts. 
   - Authentication and authorization mechanisms are implemented to protect endpoints.
2. **Importing Data:**
   - Admin only feature.
   - Upload `airports.csv`, `airlines.csv`, and `routes.csv` via `multipart/form-data`.
   - Replaces existing records, validates schemas, preserves referential integrity, and processes atomically.
3. **Exploring Data:**
   - Browse and search detailed information about airports, airlines, and routes.
   - Navigate relationships (e.g., airlines operating from an airport, routes of an airline).
4. **My Travel Plans:**
   - Users can create, retrieve, modify, and remove private travel plans.
   - Plans include a name, departure date, return date, and a collection of routes.
   - All plans are strictly private to the account owner (including the Admin).
5. **Exporting Travel Plans:**
   - Export private travel plans as a downloadable self-contained JSON file.
   - Export a visualization image (e.g., generated with `matplotlib`) showing up to five future travel plans.
6. **Testing:**
   - Automated REST client test suite covering main functionalities, edge cases, status codes, and error messages.

## How to Run

1. Create and activate a virtual environment.
2. Install the required libraries:
   ```bash
   pip install -r requirements.txt
   ```
3. Start the API server:
   ```bash
   python z[your_zid]_api.py
   ```
4. Access the interactive Swagger UI via your browser at:
   `http://127.0.0.1:5000`

## How to Test
Ensure your API is running at `http://127.0.0.1:5000` with a freshly created database and the three CSV files in the working directory. Then, run the test suite using pytest:
```bash
pytest z[your_zid]_tests.py
```
## Implemented files and quick start

The implementation is `z1234567_api.py` and the REST-client suite is
`z1234567_tests.py`. Replace `z1234567` in both filenames with your actual student
ID before submission. No API implementation is stored in the specification
notebook.

Use **Python 3.13** from this directory:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python z1234567_api.py
```

In the prepared cloud environment, use
`source /workspace/.venvs/9321-ass2/bin/activate` instead of creating a new venv.
If the home directory is read-only, set `MPLCONFIGDIR` and `XDG_CACHE_HOME` to
writable cache directories before starting the server (the cloud configuration
contains these settings).

SQLite creates `aviation.db` relative to the working directory and reuses it on
restart. `AVIATION_DB` optionally selects another **relative** filename, such as
`AVIATION_DB=test.db python z1234567_api.py`. Absolute paths, parent traversal and
in-memory databases are rejected. The default accounts are seeded once, so a
deleted default User stays deleted on restart. Passwords and password hashes are
never returned. Do not submit database files.

### Authentication and API design

Swagger is available at `/`; the machine-readable specification is
`/swagger.json`. Click **Authorize** and enter a username and password. All data
endpoints require HTTP Basic authentication, with account activation checked on
every request. Passwords are salted and hashed with Werkzeug scrypt. Basic auth
is straightforward for this local REST-client assignment and does not require
storing bearer tokens; use HTTPS when deployed outside local development.
Default assignment passwords should not be used for a real deployment.

| Resource | Operations | Access |
| --- | --- | --- |
| `/accounts` | GET list; POST create User | Admin |
| `/accounts/{id}` | GET details; PATCH `active`; DELETE | Admin |
| `/imports` | POST multipart CSV files | Admin |
| `/airports`, `/airlines`, `/routes` | GET paginated list/search | Active accounts |
| `/airports/{id}`, `/airlines/{id}`, `/routes/{id}` | GET details and relationship links | Active accounts |
| `/airports/{id}/routes` | GET departures | Active accounts |
| `/airports/{id}/airlines` | GET airlines operating departures | Active accounts |
| `/airlines/{id}/routes` | GET operated routes | Active accounts |
| `/plans` | GET own plans; POST create | Owner |
| `/plans/{id}` | GET full details; PATCH; DELETE | Owner |
| `/plans/{id}/routes/{route_id}` | PUT add; DELETE remove | Owner |
| `/plans/export?format=json` | GET all own plans as JSON attachment | Owner |
| `/plans/export?format=png` | GET timeline of next five future plans | Owner |

A User can never create another Admin. The dedicated Admin cannot be deleted or
deactivated. Attempts to access another account's plans return 404 even for the
Admin, avoiding disclosure of private plan existence. Deleting a User cascades
to their plans, but does not remove aviation data. Invalid authentication returns
401 with a Basic challenge; deactivated accounts/insufficient roles return 403.
Errors use a JSON `message` field. Invalid input returns 400, missing resources
404, and conflicts 409.

Lists of aviation records support `q`, `page` (default 1), and `per_page` (default
20, maximum 100). Search is literal, case-insensitive substring matching over
record fields. Fuzzy matching is deliberately avoided for exact aviation codes.
Routes additionally support exact `airline_id`, `origin_airport_id` and
`destination_airport_id` filters. Lists use stable ID ordering and return
`items`, `total`, `page`, and `per_page`. Detail responses contain navigation
links; route detail also embeds the airline and both airports.

### Import data

```bash
curl -u admin:admin -X POST http://127.0.0.1:5000/imports \
  -F airports=@airports.csv -F airlines=@airlines.csv -F routes=@routes.csv
curl -u user:user 'http://127.0.0.1:5000/airports?q=sydney'
```

Use multipart field names `airports`, `airlines`, and `routes`. Any subset is
accepted. Each included dataset replaces its previous contents; a header-only
CSV intentionally empties that dataset when referential integrity permits it.
Files must have exactly the sample column names (column order may vary), valid
UTF-8, correctly typed fields and unique positive record IDs. Coordinates must
be finite and within geographic bounds, codes have the documented lengths,
timezones must be recognised, stops are nonnegative integers, and codeshare is
empty or `Y`. Maximum total upload size is 10 MiB.

Imports validate references against the **combined resulting datasets**, including
agreement between route IDs and IATA codes. A single SQLite transaction and
foreign keys protect atomicity. If a route would reference a missing or
inconsistent airline/airport, the entire import returns 409. A route that appears
in any existing travel plan cannot be removed by import: remove the membership
or plan first. Reimporting the same route ID updates its details in existing
plans. Omitted datasets remain unchanged. CSV contents are stored in SQLite,
not kept as application state in memory.

### Travel plans and export

```bash
curl -u user:user -H 'Content-Type: application/json' \
  -d '{"name":"Holiday","departure_date":"2027-01-01","return_date":"2027-01-07","route_ids":[1,2]}' \
  http://127.0.0.1:5000/plans
curl -u user:user http://127.0.0.1:5000/plans/export?format=json -o travel_plans.json
curl -u user:user http://127.0.0.1:5000/plans/export?format=png -o travel_plans.png
```

Dates must be valid `YYYY-MM-DD` values; return cannot precede departure.
Names may be duplicated, including within the same account. `route_ids` is a
set represented by an array of unique positive integers; it defaults to empty
when omitted at creation. PATCH changes only supplied fields; supplying
`route_ids` replaces the whole set. PUT/DELETE membership operations are
idempotent. The API permits same-day trips and past plans.

JSON contains an `items` array ordered by departure date, then plan ID. Every
plan includes full route, airline and airport details, so the exported file is
self-contained. An account with no plans receives `{"items":[]}`. PNG renders a
labelled travel-date timeline for up to five nearest future plans; departure
must be strictly later than the server's local calendar date. Today is excluded,
even when the return is in the future. If no future plans exist, PNG returns a
JSON 404. Image output is `image/png` for inline display; JSON is an attachment.

### Automated validation

Start the API in a separate terminal with a new, dedicated database filename:

```bash
AVIATION_DB=assignment_test.db python z1234567_api.py
```

Then, from this directory with the same Python environment:

```bash
python -m pytest -q z1234567_tests.py
```

Choose an unused database filename rather than deleting a database with useful
data. The tests use real `requests` HTTP calls against port 5000, create unique
temporary Users, clean up through the API, and restore the original CSV data.
They cover all six requirement sets, including malformed imports, combined
reference validation, rollback, account deactivation, Admin restrictions, plan
privacy, date validation and both export formats. **Use a dedicated test database**:
imports intentionally replace aviation datasets. Tests do not rely on execution
order, direct database access, or importing application internals. Each test's
docstring identifies its requirement and purpose.

The server uses Flask's local development server with debug mode disabled. No
external API keys, database server or additional dependencies are required.
