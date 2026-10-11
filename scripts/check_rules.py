"""Quick self-check of the business-rule helpers (R1 - R5) in app/main.py.

Run from the project root:   python scripts/check_rules.py
No server needed. It does not touch the database.
"""
import json
import pathlib
import sys
from datetime import date, datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import (  # noqa: E402
    IST, attendance_date, compute_late_minutes, compute_overtime, compute_work_hours,
    derived_fields, dt_to_ms, ms_to_dt, round_half_up, truncate_to_second,
)

failures = 0


def check(label, got, expected):
    global failures
    ok = got == expected
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got!r}, expected {expected!r}")


def ist(y, mo, d, h, mi, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=IST)


D = date(2026, 7, 6)

print("--- R2 late (shift 09:30) ---")
check("09:28:00", compute_late_minutes(ist(2026, 7, 6, 9, 28), D, "09:30"), 0)
check("09:40:00", compute_late_minutes(ist(2026, 7, 6, 9, 40), D, "09:30"), 0)
check("09:40:01", compute_late_minutes(ist(2026, 7, 6, 9, 40, 1), D, "09:30"), 10)
check("10:15:59", compute_late_minutes(ist(2026, 7, 6, 10, 15, 59), D, "09:30"), 45)
check("09:40:00.900 (R1 truncation)",
      compute_late_minutes(truncate_to_second(ist(2026, 7, 6, 9, 40, 0, 900000)), D, "09:30"), 0)

print("--- R3 overtime (shift end 18:30) ---")
check("18:35", compute_overtime(ist(2026, 7, 6, 18, 35), D, "09:30", "18:30"), 0)
check("18:59:59", compute_overtime(ist(2026, 7, 6, 18, 59, 59), D, "09:30", "18:30"), 0)
check("19:00:00", compute_overtime(ist(2026, 7, 6, 19, 0), D, "09:30", "18:30"), 30)
check("19:10", compute_overtime(ist(2026, 7, 6, 19, 10), D, "09:30", "18:30"), 40)
check("17:00 (left early)", compute_overtime(ist(2026, 7, 6, 17, 0), D, "09:30", "18:30"), 0)

print("--- R4 work hours, R5 half day ---")
check("9h05m", compute_work_hours(ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10)), 9.08)
check("18 s = 0.005 h -> 0.01 (half-up)",
      compute_work_hours(ist(2026, 7, 6, 9, 0), ist(2026, 7, 6, 9, 0, 18)), 0.01)
wh = compute_work_hours(ist(2026, 7, 6, 9, 0), ist(2026, 7, 6, 13, 29, 42))
check("4h29m42s -> 4.50", wh, 4.5)
check("4.50 is NOT a half day", wh < 4.5, False)
wh = compute_work_hours(ist(2026, 7, 6, 9, 0), ist(2026, 7, 6, 13, 29, 41))
check("4h29m41s -> 4.49", wh, 4.49)
check("4.49 IS a half day", wh < 4.5, True)
check("round_half_up(2.675, 2)", round_half_up(2.675, 2), 2.68)

print("--- R1 attendance date ---")
check("day shift, 00:30 IST is that IST day",
      attendance_date(ist(2026, 7, 7, 0, 30), "09:30", "18:30"), date(2026, 7, 7))
check("night 22-06, 21:55 -> same day",
      attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00"), date(2026, 7, 6))
check("night 22-06, 01:00 next day -> previous day",
      attendance_date(ist(2026, 7, 7, 1, 0), "22:00", "06:00"), date(2026, 7, 6))
check("night 22-06, exactly 06:00 -> that day",
      attendance_date(ist(2026, 7, 7, 6, 0), "22:00", "06:00"), date(2026, 7, 7))
check("night late 01:00 -> 180 min",
      compute_late_minutes(ist(2026, 7, 7, 1, 0), D, "22:00"), 180)
check("night overtime 06:40 next day -> 40",
      compute_overtime(ist(2026, 7, 7, 6, 40), D, "22:00", "06:00"), 40)

print("--- epoch milliseconds round trip ---")
check("ms -> dt -> ms", dt_to_ms(ms_to_dt(1783312500000)), 1783312500000)

print("--- the kit's sample documents: recompute and compare with stored values ---")
emps = {e["emp_code"]: e for e in json.loads((ROOT / "sample_data/employees.json").read_text())}
for rec in json.loads((ROOT / "sample_data/attendance_logs.json").read_text()):
    emp = emps[rec["emp_code"]]
    pin = rec["punch_in"] and datetime.fromisoformat(rec["punch_in"]["$date"].replace("Z", "+00:00"))
    pout = rec["punch_out"] and datetime.fromisoformat(rec["punch_out"]["$date"].replace("Z", "+00:00"))
    if pin:
        check(f"{rec['emp_code']} {rec['date']} date",
              attendance_date(pin, emp["shift_start"], emp["shift_end"]).isoformat(), rec["date"])
    got = derived_fields(rec["status"], pin or None, pout or None, date.fromisoformat(rec["date"]), emp)
    stored = {k: rec.get(k, d) for k, d in
              [("work_hours", None), ("late_minutes", 0), ("overtime_minutes", 0), ("half_day", False)]}
    check(f"{rec['emp_code']} {rec['date']} derived", got, stored)

print()
print("ALL PASSED" if failures == 0 else f"{failures} FAILED")
sys.exit(1 if failures else 0)
