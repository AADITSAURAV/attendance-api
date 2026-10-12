# REVIEW.md

My review of the starter `app/main.py`. Line numbers refer to the starter as I received it (first commit, `51a6f99`). For each issue: what was wrong, how you would notice it, and what I changed.

## Setup and connection

**1. Naive datetimes (L22).** The `MongoClient` was created without `tz_aware=True`, so PyMongo hands back datetimes with no timezone. Any conversion after that quietly assumes the machine's local zone. For example, calling `.timestamp()` on a seeded punch on an IST machine gives a value 5:30 off. *Fix:* `MongoClient(..., tz_aware=True)`, and I treat every instant as UTC.

**2. Hard-coded connection fallback (L22-23).** If `MONGO_URI` / `MONGO_DB` were missing, it silently connected to `localhost:27017/attendance_db`. The brief says not to hard-code connection details. *Fix:* the values are read from the environment only (`.env` is loaded, but real env vars win), and the app fails at startup with a clear message if they're missing.

**3. No indexes (whole file).** Nothing created any indexes. That means no uniqueness on `emp_code` or on `(emp_code, date)`, and every list or analytics query is a full collection scan. You can see it in `index_information()` (only `_id_`), or as `COLLSCAN` in explain. *Fix:* `create_indexes()` runs in the FastAPI lifespan at startup. It's safe to run again, and it adds the two unique indexes plus the ones the queries need.

**4. `/health` never checks the DB (L73).** It always returned 200, even with MongoDB stopped. *Fix:* it pings MongoDB and returns 503 if that fails.

## Helper functions

**5. Late minutes compared in the wrong timezone (`compute_late_minutes`, L33-35).** It built the shift start with `punch_in.replace(hour=..., minute=...)`, which keeps the punch's own timezone. With a UTC punch, 10:05 IST (04:35 UTC) gets compared with 09:30 *UTC*, and the result is 0 instead of 35. *Fix:* the shift start is built in IST on the attendance date.

**6. Grace period checked in whole minutes (L36-37).** `minutes > 10` on truncated minutes means 09:40:30 counts as "10 minutes, not late", but the rule says late is anything strictly after 10:00. It also never dropped milliseconds. *Fix:* I compare seconds (`> 600`), then return whole minutes since shift start. Punch times are cut to whole seconds before any calculation.

**7. No overnight shift handling (`compute_late_minutes`).** For a 22:00-06:00 shift, a punch at 01:00 should belong to the previous day and be 180 minutes late. The starter compared it with 22:00 on the same day. *Fix:* `attendance_date()` works out the right day, and the shift start is placed on that day.

**8. `round()` isn't half-up (`compute_work_hours`, L41).** Python's `round` on floats doesn't follow the half-up rule. For example, 54 seconds is 0.015 h, and `round` gives 0.01 where the rule wants 0.02. *Fix:* `Decimal` with `ROUND_HALF_UP`.

**9. No 30-minute minimum for overtime (`compute_overtime`, L47).** Leaving at 18:35 on an 18:30 shift gave 5 minutes of overtime. The rule says anything under 30 is 0. *Fix:* return the minutes only when they're 30 or more.

**10. Overnight shift end on the wrong day (`compute_overtime`, L45-46).** The shift end was always put on the record's date. For a 22:00-06:00 shift, that's the morning *before* the shift, so a 06:40 punch-out showed more than a day of overtime instead of 40 minutes. *Fix:* the shift end moves to the next day when `shift_end <= shift_start`.

## Employees

**11. No input validation (`EmployeeIn`, L53).** `{"emp_code": "E1", "email": "nope", "shift_start": "9:30", "joined_on": "2026-02-30"}` was accepted with a 201. It also allowed `shift_start == shift_end`. *Fix:* Pydantic patterns and lengths from the contract, a real date check, and a validator that rejects equal shift times.

**12. Duplicate employees under concurrency (`create_employee`, L79-83).** It checked with `find_one` and then inserted. Two requests can both pass the check at the same time and both insert. Firing a few identical creates in parallel would show more than one 201. *Fix:* no check first. I insert and let the unique index decide; the duplicate-key error becomes a 409.

**13. `created_at` in the wrong format (L82).** It used `datetime.now()` (naive, local time) and returned it as a datetime string, but the contract wants epoch milliseconds. *Fix:* the current UTC time is stored, and it's returned as epoch ms.

**14. First page always empty (`list_employees`, L93).** `skip = page * page_size` skips the first page, so `GET /employees` returned `items: []` with `total: 6`. *Fix:* `(page - 1) * page_size`.

**15. `total` ignores the filter (L94).** `count_documents({})` counted everyone, so `?department=Sales` still said 6. *Fix:* count with the same filter.

**16. No sort and no paging limits (L89-95).** The order wasn't defined, and `page=0` or `page_size=10000` were accepted. *Fix:* sort by `emp_code`; `page >= 1` and `1 <= page_size <= 100`, otherwise 422.

## Punch-in

**17. Unknown employee causes a 500 (L101, L113).** `find_one` returns `None`, then `emp["shift_start"]` throws a `TypeError`. *Fix:* return 404 when the employee doesn't exist.

**18. Wrong attendance date (L102-103).** `datetime.fromtimestamp()` uses the server's timezone, so on a UTC server a 00:30 IST punch got dated the previous day. There was also no night-shift rule and no truncation. `if body.punched_at` treats a falsy value as "not sent". *Fix:* convert ms to UTC, cut to seconds, then compute the IST attendance date with the night-shift rule.

**19. `punched_at` and `status` not validated (`PunchInIn`, L65-66).** A lax `int` accepts `"1783312500000"` as a string, `1.7e12`, `true`, and values in seconds. `status: str` accepted `ABSENT`, or anything else. *Fix:* a strict int with a range that rejects second-based values, and `status` limited to PRESENT / WFH / ON_DUTY.

**20. Duplicate punch-ins under concurrency (L104).** It's the same check-then-insert problem as employees: parallel identical punch-ins could both get a 201. *Fix:* the unique `(emp_code, date)` index, with the duplicate-key error returned as 409.

**21. Response doesn't match the contract (L119-121).** It returned an `id` field and raw datetimes. The contract has no id, and instants must be epoch ms. *Fix:* one function, `record_out()`, builds every attendance response.

## Attendance list

**22. Whole collection loaded into memory (`list_attendance`, L144-146).** It did `list(find(q))`, then sorted and sliced in Python. That works on 9 records but not on 100k. *Fix:* sort, skip and limit happen in MongoDB on an index, and `count_documents` gives the total.

**23. Sort only by date (L145).** Records on the same date came back in no particular order; the contract wants `emp_code` ascending after date. *Fix:* sort by date descending, then `emp_code` ascending.

**24. No validation on filters (L125-131).** `?date_from=2026-13-01`, `?status=HOLIDAY`, and `date_from` after `date_to` all returned 200. Paging wasn't limited either. *Fix:* date validation, a status enum, 422 when `date_from > date_to`, and the same paging limits as employees.

**25. Legacy records and ids (L148-149).** It returned `id` and datetimes, and older records missing `history` / `half_day` came back without them (sample record #9). *Fix:* `record_out()` fills in `history: []`, `half_day: false` and 0 for missing minutes.

## Things I checked and left alone

- **Sync `def` endpoints with PyMongo.** FastAPI runs these in a thread pool, so blocking DB calls don't freeze the server. The correctness under concurrency comes from the database (unique indexes and conditional updates), not from async.
- **`load_dotenv()`.** It already defaults to not overriding real environment variables. I only made `override=False` explicit.
- **Dates and shift times stored as strings.** That's how DATA_MODEL.md defines them, and `YYYY-MM-DD` strings sort in date order, so range queries on them work.
- **`IST = timezone(timedelta(hours=5, minutes=30))`.** That's correct; India has no daylight saving, so a fixed offset is fine.
