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
print()
print("ALL PASSED" if failures == 0 else f"{failures} FAILED")
sys.exit(1 if failures else 0)
