"""Employee Attendance & Analytics API.

Run with: uvicorn app.main:app --port 8000
Needs MONGO_URI and MONGO_DB (env vars, or a local .env).
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --- config ---
load_dotenv(override=False)  # real environment variables win over .env

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB = os.environ.get("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set (environment or .env)")

# tz_aware so datetimes come back as UTC-aware instead of naive
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --- indexes (created at startup; create_indexes is a no-op if they exist) ---
INDEXES = {
    "employees": [
        IndexModel([("emp_code", ASCENDING)], name="uniq_emp_code", unique=True),
        IndexModel([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on"),
        IndexModel([("joined_on", ASCENDING)], name="joined_on"),
    ],
    "attendance_logs": [
        # one record per employee per day - this is what makes punch-in race-safe
        IndexModel([("emp_code", ASCENDING), ("date", ASCENDING)], name="uniq_emp_code_date", unique=True),
        # punch-out looks up the latest punch_in for an employee
        IndexModel([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_code_punch_in"),
        IndexModel([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_code"),
        IndexModel([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp_code"),
    ],
}


def create_indexes() -> None:
    for collection_name, models in INDEXES.items():
        db[collection_name].create_indexes(models)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --- business rule constants ---
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
MAX_SHIFT = timedelta(hours=24)
HALF_DAY_HOURS = 4.5
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# --- time conversion / rounding helpers ---
def ms_to_dt(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)  # integer division, avoids float error


def truncate_to_second(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def round_half_up(value, places: int) -> float:
    # Python's round() is not half-up on floats, so go through Decimal
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


# --- shift / attendance rules ---
def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    # Dates are IST. On an overnight shift, a punch-in before shift_end
    # (e.g. 01:00 on a 22:00-06:00 shift) belongs to the previous day.
    local = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_start_at(att_date: date, shift_start: str) -> datetime:
    return datetime.combine(att_date, parse_hhmm(shift_start), tzinfo=IST)


def shift_end_at(att_date: date, shift_start: str, shift_end: str) -> datetime:
    end_day = att_date + timedelta(days=1) if is_overnight(shift_start, shift_end) else att_date
    return datetime.combine(end_day, parse_hhmm(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, att_date: date, shift_start: str) -> int:
    # compare in seconds: 09:40:00 is not late, 09:40:01 is (10 minutes)
    seconds = int((punch_in - shift_start_at(att_date, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, att_date: date, shift_start: str, shift_end: str) -> int:
    seconds = int((punch_out - shift_end_at(att_date, shift_start, shift_end)).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                   att_date: date, emp: dict) -> dict:
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, att_date, emp["shift_start"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, att_date, emp["shift_start"], emp["shift_end"]),
        "half_day": hours < HALF_DAY_HOURS,  # uses the rounded hours
    }


# --- request models / validated types ---
def _check_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    date.fromisoformat(value)  # rejects things like 2026-02-30
    return value


# StrictInt so "123..." strings, floats and booleans are rejected; the lower
# bound rejects timestamps sent in seconds instead of milliseconds.
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
DateStr = Annotated[str, AfterValidator(_check_date)]
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
Month = Annotated[str, Query(pattern=MONTH_PATTERN)]
Limit = Annotated[int, Query(ge=1, le=50)]
MAX_TREND_DAYS = 92


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None  # defaults to now
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    # derived fields (late_minutes etc.) are not accepted here - always recomputed
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


# --- response serializers ---
def now_utc() -> datetime:
    # BSON dates only keep milliseconds
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _api_value(value):
    return dt_to_ms(value) if isinstance(value, datetime) else value


def history_out(entry: dict) -> dict:
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {"from": _api_value(change.get("from")), "to": _api_value(change.get("to"))}
            for field, change in (entry.get("changes") or {}).items()
        },
    }


def record_out(doc: dict) -> dict:
    # older seeded records can be missing history/half_day/minutes
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def validation_error(field: str, location: str, message: str, value) -> RequestValidationError:
    # 422 in FastAPI's normal format, for checks pydantic can't do on its own
    return RequestValidationError([{"type": "value_error", "loc": [location, field], "msg": message, "input": value}])


# --- endpoints ---
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        # no find-then-insert: the unique index decides between concurrent requests
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    instant = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    punched = truncate_to_second(instant)
    att_date = attendance_date(punched, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        **derived_fields(body.status, punched, None, att_date, emp),
        "history": [],
    }
    try:
        # unique (emp_code, date) index: only one concurrent punch-in can win
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    instant = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    punched = truncate_to_second(instant)

    # latest record with punch_in <= punch-out time (also finds yesterday's
    # record when a night shift punches out after midnight)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")
    if punched <= record["punch_in"]:
        raise validation_error("punched_at", "body", "punch-out must be after punch-in", body.punched_at)
    if punched - record["punch_in"] > MAX_SHIFT:
        raise validation_error("punched_at", "body", "punch-out is more than 24 hours after punch-in",
                               body.punched_at)

    att_date = date.fromisoformat(record["date"])
    derived = derived_fields(record["status"], record["punch_in"], punched, att_date, emp)

    # only matches while punch_out is still null, so if two punch-outs race
    # only one of them updates the record
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return record_out(updated)


# fields that go into history[].changes, with their default if missing
TRACKED_FIELDS = {
    "status": None, "punch_in": None, "punch_out": None, "work_hours": None,
    "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="no attendance record for this date")
    att_date = datetime.strptime(date, "%Y-%m-%d").date()

    new_status = body.status or record["status"]
    if new_status not in PRESENCE_STATUSES:
        # ABSENT/LEAVE have no punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise validation_error("status", "body", f"{new_status} cannot have punch times", new_status)
        new_in = new_out = None
    else:
        new_in = truncate_to_second(ms_to_dt(body.punch_in)) if body.punch_in is not None else record.get("punch_in")
        new_out = truncate_to_second(ms_to_dt(body.punch_out)) if body.punch_out is not None else record.get("punch_out")
        if new_in is None:
            raise validation_error("punch_in", "body", "a presence status requires punch_in", None)
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != att_date:
            raise validation_error("punch_in", "body", "punch_in must stay on the record's attendance date (R1)",
                                   body.punch_in)
        if new_out is not None and not (new_in < new_out <= new_in + MAX_SHIFT):
            raise validation_error("punch_out", "body", "punch_out must be after punch_in and within 24 hours",
                                   body.punch_out)

    new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                  **derived_fields(new_status, new_in, new_out, att_date, emp)}

    changes = {}
    for field, missing in TRACKED_FIELDS.items():
        before = record.get(field, missing)
        if before != new_values[field]:
            changes[field] = {"from": before, "to": new_values[field]}
    if not changes:
        raise validation_error("body", "body", "the request changes nothing", None)

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: only update if the record still looks like what we
    # read (same punches/status and same history length). If another correction
    # got in first this matches nothing and we return 409 instead of losing it.
    guard = {"_id": record["_id"], "status": record["status"],
             "punch_in": record.get("punch_in"), "punch_out": record.get("punch_out")}
    if "history" in record:
        guard["history"] = {"$size": len(record["history"] or [])}
    else:
        guard["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new_values, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record was changed by another request; retry")
    return record_out(updated)


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def build_attendance_filter(emp_code: Optional[str], date_from: Optional[str],
                            date_to: Optional[str], status: Optional[str]) -> dict:
    # also used by /admin/explain so both run the same query
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from  # YYYY-MM-DD strings compare correctly
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if date_from and date_to and date_from > date_to:
        raise validation_error("date_from", "query", "date_from must not be after date_to", date_from)
    query = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --- analytics helpers ---
PRESENCE_LIST = sorted(PRESENCE_STATUSES)


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = map(int, month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_working_days(first: date, last: date) -> int:
    days = (last - first).days + 1
    return sum(1 for i in range(max(days, 0)) if (first + timedelta(days=i)).weekday() < 5)


def _f(var: str, field: str) -> str:
    # "$field" for a top-level field, "$$l.field" inside a $map over logs
    return f"$${var}.{field}" if var else f"${field}"


def is_weekday_expr(var: str = "") -> dict:
    day = {"$dateFromString": {"dateString": _f(var, "date"), "format": "%Y-%m-%d"}}
    return {"$lte": [{"$isoDayOfWeek": day}, 5]}


def presence_value_expr(var: str = "", weekdays_only: bool = True) -> dict:
    # 1 for a present day, 0.5 for a half day, 0 otherwise
    value = {"$cond": [{"$eq": [{"$ifNull": [_f(var, "half_day"), False]}, True]}, 0.5, 1]}
    present = {"$in": [_f(var, "status"), PRESENCE_LIST]}
    condition = {"$and": [present, is_weekday_expr(var)]} if weekdays_only else present
    return {"$cond": [condition, value, 0]}


def late_flag_expr(var: str = "") -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [_f(var, "late_minutes"), 0]}, 0]}, 1, 0]}


def sum_over_logs(expr: dict) -> dict:
    return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}


def half_up_decimal_expr(expr, places: int = 4) -> dict:
    # $round is banker's rounding, so do floor(x * 10^n + 0.5) in Decimal128
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, Decimal128("0.5")]}}, factor]}


def to_float(value):
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    return value


# --- GET /analytics/employees/{emp_code}/monthly ---
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": presence_value_expr()},  # Mon-Fri only
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": late_flag_expr()},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},  # weekends count here
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    first, last = month_bounds(month)
    # mid-month joiners: working days start from joined_on
    start = max(date.fromisoformat(first), date.fromisoformat(emp["joined_on"]))
    working_days = count_working_days(start, date.fromisoformat(last))

    rows = list(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, month)))
    stats = rows[0] if rows else {}
    present_days = round_half_up(stats.get("present_days", 0), 2)
    attendance_pct = None
    if working_days:
        attendance_pct = round_half_up(Decimal(str(present_days)) * 100 / working_days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


# --- GET /analytics/departments/summary ---
def build_department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    present_with_hours = {"$and": [
        {"$in": ["$$l.status", PRESENCE_LIST]},
        {"$ne": [{"$ifNull": ["$$l.work_hours", None]}, None]},
    ]}
    return [
        # start from employees (not logs) so people with no logs still count
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1,
                              "work_hours": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {
            "department": 1,
            "present_days": sum_over_logs(presence_value_expr("l")),
            # sum hours as integer hundredths to avoid float drift
            "work_cents": sum_over_logs({"$cond": [present_with_hours,
                                                   {"$round": [{"$multiply": ["$$l.work_hours", 100]}, 0]}, 0]}),
            "work_records": sum_over_logs({"$cond": [present_with_hours, 1, 0]}),
            "late_count": sum_over_logs(late_flag_expr("l")),
            "total_late_minutes": sum_over_logs({"$ifNull": ["$$l.late_minutes", 0]}),
            "leave_count": sum_over_logs({"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}),
            "on_duty_count": sum_over_logs({"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}),
        }},
        # avg_work_hours is total hours / number of records, not an average of averages
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "work_cents": {"$sum": "$work_cents"},
            "work_records": {"$sum": "$work_records"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    items = []
    for row in db.employees.aggregate(build_department_summary_pipeline(month, department)):
        avg = None
        if row["work_records"]:
            avg = round_half_up(Decimal(int(row["work_cents"])) / row["work_records"] / 100, 2)
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": round_half_up(row["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


# --- GET /analytics/leaderboard/late ---
def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        # $unwind drops logs whose employee doesn't exist
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank gives 1, 2, 2, 4 for ties (not the array position)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        # limit is applied after ranking so ties at the cutoff are all kept
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: Optional[str] = None):
    items = list(db.attendance_logs.aggregate(build_late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


# --- GET /analytics/departments/{department}/trend ---
def validate_trend_range(from_: str, to: str) -> None:
    if to < from_:
        raise validation_error("to", "query", "to must not be before from", to)
    days = (date.fromisoformat(to) - date.fromisoformat(from_)).days + 1
    if days > MAX_TREND_DAYS:
        raise validation_error("to", "query", f"range must not exceed {MAX_TREND_DAYS} days", to)


def build_department_trend_pipeline(department: str, from_: str, to: str) -> list:
    start = datetime.strptime(from_, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_exclusive = datetime.strptime(to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    return [
        {"$match": {"department": department, "joined_on": {"$lte": to}}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": from_, "$lte": to}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        # turn each employee into a "joined" event (+1 headcount) plus one event per log
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            [{"day": {"$max": ["$joined_on", from_]}, "hc": {"$literal": 1},
              "present": {"$literal": 0}, "late": {"$literal": 0}}],
            {"$map": {"input": "$logs", "as": "l", "in": {
                "day": "$$l.date", "hc": {"$literal": 0},
                "present": presence_value_expr("l", weekdays_only=False),
                "late": late_flag_expr("l"),
            }}},
        ]}}},
        {"$unwind": "$events"},
        {"$replaceWith": "$events"},
        # make sure there is at least one document for $densify to work with
        {"$unionWith": {"pipeline": [{"$documents": [{"day": from_, "hc": 0, "present": 0, "late": 0}]}]}},
        {"$group": {"_id": "$day", "hc": {"$sum": "$hc"},
                    "present": {"$sum": "$present"}, "late": {"$sum": "$late"}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # fill in missing days so there is one row per calendar day
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start, end_exclusive]}}},
        # running total of joins = headcount on that day
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "headcount": {"$sum": "$hc", "window": {"documents": ["unbounded", "current"]}},
        }}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
            "present_count": {"$ifNull": ["$present", 0]},
            "late_count": {"$ifNull": ["$late", 0]},
        }},
        # rate only on working days with headcount > 0, otherwise null
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_decimal_expr({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}),
            None,
        ]}}},
        # 7-day window inside the requested range; $avg skips null rates
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}},
        }}},
        {"$addFields": {"moving_avg_7d": {"$cond": [
            {"$eq": [{"$ifNull": ["$moving_avg_7d", None]}, None]}, None,
            half_up_decimal_expr("$moving_avg_7d"),
        ]}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     from_: Annotated[DateStr, Query(alias="from")],
                     to: Annotated[DateStr, Query()]):
    validate_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(status_code=404, detail="unknown department")
    items = []
    for row in db.employees.aggregate(build_department_trend_pipeline(department, from_, to)):
        row["attendance_rate"] = to_float(row["attendance_rate"])
        row["moving_avg_7d"] = to_float(row["moving_avg_7d"])
        items.append(row)
    return {"department": department, "items": items}


# --- GET /admin/explain/{endpoint} ---
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary",
                        "late_leaderboard", "department_trend"]


def _require(name: str, value):
    if value is None:
        raise validation_error(name, "query", f"{name} is required for this endpoint", None)
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    # builds the query with the same build_* function the real endpoint uses
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise validation_error("date_from", "query", "date_from must not be after date_to", date_from)
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": build_attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            collection = "attendance_logs"
            pipeline = build_employee_monthly_pipeline(_require("emp_code", emp_code), _require("month", month))
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline = build_department_summary_pipeline(_require("month", month), department)
        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            pipeline = build_late_leaderboard_pipeline(_require("month", month), limit, department)
        else:  # department_trend
            collection = "employees"
            dept, start, end = _require("department", department), _require("from", from_), _require("to", to)
            validate_trend_range(start, end)
            pipeline = build_department_trend_pipeline(dept, start, end)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command("explain", command, verbosity="executionStats")
    # json_util converts BSON types (Timestamp etc.) so the response is plain JSON
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}
