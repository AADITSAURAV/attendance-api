"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB. A local .env is loaded for convenience, but real
      environment variables always take priority over it.
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
import re
from typing import Annotated, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
# override=False (the default): if a variable is already set in the real
# environment, the value in .env does NOT replace it.
load_dotenv(override=False)

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB = os.environ.get("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set (environment or .env)")

# tz_aware=True -> PyMongo returns timezone-aware UTC datetimes, not naive ones.
# serverSelectionTimeoutMS -> fail after 5 s instead of the default 30 s.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Indexes - created at startup. create_indexes() is idempotent: running it
# again with the same definitions changes nothing.
# --------------------------------------------------------------------------- #
INDEXES = {
    "employees": [
        # Uniqueness of emp_code is enforced by the database (race-safe create).
        IndexModel([("emp_code", ASCENDING)], name="uniq_emp_code", unique=True),
        # Department filter + joined_on (headcount, trend).
        IndexModel([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on"),
        # joined_on alone (department summary without a department filter).
        IndexModel([("joined_on", ASCENDING)], name="joined_on"),
    ],
    "attendance_logs": [
        # One record per employee per day; makes concurrent punch-ins race-safe.
        IndexModel([("emp_code", ASCENDING), ("date", ASCENDING)], name="uniq_emp_code_date", unique=True),
        # Punch-out: "latest record of this employee with punch_in <= t".
        IndexModel([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_code_punch_in"),
        # Listing sorted by date desc, emp_code asc; month ranges in analytics.
        IndexModel([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_code"),
        # Listing filtered by status.
        IndexModel([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp_code"),
    ],
}


def create_indexes() -> None:
    for collection_name, models in INDEXES.items():
        db[collection_name].create_indexes(models)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()  # runs once, before the first request is served
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}   # R6: these count as present
GRACE_SECONDS = 10 * 60                              # R2: 10 minute grace period
MIN_OVERTIME_MINUTES = 30                            # R3: overtime counts from 30 min
MAX_SHIFT = timedelta(hours=24)                      # punch-out at most 24 h after punch-in
HALF_DAY_HOURS = 4.5                                 # R5: below this is a half day
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Helpers - time conversion and rounding
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds (API) -> aware UTC datetime (MongoDB)."""
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Aware datetime (MongoDB) -> epoch milliseconds (API). None stays None."""
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat a naive value as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)  # integer maths, no float error


def truncate_to_second(dt: datetime) -> datetime:
    """R1: 09:40:00.900 counts as 09:40:00."""
    return dt.replace(microsecond=0)


def round_half_up(value, places: int) -> float:
    """R4/R8: half-up rounding (2.345 -> 2.35), unlike Python's round()."""
    quantum = Decimal(1).scaleb(-places)  # places=2 -> Decimal("0.01")
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Helpers - shifts and business rules R1 - R5
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means the shift crosses midnight."""
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: the IST calendar date of punch_in; for an overnight shift a punch-in
    earlier than shift_end belongs to the shift that started the day before."""
    local = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_start_at(att_date: date, shift_start: str) -> datetime:
    """The instant the shift starts: shift_start (IST) on the attendance date."""
    return datetime.combine(att_date, parse_hhmm(shift_start), tzinfo=IST)


def shift_end_at(att_date: date, shift_start: str, shift_end: str) -> datetime:
    """The instant the shift ends; an overnight shift ends on the next day."""
    end_day = att_date + timedelta(days=1) if is_overnight(shift_start, shift_end) else att_date
    return datetime.combine(end_day, parse_hhmm(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, att_date: date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole
    minutes (floor) since shift start. 09:40:00 -> 0, 09:40:01 -> 10."""
    seconds = int((punch_in - shift_start_at(att_date, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, att_date: date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes (floor) from shift end to punch-out, only if >= 30."""
    seconds = int((punch_out - shift_end_at(att_date, shift_start, shift_end)).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) seconds / 3600, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                   att_date: date, emp: dict) -> dict:
    """The four computed fields for a record, following the table in DATA_MODEL.md."""
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
        "half_day": hours < HALF_DAY_HOURS,  # R5: compared on the ROUNDED hours
    }


# --------------------------------------------------------------------------- #
# Validated types and request models
# --------------------------------------------------------------------------- #
def _check_date(value: str) -> str:
    """YYYY-MM-DD and a real calendar date (2026-02-30 is rejected)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    date.fromisoformat(value)  # raises ValueError for impossible dates
    return value


# StrictInt: rejects floats (1.7e12), strings ("1783312500000") and booleans.
# The range rejects values that look like seconds (< 100000000000).
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
DateStr = Annotated[str, AfterValidator(_check_date)]
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Page = Annotated[int, Query(ge=1)]                    # R10: page starts at 1
PageSize = Annotated[int, Query(ge=1, le=100)]        # R10: max 100
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
Month = Annotated[str, Query(pattern=MONTH_PATTERN)]  # YYYY-MM, required
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
    punched_at: Optional[EpochMillis] = None   # omitted -> now
    status: PresenceStatus = "PRESENT"         # ABSENT / LEAVE are rejected (422)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None   # omitted -> now


class RegularizeRequest(BaseModel):
    """Only these fields can be changed. Anything else in the body (for example
    late_minutes or work_hours) is ignored - derived fields are always recomputed."""
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


# --------------------------------------------------------------------------- #
# Response serializers - build the API shape explicitly (never leak _id)
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    """Current time, cut to whole milliseconds (what BSON can store)."""
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
    """history 'from'/'to' values: datetimes become epoch ms, others unchanged."""
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
    """An attendance record in API shape. Legacy documents may lack history,
    half_day, late_minutes or overtime_minutes: treat them as [], False, 0, 0."""
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
    """A 422 with FastAPI's standard body, for checks that span two fields."""
    return RequestValidationError([{"type": "value_error", "loc": [location, field], "msg": message, "input": value}])


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    # 200 only if MongoDB actually answers a ping; otherwise 503.
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
        # No check-then-insert: the unique index decides, so two concurrent
        # creates with the same emp_code give exactly one 201 and one 409.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    total = db.employees.count_documents(query)  # R10: total reflects the filter
    cursor = (
        db.employees.find(query)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)            # page 1 skips nothing
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    instant = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    punched = truncate_to_second(instant)                                   # R1
    att_date = attendance_date(punched, emp["shift_start"], emp["shift_end"])  # R1

    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        **derived_fields(body.status, punched, None, att_date, emp),  # R2; others start empty
        "history": [],
    }
    try:
        # Race-safe: the unique (emp_code, date) index lets exactly one insert win.
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
    punched = truncate_to_second(instant)  # R1

    # The employee's most recent record whose punch_in is at or before this
    # punch-out (index emp_code_punch_in). This also finds yesterday's record
    # when an overnight shift is closed after midnight.
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
    derived = derived_fields(record["status"], record["punch_in"], punched, att_date, emp)  # R3-R5

    # Race-safe: the filter only matches while punch_out is still null, so of
    # two simultaneous punch-outs exactly one gets the document back.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return record_out(updated)


# Fields compared for history[].changes, with the value a missing field means.
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

    # 1. Final values of the editable fields (omitted -> unchanged).
    new_status = body.status or record["status"]
    if new_status not in PRESENCE_STATUSES:
        # ABSENT / LEAVE clear both punch times; supplying one is a contradiction.
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

    # 2. Recompute every derived field from the final values (R2-R5).
    new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                  **derived_fields(new_status, new_in, new_out, att_date, emp)}

    # 3. Only fields whose value actually changes go into the audit entry.
    changes = {}
    for field, missing in TRACKED_FIELDS.items():
        before = record.get(field, missing)
        if before != new_values[field]:
            changes[field] = {"from": before, "to": new_values[field]}
    if not changes:
        raise validation_error("body", "body", "the request changes nothing", None)

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # 4. Optimistic concurrency: update only if the record is still exactly as we
    #    read it (same editable fields, same number of history entries). If a
    #    concurrent correction got there first, this matches nothing -> 409, so
    #    no history entry is ever silently lost. $push appends, never overwrites.
    guard = {"_id": record["_id"], "status": record["status"],
             "punch_in": record.get("punch_in"), "punch_out": record.get("punch_out")}
    if "history" in record:
        guard["history"] = {"$size": len(record["history"] or [])}
    else:
        guard["history"] = {"$exists": False}   # legacy record without history
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
    """Shared by GET /attendance and its explain, so both run the same query."""
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from   # YYYY-MM-DD strings sort like dates
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
    # Sorting, skipping and limiting happen in MongoDB on an index -
    # the collection is never loaded into memory.
    cursor = (
        db.attendance_logs.find(query)
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Analytics - shared aggregation building blocks
# --------------------------------------------------------------------------- #
PRESENCE_LIST = sorted(PRESENCE_STATUSES)


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31'). Dates are compared as strings."""
    year, mon = map(int, month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_working_days(first: date, last: date) -> int:
    """R7: Monday-Friday between two dates inclusive (calendar arithmetic only)."""
    days = (last - first).days + 1
    return sum(1 for i in range(max(days, 0)) if (first + timedelta(days=i)).weekday() < 5)


def _f(var: str, field: str) -> str:
    """Field path for a top-level field ('') or a field of a $map variable ('l')."""
    return f"$${var}.{field}" if var else f"${field}"


def is_weekday_expr(var: str = "") -> dict:
    """Mon-Fri check on the YYYY-MM-DD 'date' string (isoDayOfWeek: Mon=1 .. Sun=7)."""
    day = {"$dateFromString": {"dateString": _f(var, "date"), "format": "%Y-%m-%d"}}
    return {"$lte": [{"$isoDayOfWeek": day}, 5]}


def presence_value_expr(var: str = "", weekdays_only: bool = True) -> dict:
    """R5/R6/R7: 1 for a present-status record, 0.5 for a half day, else 0.
    Missing half_day means false."""
    value = {"$cond": [{"$eq": [{"$ifNull": [_f(var, "half_day"), False]}, True]}, 0.5, 1]}
    present = {"$in": [_f(var, "status"), PRESENCE_LIST]}
    condition = {"$and": [present, is_weekday_expr(var)]} if weekdays_only else present
    return {"$cond": [condition, value, 0]}


def late_flag_expr(var: str = "") -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [_f(var, "late_minutes"), 0]}, 0]}, 1, 0]}


def sum_over_logs(expr: dict) -> dict:
    """Sum an expression over the 'logs' array produced by a $lookup."""
    return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}


def half_up_decimal_expr(expr, places: int = 4) -> dict:
    """R8 half-up rounding inside MongoDB, done in Decimal128 so there is no
    binary-float error ($round would use banker's rounding)."""
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, Decimal128("0.5")]}}, factor]}


def to_float(value):
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    return value


# --------------------------------------------------------------------------- #
# GET /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        # Index uniq_emp_code_date: equality on emp_code + range on date.
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": presence_value_expr()},                      # Mon-Fri only
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": late_flag_expr()},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},       # weekends too
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    first, last = month_bounds(month)
    # R7: working days start at joined_on for a mid-month joiner.
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


# --------------------------------------------------------------------------- #
# GET /analytics/departments/summary
# --------------------------------------------------------------------------- #
def build_department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last}}          # R9: joined by the end of the month
    if department is not None:
        match["department"] = department
    present_with_hours = {"$and": [
        {"$in": ["$$l.status", PRESENCE_LIST]},
        {"$ne": [{"$ifNull": ["$$l.work_hours", None]}, None]},
    ]}
    return [
        # Start from EMPLOYEES so people with zero logs are still counted.
        {"$match": match},
        # Each employee's logs for the month (index uniq_emp_code_date).
        # Logs whose employee does not exist are never reached.
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
        # Per-employee totals (an employee without logs gets zeros).
        {"$project": {
            "department": 1,
            "present_days": sum_over_logs(presence_value_expr("l")),
            # work_hours summed as integer hundredths so the sum is exact.
            "work_cents": sum_over_logs({"$cond": [present_with_hours,
                                                   {"$round": [{"$multiply": ["$$l.work_hours", 100]}, 0]}, 0]}),
            "work_records": sum_over_logs({"$cond": [present_with_hours, 1, 0]}),
            "late_count": sum_over_logs(late_flag_expr("l")),
            "total_late_minutes": sum_over_logs({"$ifNull": ["$$l.late_minutes", 0]}),
            "leave_count": sum_over_logs({"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}),
            "on_duty_count": sum_over_logs({"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}),
        }},
        # Per-department totals. avg_work_hours = total hours / number of
        # records (an average over records, not an average of averages).
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


# --------------------------------------------------------------------------- #
# GET /analytics/leaderboard/late
# --------------------------------------------------------------------------- #
def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline = [
        # Index date_emp_code: range on date. Only late records matter.
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        # name/department come from employees; unknown employees drop out here.
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department
    pipeline += [
        # Standard competition ranking: ties share a rank, next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        # limit applies AFTER ranking, so everyone tied at the cutoff is returned.
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


# --------------------------------------------------------------------------- #
# GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
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
        # Employees of the department who joined by the last day (index department_joined_on).
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
        # One "event" per employee joining (+1 headcount from that day on) and
        # one per log (presence and lateness on that day).
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            [{"day": {"$max": ["$joined_on", from_]}, "hc": {"$literal": 1},
              "present": {"$literal": 0}, "late": {"$literal": 0}}],
            {"$map": {"input": "$logs", "as": "l", "in": {
                "day": "$$l.date", "hc": {"$literal": 0},
                "present": presence_value_expr("l", weekdays_only=False),  # any day counts here
                "late": late_flag_expr("l"),
            }}},
        ]}}},
        {"$unwind": "$events"},
        {"$replaceWith": "$events"},
        # Guarantees at least one document even if nobody has joined yet.
        {"$unionWith": {"pipeline": [{"$documents": [{"day": from_, "hc": 0, "present": 0, "late": 0}]}]}},
        {"$group": {"_id": "$day", "hc": {"$sum": "$hc"},
                    "present": {"$sum": "$present"}, "late": {"$sum": "$late"}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # Gap filling: exactly one document per calendar day from `from` to `to`.
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start, end_exclusive]}}},
        # R9: headcount on a day = everyone who joined on or before it (running total).
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "headcount": {"$sum": "$hc", "window": {"documents": ["unbounded", "current"]}},
        }}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
            "present_count": {"$ifNull": ["$present", 0]},
            "late_count": {"$ifNull": ["$late", 0]},
        }},
        # Rate only on working days with headcount > 0 (a working day with no
        # records is 0.0); computed in Decimal128 and rounded half-up to 4 places.
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_decimal_expr({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}),
            None,
        ]}}},
        # Mean of the non-null rates in this row and up to 6 rows before it,
        # inside the requested range only ($avg ignores nulls; all null -> null).
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


# --------------------------------------------------------------------------- #
# GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
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
    """Runs explain (executionStats) on the same query the real endpoint runs -
    both are built by the same build_* function."""
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
    # json_util turns BSON-only types (Timestamp, Int64, ...) into plain JSON.
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}


