# Employee Attendance & Analytics API

Backend for the HROne attendance assignment: employees punch in and out, HR can correct a day's record (with an audit trail), and there are four analytics endpoints built as MongoDB aggregation pipelines.

Stack: Python 3.11+, FastAPI, PyMongo, MongoDB 6.0+ (I used 7.0). All application code is in `app/main.py`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                 # then edit if your MongoDB isn't on localhost
```

Environment variables:

| Variable | Example |
|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` |
| `MONGO_DB` | `attendance_db` |

The `.env` file is only a convenience. If the variables are set in the real environment, those win. The app refuses to start if either one is missing.

## Running

```bash
python sample_seed.py                # optional: loads the kit's 9 sample documents
uvicorn app.main:app --port 8000
```

Indexes are created at startup, so there are no manual DB steps. Swagger docs are at http://127.0.0.1:8000/docs.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | 200 if MongoDB answers a ping, else 503 |
| POST | `/employees` | 409 on duplicate `emp_code` |
| GET | `/employees` | `department`, `page`, `page_size`; sorted by `emp_code` |
| POST | `/attendance/punch-in` | creates the day's record; 409 if one already exists |
| POST | `/attendance/punch-out` | closes the latest record with `punch_in <= punched_at` |
| PATCH | `/attendance/{emp_code}/{date}` | manual correction, appends to `history` |
| GET | `/attendance` | filters: `emp_code`, `date_from`, `date_to`, `status`; paginated |
| GET | `/analytics/employees/{emp_code}/monthly` | `month=YYYY-MM` |
| GET | `/analytics/departments/summary` | `month`, optional `department` |
| GET | `/analytics/leaderboard/late` | `month`, `limit`, optional `department` |
| GET | `/analytics/departments/{department}/trend` | `from`, `to` (max 92 days) |
| GET | `/admin/explain/{endpoint}` | MongoDB explain output for an endpoint's main query |

All times in requests and responses are epoch milliseconds; dates are `YYYY-MM-DD` (IST).

## Notes on the implementation

- **Concurrency.** Duplicate employees and duplicate punch-ins are stopped by unique indexes, not by checking first. Punch-out only updates a record whose `punch_out` is still null. PATCH uses a simple optimistic check (status, punch times and history length must be unchanged), so a conflicting correction gets a 409 instead of overwriting the other one.
- **Time handling.** Everything is stored as UTC. Shift times and attendance dates are worked out in IST. Instants are cut to whole seconds before any calculation. For night shifts, a punch-in before `shift_end` belongs to the previous day.
- **Rounding.** I use half-up rounding through `Decimal`, because Python's `round()` and MongoDB's `$round` don't round .5 cases the way the rules ask.
- **Analytics.** Everything is done in aggregation pipelines. The department summary starts from `employees`, so people with no logs are still counted. The leaderboard uses `$rank` so ties share a rank. The trend uses `$densify` to get one row per day, and `$setWindowFields` for headcount and the 7-day average.

## Where I had to interpret the spec

- Records dated before an employee's `joined_on` are counted as stored; only `working_days` starts from `joined_on`.
- The department summary only includes employees who joined by the end of the month.
- The trend's 92-day limit counts both ends, so `from`..`to` spanning 93 days is a 422.
- A PATCH that wouldn't change any field (including the recomputed ones) is a 422.
- For a night shift, a punch-in at exactly `shift_end` counts for that day, since the rule says "earlier than".
- The 7-day moving average is the mean of the rates as reported (already rounded to 4 decimals).

## Test scripts

These are separate from the app and are not used by it:

- `scripts/check_rules.py`: the rule helpers against the examples in the spec and the sample data. It doesn't need the server.
- `scripts/check_api.py`: status codes, validation and parallel requests against a running server.
- `scripts/check_analytics.py`: creates a small tricky dataset and compares the analytics with a plain-Python calculation. It also checks the explain plans.
- `scripts/load_test.py`: inserts ~100k records and times the endpoints. Use `--cleanup` to remove them.
