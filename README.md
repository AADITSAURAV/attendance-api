# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                                  # set MONGO_URI / MONGO_DB (real env vars take priority)
python sample_seed.py                                 # optional: the kit's nine sample documents
uvicorn app.main:app --port 8000
```
Requires MongoDB 6.0+ (developed and tested on 7.0). Indexes are created automatically at startup.

## Checks (not part of the app; need the server running)
- `python scripts/check_rules.py` - business rules R1-R5 against the spec's examples and the sample data (no server needed).
- `python scripts/check_api.py` - CRUD, validation, status codes, concurrency (parallel punch-in/out/PATCH).
- `python scripts/check_analytics.py` - builds an awkward dataset and compares all analytics with an independent Python implementation; checks explain plans.
- `python scripts/load_test.py` - seeds ~100,000 records and times every endpoint + explain (`--cleanup` removes them).

## Interpretations (where the spec left room)
- Records dated before an employee's `joined_on` are counted as stored; only `working_days` starts at `joined_on` (R7).
- Department summary includes only employees who joined by the month's end (R9); their logs are the department's logs.
- Trend range limit: more than 92 days counted inclusively -> 422.
- A PATCH "changes nothing" when no tracked field (including recomputed derived fields) would change -> 422.
- Overnight shift: a punch-in exactly at `shift_end` belongs to that day (the rule says "earlier than").
- Attendance rates are rounded half-up to 4 decimals; the 7-day moving average is the mean of those reported rates.
