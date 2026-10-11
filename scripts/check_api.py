"""End-to-end checks against the RUNNING API (http://127.0.0.1:8000).

Start the server first, then run from the project root:
    python scripts/check_api.py

Uses only the Python standard library. Every run uses fresh, random employee
codes (EMPnnnnnn) and a fresh department name, so it can be run many times.
"""
import json
import random
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

BASE = "http://127.0.0.1:8000"
failures = 0


def call(method, path, body=None):
    """Send one request; return (status_code, parsed_json_or_text)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode(errors="replace")


def check(label, got, expected):
    global failures
    ok = got == expected
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got!r}, expected {expected!r}")


RUN = random.randint(10000, 99999)          # makes this run's data unique
DEPT = f"QA-{RUN}"
IST = timezone(timedelta(hours=5, minutes=30))
# A run-specific block of calendar days far in the future, so filters by date
# only ever see this run's records. BASE_DAY is a Monday.
BASE_DAY = date(2040, 1, 2) + timedelta(weeks=RUN % 2500)


def day(n):
    """BASE_DAY + n days, as a date."""
    return BASE_DAY + timedelta(days=n)


def ist_ms(d, hh, mm, ss=0, ms=0):
    """Epoch milliseconds of an IST wall-clock time on date d."""
    dt = datetime(d.year, d.month, d.day, hh, mm, ss, ms * 1000, tzinfo=IST)
    return int(dt.timestamp()) * 1000 + ms


def emp_code(n):
    return f"EMP{RUN}{n}"  # EMP + 5 random digits + 1 digit = matches ^EMP\d{4,6}$


def new_employee(code, **overrides):
    body = {"emp_code": code, "name": "Test Person", "email": "test@example.com",
            "department": DEPT, "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-07-01"}
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
print("--- /health ---")
status, body = call("GET", "/health")
check("GET /health", (status, body), (200, {"status": "ok"}))

# --------------------------------------------------------------------------- #
print("--- POST /employees ---")
code_a = emp_code(1)
status, body = call("POST", "/employees", new_employee(code_a))
check("create -> 201", status, 201)
check("response fields", sorted(body) if isinstance(body, dict) else body,
      sorted(["emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"]))
check("created_at is epoch ms (int)", isinstance(body.get("created_at"), int) and body["created_at"] > 10**11, True)
check("duplicate emp_code -> 409", call("POST", "/employees", new_employee(code_a))[0], 409)

bad_inputs = {
    "emp_code wrong format": new_employee("E1"),
    "email without @": new_employee(emp_code(2), email="nope"),
    "shift_start 9:30 (not HH:MM)": new_employee(emp_code(2), shift_start="9:30"),
    "shift_start == shift_end": new_employee(emp_code(2), shift_start="09:00", shift_end="09:00"),
    "joined_on 2026-02-30": new_employee(emp_code(2), joined_on="2026-02-30"),
    "name empty": new_employee(emp_code(2), name=""),
}
for label, payload in bad_inputs.items():
    check(f"{label} -> 422", call("POST", "/employees", payload)[0], 422)
missing = new_employee(emp_code(2))
del missing["department"]
check("missing department -> 422", call("POST", "/employees", missing)[0], 422)

code_night = emp_code(3)
status, body = call("POST", "/employees", new_employee(code_night, shift_start="22:00", shift_end="06:00"))
check("overnight shift 22:00-06:00 accepted", status, 201)

print("--- concurrent duplicate creates ---")
code_race = emp_code(4)
with ThreadPoolExecutor(max_workers=10) as pool:
    statuses = sorted(pool.map(lambda _: call("POST", "/employees", new_employee(code_race))[0], range(10)))
check("10 parallel creates: one 201, nine 409", statuses, [201] + [409] * 9)

# --------------------------------------------------------------------------- #
print("--- GET /employees ---")
status, body = call("GET", f"/employees?department={DEPT}")
check("filter by department -> total 3", (status, body.get("total")), (200, 3))
codes = [e["emp_code"] for e in body.get("items", [])]
check("sorted by emp_code", codes, sorted(codes))
check("no _id leaked", any("_id" in e for e in body.get("items", [])), False)

status, body = call("GET", f"/employees?department={DEPT}&page=2&page_size=2")
check("page 2, size 2 -> 1 item, total still 3", (len(body.get("items", [])), body.get("total")), (1, 3))
status, body = call("GET", "/employees?page=1&page_size=20")
check("page 1 is not empty", len(body.get("items", [])) > 0, True)
check("page=0 -> 422", call("GET", "/employees?page=0")[0], 422)
check("page_size=101 -> 422", call("GET", "/employees?page_size=101")[0], 422)

# --------------------------------------------------------------------------- #
print("--- POST /attendance/punch-in ---")
DAY_EMP, NIGHT_EMP = code_a, code_night            # created above (09:30-18:30 and 22:00-06:00)
RECORD_FIELDS = sorted(["emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
                        "late_minutes", "overtime_minutes", "half_day", "history"])

status, rec = call("POST", "/attendance/punch-in",
                   {"emp_code": DAY_EMP, "punched_at": ist_ms(day(0), 10, 15, 59, 900)})
check("punch-in -> 201", status, 201)
check("record fields (no id/_id)", sorted(rec) if isinstance(rec, dict) else rec, RECORD_FIELDS)
check("R1 date is the IST date", rec.get("date"), day(0).isoformat())
check("R1 punch_in truncated to whole seconds", rec.get("punch_in"), ist_ms(day(0), 10, 15, 59))
check("R2 late 10:15:59 -> 45", rec.get("late_minutes"), 45)
check("defaults: status/punch_out/work_hours/overtime/half_day/history",
      [rec.get(k) for k in ("status", "punch_out", "work_hours", "overtime_minutes", "half_day", "history")],
      ["PRESENT", None, None, 0, False, []])

check("second punch-in same day -> 409",
      call("POST", "/attendance/punch-in", {"emp_code": DAY_EMP, "punched_at": ist_ms(day(0), 11, 0)})[0], 409)
check("unknown employee -> 404",
      call("POST", "/attendance/punch-in", {"emp_code": "EMP_NOPE", "punched_at": ist_ms(day(0), 9, 0)})[0], 404)

bad_punches = {
    "status ABSENT": {"emp_code": DAY_EMP, "status": "ABSENT"},
    "status LEAVE": {"emp_code": DAY_EMP, "status": "LEAVE"},
    "punched_at in seconds": {"emp_code": DAY_EMP, "punched_at": ist_ms(day(1), 9, 0) // 1000},
    "punched_at float": {"emp_code": DAY_EMP, "punched_at": float(ist_ms(day(1), 9, 0))},
    "punched_at string": {"emp_code": DAY_EMP, "punched_at": str(ist_ms(day(1), 9, 0))},
    "punched_at boolean": {"emp_code": DAY_EMP, "punched_at": True},
    "punched_at beyond 2100": {"emp_code": DAY_EMP, "punched_at": 4_102_444_800_001},
    "missing emp_code": {"punched_at": ist_ms(day(1), 9, 0)},
}
for label, payload in bad_punches.items():
    check(f"{label} -> 422", call("POST", "/attendance/punch-in", payload)[0], 422)

status, rec = call("POST", "/attendance/punch-in",
                   {"emp_code": DAY_EMP, "punched_at": ist_ms(day(1), 9, 40, 0), "status": "WFH"})
check("WFH at 09:40:00 -> 201, late 0", (status, rec.get("status"), rec.get("late_minutes")), (201, "WFH", 0))

status, rec = call("POST", "/attendance/punch-in",
                   {"emp_code": NIGHT_EMP, "punched_at": ist_ms(day(1), 1, 0)})
check("night shift 01:00 belongs to previous day (R1)", (status, rec.get("date")), (201, day(0).isoformat()))
check("night shift 01:00 late 180 min (R2)", rec.get("late_minutes"), 180)

print("--- concurrent punch-ins ---")
code_race2 = emp_code(5)
call("POST", "/employees", new_employee(code_race2))
with ThreadPoolExecutor(max_workers=10) as pool:
    statuses = sorted(pool.map(lambda _: call("POST", "/attendance/punch-in",
                                              {"emp_code": code_race2, "punched_at": ist_ms(day(2), 9, 0)})[0],
                               range(10)))
check("10 parallel punch-ins: one 201, nine 409", statuses, [201] + [409] * 9)

# --------------------------------------------------------------------------- #
print("--- GET /attendance ---")
status, body = call("GET", f"/attendance?emp_code={DAY_EMP}")
check("filter by emp_code -> 2 records", (status, body.get("total")), (200, 2))
check("sorted by date descending", [r["date"] for r in body.get("items", [])],
      [day(1).isoformat(), day(0).isoformat()])

d0 = day(0).isoformat()
status, body = call("GET", f"/attendance?date_from={d0}&date_to={d0}")
check("one day -> both employees", body.get("total"), 2)
check("same date sorted by emp_code ascending", [r["emp_code"] for r in body.get("items", [])],
      sorted([DAY_EMP, NIGHT_EMP]))
status, body = call("GET", f"/attendance?date_from={d0}&date_to={day(2).isoformat()}&status=WFH")
check("status filter WFH -> 1", body.get("total"), 1)
status, body = call("GET", f"/attendance?date_from={d0}&date_to={day(2).isoformat()}&page=2&page_size=2")
check("page 2 of 4 records, size 2 -> 2 items, total 4", (len(body.get("items", [])), body.get("total")), (2, 4))

check("date_from after date_to -> 422",
      call("GET", f"/attendance?date_from={day(2).isoformat()}&date_to={d0}")[0], 422)
check("invalid date 2026-13-01 -> 422", call("GET", "/attendance?date_from=2026-13-01")[0], 422)
check("unknown status -> 422", call("GET", "/attendance?status=HOLIDAY")[0], 422)
check("page_size=101 -> 422", call("GET", "/attendance?page_size=101")[0], 422)

status, body = call("GET", "/attendance?emp_code=EMP0002&date_from=2026-07-14&date_to=2026-07-14")
if body.get("total") == 1:  # only when the kit's sample data is loaded
    legacy = body["items"][0]
    check("legacy record: missing history -> [], half_day -> false",
          (legacy["history"], legacy["half_day"]), ([], False))

# --------------------------------------------------------------------------- #
print("--- POST /attendance/punch-out ---")
# DAY_EMP has open records on day(0) (in 10:15:59, late 45) and day(1) (WFH, in 09:40).
status, rec = call("POST", "/attendance/punch-out", {"emp_code": DAY_EMP, "punched_at": ist_ms(day(1), 13, 0)})
check("closes the most recent record (day 1)", (status, rec.get("date")), (200, day(1).isoformat()))
check("R4 work 3h20m -> 3.33, R5 half day, R3 no overtime",
      [rec.get(k) for k in ("work_hours", "half_day", "overtime_minutes", "late_minutes")], [3.33, True, 0, 0])
check("punch_out returned as epoch ms", rec.get("punch_out"), ist_ms(day(1), 13, 0))

status, rec = call("POST", "/attendance/punch-out", {"emp_code": DAY_EMP, "punched_at": ist_ms(day(0), 19, 30)})
check("punch_in <= punched_at picks day 0", (status, rec.get("date")), (200, day(0).isoformat()))
check("R4 9h14m01s -> 9.23, R3 overtime 60, late kept 45",
      [rec.get(k) for k in ("work_hours", "overtime_minutes", "late_minutes", "half_day")], [9.23, 60, 45, False])
check("history untouched by punch-out", rec.get("history"), [])
check("already punched out -> 409",
      call("POST", "/attendance/punch-out", {"emp_code": DAY_EMP, "punched_at": ist_ms(day(0), 20, 0)})[0], 409)

status, rec = call("POST", "/attendance/punch-out", {"emp_code": NIGHT_EMP, "punched_at": ist_ms(day(1), 6, 40)})
check("overnight: closes across midnight", (status, rec.get("date")), (200, day(0).isoformat()))
check("overnight: work 5.67, overtime 40",
      [rec.get(k) for k in ("work_hours", "overtime_minutes")], [5.67, 40])

check("unknown employee -> 404",
      call("POST", "/attendance/punch-out", {"emp_code": "EMP_NOPE", "punched_at": ist_ms(day(0), 18, 0)})[0], 404)
code_nolog = emp_code(6)
call("POST", "/employees", new_employee(code_nolog))
check("employee with no punch-in -> 404",
      call("POST", "/attendance/punch-out", {"emp_code": code_nolog, "punched_at": ist_ms(day(0), 18, 0)})[0], 404)

call("POST", "/attendance/punch-in", {"emp_code": code_nolog, "punched_at": ist_ms(day(3), 9, 0)})
check("punch-out before any punch-in -> 404",
      call("POST", "/attendance/punch-out", {"emp_code": code_nolog, "punched_at": ist_ms(day(2), 18, 0)})[0], 404)
check("punched_at == punch_in -> 422",
      call("POST", "/attendance/punch-out", {"emp_code": code_nolog, "punched_at": ist_ms(day(3), 9, 0)})[0], 422)
check("more than 24 h after punch_in -> 422",
      call("POST", "/attendance/punch-out", {"emp_code": code_nolog, "punched_at": ist_ms(day(4), 9, 0, 1)})[0], 422)
check("punched_at in seconds -> 422",
      call("POST", "/attendance/punch-out", {"emp_code": code_nolog, "punched_at": ist_ms(day(3), 18, 0) // 1000})[0], 422)
check("valid punch-out after those errors -> 200",
      call("POST", "/attendance/punch-out", {"emp_code": code_nolog, "punched_at": ist_ms(day(3), 18, 30)})[0], 200)

print("--- concurrent punch-outs ---")
code_race3 = emp_code(7)
call("POST", "/employees", new_employee(code_race3))
call("POST", "/attendance/punch-in", {"emp_code": code_race3, "punched_at": ist_ms(day(3), 9, 0)})
with ThreadPoolExecutor(max_workers=10) as pool:
    statuses = sorted(pool.map(lambda _: call("POST", "/attendance/punch-out",
                                              {"emp_code": code_race3, "punched_at": ist_ms(day(3), 18, 0)})[0],
                               range(10)))
check("10 parallel punch-outs: one 200, nine 409", statuses, [200] + [409] * 9)

# --------------------------------------------------------------------------- #
print("--- PATCH /attendance/{emp_code}/{date} ---")
REG = emp_code(8)
call("POST", "/employees", new_employee(REG))
D5 = day(5).isoformat()
call("POST", "/attendance/punch-in", {"emp_code": REG, "punched_at": ist_ms(day(5), 10, 5)})
status, rec = call("POST", "/attendance/punch-out", {"emp_code": REG, "punched_at": ist_ms(day(5), 18, 40)})
check("setup: late 35, work 8.58, no overtime",
      [rec.get(k) for k in ("late_minutes", "work_hours", "overtime_minutes")], [35, 8.58, 0])


def regularize(payload, code=REG, d=D5):
    body = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    body.update(payload)
    return call("PATCH", f"/attendance/{code}/{d}", body)


status, rec = regularize({"punch_in": ist_ms(day(5), 9, 28)})
check("fix punch_in -> 200", status, 200)
check("derived recomputed: late 0, work 9.2", [rec.get("late_minutes"), rec.get("work_hours")], [0, 9.2])
hist = rec.get("history", [])
check("one history entry appended", len(hist), 1)
entry = hist[0] if hist else {}
check("entry has at/by/reason/changes", sorted(entry), ["at", "by", "changes", "reason"])
check("entry.by / reason", (entry.get("by"), entry.get("reason")), ("hr.admin", "biometric glitch"))
check("entry.at is epoch ms", isinstance(entry.get("at"), int) and entry["at"] > 10**11, True)
check("only changed fields recorded", sorted(entry.get("changes", {})), ["late_minutes", "punch_in", "work_hours"])
check("punch_in from/to are epoch ms", entry.get("changes", {}).get("punch_in"),
      {"from": ist_ms(day(5), 10, 5), "to": ist_ms(day(5), 9, 28)})
check("late_minutes from/to", entry.get("changes", {}).get("late_minutes"), {"from": 35, "to": 0})

check("same value again (changes nothing) -> 422", regularize({"punch_in": ist_ms(day(5), 9, 28)})[0], 422)
check("only reason + regularized_by -> 422", regularize({})[0], 422)

status, rec = regularize({"punch_out": ist_ms(day(5), 19, 15), "late_minutes": 999, "work_hours": 1})
check("fix punch_out: overtime 45, work 9.78 (client-sent derived values ignored)",
      [status, rec.get("overtime_minutes"), rec.get("work_hours"), rec.get("late_minutes")], [200, 45, 9.78, 0])
check("history appended, first entry kept", [len(rec.get("history", [])), rec["history"][0]["reason"]],
      [2, "biometric glitch"])

check("LEAVE with a punch time -> 422", regularize({"status": "LEAVE", "punch_in": ist_ms(day(5), 9, 0)})[0], 422)
status, rec = regularize({"status": "LEAVE", "reason": "approved leave"})
check("set LEAVE -> punches cleared, derived reset",
      [status] + [rec.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes",
                                       "overtime_minutes", "half_day")],
      [200, "LEAVE", None, None, None, 0, 0, False])
check("LEAVE entry changes", sorted(rec["history"][-1]["changes"]),
      ["overtime_minutes", "punch_in", "punch_out", "status", "work_hours"])
check("presence status without any punch_in -> 422", regularize({"status": "PRESENT"})[0], 422)
status, rec = regularize({"status": "WFH", "punch_in": ist_ms(day(5), 9, 50)})
check("back to WFH with punch_in: late 20, still open",
      [status, rec.get("status"), rec.get("late_minutes"), rec.get("punch_out"), rec.get("work_hours"),
       len(rec.get("history", []))], [200, "WFH", 20, None, None, 4])

check("punch_in on another date (R1) -> 422", regularize({"punch_in": ist_ms(day(6), 9, 0)})[0], 422)
check("punch_out before punch_in -> 422", regularize({"punch_out": ist_ms(day(5), 9, 0)})[0], 422)
check("punch_out > 24 h after punch_in -> 422", regularize({"punch_out": ist_ms(day(6), 10, 0)})[0], 422)
check("reason too short -> 422", regularize({"punch_out": ist_ms(day(5), 18, 0), "reason": "bad"})[0], 422)
check("missing regularized_by -> 422",
      call("PATCH", f"/attendance/{REG}/{D5}", {"punch_out": ist_ms(day(5), 18, 0), "reason": "valid reason"})[0], 422)
check("punch_out in seconds -> 422", regularize({"punch_out": ist_ms(day(5), 18, 0) // 1000})[0], 422)
check("unknown employee -> 404", regularize({"status": "ABSENT"}, code="EMP_NOPE")[0], 404)
check("no record for that date -> 404", regularize({"status": "ABSENT"}, d=day(9).isoformat())[0], 404)
check("invalid date in path -> 422", regularize({"status": "ABSENT"}, d="2026-13-01")[0], 422)

print("--- concurrent corrections ---")
before = len(call("GET", f"/attendance?emp_code={REG}")[1]["items"][0]["history"])
with ThreadPoolExecutor(max_workers=6) as pool:
    results = list(pool.map(lambda i: regularize({"punch_out": ist_ms(day(5), 18, 30 + i),
                                                  "reason": f"parallel fix {i}"})[0], range(6)))
after = call("GET", f"/attendance?emp_code={REG}")[1]["items"][0]["history"]
check("every response is 200 or 409", all(s in (200, 409) for s in results), True)
check("at least one succeeded", results.count(200) >= 1, True)
check("no lost history: one entry per 200", len(after) - before, results.count(200))

# --------------------------------------------------------------------------- #
print()
print("ALL PASSED" if failures == 0 else f"{failures} FAILED")
sys.exit(1 if failures else 0)
