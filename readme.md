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