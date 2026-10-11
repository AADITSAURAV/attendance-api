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
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient
from pymongo.errors import PyMongoError

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
# Models  (STARTER - not reviewed yet)
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


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


# ----- STARTER endpoints below - not reviewed yet --------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = page * page_size
    total = db.employees.count_documents({})
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,  # TEMP: starter punch-in is rewritten in a later step
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
