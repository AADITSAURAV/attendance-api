"""Analytics checks against the RUNNING API + your real MongoDB.

Run from the project root (server running):   python scripts/check_analytics.py

1. Builds an awkward dataset for one far-future month through the API:
   late/early/half-day/weekend/LEAVE/ABSENT/ON_DUTY/open records, a tie in
   late minutes, an employee with no logs, a mid-month joiner, someone who joins
   after the month, an overnight shift, and logs of a non-existent employee
   (inserted straight into MongoDB, since the API refuses them).
2. Computes the expected answers independently in plain Python.
3. Compares them with what the four analytics endpoints return, and checks
   that the explain endpoint shows IXSCAN and no COLLSCAN.
"""
import calendar
import json
import os
import random
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from dotenv import load_dotenv
from pymongo import MongoClient

BASE = "http://127.0.0.1:8000"
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
failures = 0


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode(errors="replace")


def must(method, path, body, expected_status):
    status, resp = call(method, path, body)
    if status != expected_status:
        print(f"SETUP FAILED: {method} {path} {body} -> {status} {resp}")
        sys.exit(2)
    return resp


def check(label, got, expected):
    global failures
    ok = got == expected
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got:      {got!r}\n        expected: {expected!r}")


def half_up(value, places):
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def q(params):
    return urllib.parse.urlencode(params)


# --------------------------------------------------------------------------- #
# Dataset
# --------------------------------------------------------------------------- #
RUN = random.randint(10000, 99999)
YEAR, MON = 2050 + RUN % 40, RUN % 12 + 1
MONTH = f"{YEAR}-{MON:02d}"
FIRST = date(YEAR, MON, 1)
LAST = date(YEAR, MON, calendar.monthrange(YEAR, MON)[1])
NEXT_FIRST = LAST + timedelta(days=1)
WEEKDAYS = [FIRST + timedelta(days=i) for i in range((LAST - FIRST).days + 1)
            if (FIRST + timedelta(days=i)).weekday() < 5]
WEEKEND = next(FIRST + timedelta(days=i) for i in range(7) if (FIRST + timedelta(days=i)).weekday() == 5)
DEPT, DEPT2 = f"QA-AN-{RUN}-A", f"QA-AN-{RUN}-B"
print(f"Test month {MONTH}, departments {DEPT} / {DEPT2}")


def code(n):
    return f"EMP{RUN}{n}"


def ms(d, hh, mm, ss=0):
    return int(datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=IST).timestamp()) * 1000


def employee(n, dept=DEPT, joined="2040-01-01", start="09:30", end="18:30", name=None):
    must("POST", "/employees", {"emp_code": code(n), "name": name or f"Person {n}", "email": f"p{n}@x.com",
                                "department": dept, "shift_start": start, "shift_end": end,
                                "joined_on": joined}, 201)
    return code(n)


def worked(c, d, t_in, t_out=None, status="PRESENT", out_day=None):
    must("POST", "/attendance/punch-in", {"emp_code": c, "punched_at": ms(d, *t_in), "status": status}, 201)
    if t_out:
        must("POST", "/attendance/punch-out", {"emp_code": c, "punched_at": ms(out_day or d, *t_out)}, 200)


def not_worked(c, d, status):
    """No endpoint creates ABSENT/LEAVE, so: punch in, then correct the status."""
    must("POST", "/attendance/punch-in", {"emp_code": c, "punched_at": ms(d, 9, 0)}, 201)
    must("PATCH", f"/attendance/{c}/{d.isoformat()}",
         {"status": status, "reason": "test data", "regularized_by": "checker"}, 200)


wd = WEEKDAYS
A = employee(1)
worked(A, wd[0], (9, 28), (18, 35))                   # on time, overtime 5 -> 0
worked(A, wd[1], (10, 5), (19, 10))                   # late 35, overtime 40
worked(A, wd[2], (9, 30), (13, 0), status="WFH")      # 3.5 h -> half day
not_worked(A, wd[3], "LEAVE")
not_worked(A, wd[4], "ABSENT")
worked(A, wd[5], (9, 45), (18, 30), status="ON_DUTY") # late 15
worked(A, WEEKEND, (11, 30), (15, 0))                 # weekend: late 120, half day
worked(A, wd[6], (9, 40, 1))                          # open record, late 10
B = employee(2)
worked(B, wd[0], (10, 30), (19, 0))                   # late 60
worked(B, wd[1], (9, 30), (18, 30))
C = employee(3)
worked(C, wd[2], (10, 0, 59), (18, 0))                # late 30
worked(C, wd[3], (10, 0), (18, 30))                   # late 30 -> total 60 = tie with B
D = employee(4)                                       # no logs at all
mid = FIRST.replace(day=15)
E = employee(5, joined=mid.isoformat())               # mid-month joiner
worked(E, wd[-1], (9, 30), (18, 30))
F = employee(6, joined=NEXT_FIRST.isoformat())        # joins after the month
N = employee(7, start="22:00", end="06:00")           # overnight shift
worked(N, wd[0], (22, 30), (6, 40), out_day=wd[0] + timedelta(days=1))   # late 30, overtime 40
worked(N, wd[1], (23, 15), (7, 0), out_day=wd[1] + timedelta(days=1))    # late 75, overtime 60
G = employee(8, dept=DEPT2)
worked(G, wd[0], (9, 30), (18, 30))

# Logs of an employee that does not exist - must be ignored everywhere.
load_dotenv()
mongo = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]]
GHOST = f"EMP{RUN}9"
mongo.attendance_logs.insert_one({
    "emp_code": GHOST, "date": wd[0].isoformat(), "status": "PRESENT",
    "punch_in": datetime(wd[0].year, wd[0].month, wd[0].day, 6, 0, tzinfo=timezone.utc),
    "punch_out": None, "work_hours": None, "late_minutes": 500, "overtime_minutes": 0,
    "half_day": False, "history": []})

# --------------------------------------------------------------------------- #
# Independent reference implementation (plain Python over stored records)
# --------------------------------------------------------------------------- #
EMPLOYEES = {}
for dept in (DEPT, DEPT2):
    for e in call("GET", "/employees?" + q({"department": dept, "page_size": 100}))[1]["items"]:
        EMPLOYEES[e["emp_code"]] = e


def records(c, d_from, d_to):
    return call("GET", "/attendance?" + q({"emp_code": c, "date_from": d_from.isoformat(),
                                           "date_to": d_to.isoformat(), "page_size": 100}))[1]["items"]


def presence(r, weekdays_only=True):
    if r["status"] not in PRESENCE:
        return 0
    if weekdays_only and date.fromisoformat(r["date"]).weekday() >= 5:
        return 0
    return 0.5 if r["half_day"] else 1


def expected_monthly(c):
    emp = EMPLOYEES[c]
    start = max(FIRST, date.fromisoformat(emp["joined_on"]))
    working = sum(1 for i in range((LAST - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)
    recs = records(c, FIRST, LAST)
    present = half_up(sum(presence(r) for r in recs), 2)
    return {
        "emp_code": c, "month": MONTH, "working_days": working, "present_days": present,
        "leave_days": sum(r["status"] == "LEAVE" for r in recs),
        "late_count": sum(r["late_minutes"] > 0 for r in recs),
        "total_late_minutes": sum(r["late_minutes"] for r in recs),
        "total_overtime_minutes": sum(r["overtime_minutes"] for r in recs),
        "attendance_pct": half_up(Decimal(str(present)) * 100 / working, 2) if working else None,
    }


def expected_summary(dept):
    members = [c for c, e in EMPLOYEES.items() if e["department"] == dept and e["joined_on"] <= LAST.isoformat()]
    recs = [r for c in members for r in records(c, FIRST, LAST)]
    hours = [r["work_hours"] for r in recs if r["status"] in PRESENCE and r["work_hours"] is not None]
    return {
        "department": dept, "headcount": len(members),
        "present_days": half_up(sum(presence(r) for r in recs), 2),
        "avg_work_hours": half_up(Decimal(str(sum(Decimal(str(h)) for h in hours))) / len(hours), 2) if hours else None,
        "late_count": sum(r["late_minutes"] > 0 for r in recs),
        "total_late_minutes": sum(r["late_minutes"] for r in recs),
        "leave_count": sum(r["status"] == "LEAVE" for r in recs),
        "on_duty_count": sum(r["status"] == "ON_DUTY" for r in recs),
    }


def expected_trend(dept, d_from, d_to):
    members = [e for e in EMPLOYEES.values() if e["department"] == dept]
    recs = [r for e in members for r in records(e["emp_code"], d_from, d_to)]
    rows, rates = [], []
    for i in range((d_to - d_from).days + 1):
        d = d_from + timedelta(days=i)
        working = d.weekday() < 5
        headcount = sum(e["joined_on"] <= d.isoformat() for e in members)
        today = [r for r in recs if r["date"] == d.isoformat()]
        present = sum(presence(r, weekdays_only=False) for r in today)
        rate = half_up(Decimal(str(present)) / headcount, 4) if working and headcount else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        avg = half_up(sum(Decimal(str(x)) for x in window) / len(window), 4) if window else None
        rows.append({"date": d.isoformat(), "is_working_day": working, "headcount": headcount,
                     "present_count": present, "late_count": sum(r["late_minutes"] > 0 for r in today),
                     "attendance_rate": rate, "moving_avg_7d": avg})
    return rows


# --------------------------------------------------------------------------- #
print("--- employee monthly ---")
for label, c in [("A (everything)", A), ("B", B), ("D (no logs)", D), ("E (mid-month joiner)", E),
                 ("F (joins after month)", F), ("N (overnight)", N)]:
    status, got = call("GET", f"/analytics/employees/{c}/monthly?month={MONTH}")
    check(f"monthly {label}", (status, got), (200, expected_monthly(c)))
got = call("GET", f"/analytics/employees/{A}/monthly?month={MONTH}")[1]
check("hand-checked A: present 4.5, leave 1, late 4 = 180 min, overtime 40",
      [got.get(k) for k in ("present_days", "leave_days", "late_count", "total_late_minutes",
                            "total_overtime_minutes")], [4.5, 1, 4, 180, 40])
check("F: working_days 0 -> attendance_pct null",
      [got_f.get("working_days"), got_f.get("attendance_pct")] if (got_f := call(
          "GET", f"/analytics/employees/{F}/monthly?month={MONTH}")[1]) else None, [0, None])
check("unknown employee -> 404", call("GET", f"/analytics/employees/EMP_NOPE/monthly?month={MONTH}")[0], 404)
check("bad month -> 422", call("GET", f"/analytics/employees/{A}/monthly?month={YEAR}-13")[0], 422)

print("--- department summary ---")
status, body = call("GET", f"/analytics/departments/summary?month={MONTH}&department={DEPT}")
check("filtered summary for dept A", (status, body.get("items")), (200, [expected_summary(DEPT)]))
check("hand-checked: headcount 6 (no-log employee in, future joiner out)",
      body["items"][0]["headcount"] if body.get("items") else None, 6)
status, body = call("GET", f"/analytics/departments/summary?month={MONTH}")
depts = [i["department"] for i in body.get("items", [])]
check("unfiltered: sorted by department", depts, sorted(depts))
check("unfiltered: our two departments present and correct",
      [i for i in body.get("items", []) if i["department"] in (DEPT, DEPT2)],
      [expected_summary(DEPT), expected_summary(DEPT2)])
check("no department with headcount 0", any(i["headcount"] == 0 for i in body.get("items", [])), False)
check("missing month -> 422", call("GET", "/analytics/departments/summary")[0], 422)

print("--- late leaderboard ---")
def lb(limit, dept=DEPT):
    params = {"month": MONTH, "limit": limit}
    if dept:
        params["department"] = dept
    return call("GET", "/analytics/leaderboard/late?" + q(params))[1].get("items")

rows = lb(10)
check("ranking 1, 2, 3, 3 (tie shares rank 3, ordered by emp_code)",
      [(r["rank"], r["emp_code"], r["total_late_minutes"], r["late_count"]) for r in rows],
      [(1, A, 180, 4), (2, N, 105, 2), (3, B, 60, 1), (3, C, 60, 2)])
check("row fields", sorted(rows[0]) if rows else None,
      sorted(["rank", "emp_code", "name", "department", "total_late_minutes", "late_count"]))
check("name/department come from employees", (rows[0]["name"], rows[0]["department"]) if rows else None,
      ("Person 1", DEPT))
check("limit=3 keeps everyone tied at rank 3 (4 rows)", len(lb(3) or []), 4)
check("limit=2 -> 2 rows", [r["emp_code"] for r in lb(2) or []], [A, N])
check("department B ranked on its own", lb(10, DEPT2), [])
all_rows = lb(50, dept=None) or []
check("non-existent employee's logs ignored", any(r["emp_code"] == GHOST for r in all_rows), False)
check("limit=0 -> 422", call("GET", f"/analytics/leaderboard/late?month={MONTH}&limit=0")[0], 422)

print("--- department trend ---")
for label, d_from, d_to in [("whole month", FIRST, LAST),
                            ("range crossing the joiner's first day", NEXT_FIRST - timedelta(days=10),
                             NEXT_FIRST + timedelta(days=4))]:
    status, body = call("GET", f"/analytics/departments/{DEPT}/trend?" + q({"from": d_from.isoformat(),
                                                                           "to": d_to.isoformat()}))
    got_rows = body.get("items", []) if isinstance(body, dict) else []
    want = expected_trend(DEPT, d_from, d_to)
    check(f"{label}: one row per day ({len(want)})", (status, len(got_rows)), (200, len(want)))
    bad = [(g, w) for g, w in zip(got_rows, want) if g != w]
    check(f"{label}: every row matches", bad[:2], [])
check("first row: window of 1 (moving avg = its own rate or null)",
      (lambda r: r["moving_avg_7d"] == r["attendance_rate"])(
          call("GET", f"/analytics/departments/{DEPT}/trend?" + q({"from": FIRST.isoformat(),
                                                                   "to": FIRST.isoformat()}))[1]["items"][0]),
      True)
check("92 days -> 200 with 92 rows",
      (lambda s_b: (s_b[0], len(s_b[1].get("items", []))))(call(
          "GET", f"/analytics/departments/{DEPT}/trend?" + q({"from": FIRST.isoformat(),
                                                             "to": (FIRST + timedelta(days=91)).isoformat()}))),
      (200, 92))
check("93 days -> 422", call("GET", f"/analytics/departments/{DEPT}/trend?" + q(
    {"from": FIRST.isoformat(), "to": (FIRST + timedelta(days=92)).isoformat()}))[0], 422)
check("to before from -> 422", call("GET", f"/analytics/departments/{DEPT}/trend?" + q(
    {"from": LAST.isoformat(), "to": FIRST.isoformat()}))[0], 422)
check("unknown department -> 404", call("GET", "/analytics/departments/NO-SUCH-DEPT/trend?" + q(
    {"from": FIRST.isoformat(), "to": LAST.isoformat()}))[0], 404)

print("--- explain ---")
explains = {
    "attendance_list": {"emp_code": A},
    "employee_monthly": {"emp_code": A, "month": MONTH},
    "department_summary": {"month": MONTH},
    "late_leaderboard": {"month": MONTH},
    "department_trend": {"department": DEPT, "from": FIRST.isoformat(), "to": LAST.isoformat()},
}
for name, params in explains.items():
    status, body = call("GET", f"/admin/explain/{name}?" + q(params))
    text = json.dumps(body)
    check(f"explain {name}: 200, IXSCAN, no COLLSCAN",
          (status, "IXSCAN" in text, "COLLSCAN" in text), (200, True, False))
check("explain missing month -> 422", call("GET", "/admin/explain/employee_monthly?" + q({"emp_code": A}))[0], 422)
check("explain unknown endpoint -> 422", call("GET", "/admin/explain/nope")[0], 422)

print()
print("ALL PASSED" if failures == 0 else f"{failures} FAILED")
sys.exit(1 if failures else 0)
