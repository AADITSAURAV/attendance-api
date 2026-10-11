"""Speed + index check at grader scale (~100,000 attendance records).

Run from the project root, with the server running:
    python scripts/load_test.py            seed (once) + time every endpoint + check explain plans
    python scripts/load_test.py --cleanup  delete the generated data again

The generated data lives in its own employees (EMP0xxxxx, departments LOAD-*)
and its own dates (2080), so it does not disturb the sample or test data.
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

BASE = "http://127.0.0.1:8000"
N_EMPLOYEES, N_DAYS = 1000, 100                    # 1000 x 100 = 100,000 records
START = date(2080, 1, 1)
DEPARTMENTS = [f"LOAD-D{i}" for i in range(10)]
LIMIT_MS = 1000                                     # flag anything slower than this

load_dotenv()
db = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]]
load_filter = {"emp_code": {"$regex": "^EMP0\\d{5}$"}}

if "--cleanup" in sys.argv:
    print("deleted logs:", db.attendance_logs.delete_many(load_filter).deleted_count)
    print("deleted employees:", db.employees.delete_many(load_filter).deleted_count)
    sys.exit(0)

# --------------------------------------------------------------------------- #
existing = db.attendance_logs.count_documents(load_filter)
if existing < N_EMPLOYEES * N_DAYS:
    print("seeding ... (one-off, takes a few seconds)")
    db.attendance_logs.delete_many(load_filter)
    db.employees.delete_many(load_filter)
    rnd = random.Random(42)
    employees = []
    for i in range(N_EMPLOYEES):
        employees.append({
            "emp_code": f"EMP0{i:05d}", "name": f"Load {i}", "email": f"load{i}@x.com",
            "department": DEPARTMENTS[i % 10], "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": (START + timedelta(days=rnd.choice([-400, -30, 0, 20, 45]))).isoformat(),
            "created_at": datetime(2079, 1, 1, tzinfo=timezone.utc),
        })
    db.employees.insert_many(employees)
    batch = []
    for e in employees:
        for d in range(N_DAYS):
            day = START + timedelta(days=d)
            status = rnd.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [70, 12, 5, 6, 7])[0]
            doc = {"emp_code": e["emp_code"], "date": day.isoformat(), "status": status,
                   "punch_in": None, "punch_out": None, "work_hours": None,
                   "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
            if status in ("PRESENT", "WFH", "ON_DUTY"):
                late = rnd.choice([0, 0, 0, 0, 12, 25, 47])
                hours = rnd.choice([3.5, 8.25, 9.0, 9.12])
                start = datetime(day.year, day.month, day.day, 4, 0, tzinfo=timezone.utc) + timedelta(minutes=late)
                doc.update({"punch_in": start, "punch_out": start + timedelta(hours=hours),
                            "work_hours": hours, "late_minutes": late, "half_day": hours < 4.5,
                            "overtime_minutes": rnd.choice([0, 0, 35])})
            batch.append(doc)
            if len(batch) == 5000:
                db.attendance_logs.insert_many(batch)
                batch = []
    if batch:
        db.attendance_logs.insert_many(batch)
print("attendance_logs in database:", db.attendance_logs.estimated_document_count())


# --------------------------------------------------------------------------- #
def timed(path):
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(BASE + path, timeout=60) as resp:
            status, body = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        status, body = err.code, err.read().decode(errors="replace")
    return status, (time.perf_counter() - started) * 1000, body


slow = 0
print("\n--- timings (each endpoint called twice; second call shown) ---")
endpoints = {
    "list attendance (no filter)": "/attendance?page=3&page_size=100",
    "list attendance (date range)": "/attendance?date_from=2080-02-01&date_to=2080-02-29&page=5",
    "list attendance (status)": "/attendance?status=LEAVE&page=2",
    "list attendance (employee)": "/attendance?emp_code=EMP000123",
    "employee monthly": "/analytics/employees/EMP000123/monthly?month=2080-02",
    "department summary (all)": "/analytics/departments/summary?month=2080-02",
    "department summary (one)": "/analytics/departments/summary?month=2080-02&department=LOAD-D3",
    "late leaderboard (all)": "/analytics/leaderboard/late?month=2080-02&limit=10",
    "late leaderboard (one dept)": "/analytics/leaderboard/late?month=2080-02&department=LOAD-D3",
    "department trend (92 days)": "/analytics/departments/LOAD-D3/trend?from=2080-01-01&to=2080-04-01",
}
for label, path in endpoints.items():
    timed(path)
    status, ms, _ = timed(path)
    flag = "OK  " if status == 200 and ms < LIMIT_MS else "SLOW" if status == 200 else "FAIL"
    slow += flag != "OK  "
    print(f"{flag} {ms:7.1f} ms  {status}  {label}")

print("\n--- explain plans at this size ---")
explains = {
    "attendance_list": {"status": "LEAVE"},
    "employee_monthly": {"emp_code": "EMP000123", "month": "2080-02"},
    "department_summary": {"month": "2080-02"},
    "late_leaderboard": {"month": "2080-02"},
    "department_trend": {"department": "LOAD-D3", "from": "2080-01-01", "to": "2080-04-01"},
}
for name, params in explains.items():
    status, ms, body = timed(f"/admin/explain/{name}?" + urllib.parse.urlencode(params))
    text = json.dumps(body)
    ok = status == 200 and "IXSCAN" in text and "COLLSCAN" not in text
    slow += not ok
    print(f"{'OK  ' if ok else 'FAIL'} explain {name}: IXSCAN={'IXSCAN' in text} COLLSCAN={'COLLSCAN' in text}")

print()
print("ALL FAST AND INDEXED" if slow == 0 else f"{slow} PROBLEM(S) - send me this output")
