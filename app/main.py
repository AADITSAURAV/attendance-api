"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB. A local .env is loaded for convenience, but real
      environment variables always take priority over it.
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
import re
from typing import Annotated, Literal, Optional

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
# TODO - the rest of the contract (see openapi.yaml):
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
