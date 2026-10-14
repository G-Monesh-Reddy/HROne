"""
Employee Attendance & Analytics API - Production Implementation
Contract: v2.0 (HROne Engineering Assignment)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
import pymongo
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_indexes()
    except Exception as e:
        print(f"Warning: Failed to initialize indexes at startup: {e}")
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def truncate_ms(ms: int) -> int:
    """Truncate epoch milliseconds to whole seconds (R1)."""
    return (ms // 1000) * 1000


def now_ms() -> int:
    """Current server clock in epoch milliseconds, truncated to whole seconds."""
    return truncate_ms(int(datetime.now(timezone.utc).timestamp() * 1000))


def ms_to_utc_dt(ms: int) -> datetime:
    """Convert truncated epoch ms to UTC BSON datetime."""
    t_sec = ms // 1000
    return datetime.fromtimestamp(t_sec, tz=timezone.utc)


def utc_dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert BSON datetime to epoch milliseconds integer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    """Round using standard decimal half-up rounding (R4, R8)."""
    d = Decimal(str(value)) if not isinstance(value, Decimal) else value
    q = Decimal("10") ** -places
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def get_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    is_overnight = shift_end <= shift_start
    punch_time_str = punch_in_ist.strftime("%H:%M")
    if is_overnight and punch_time_str < shift_end:
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, shift_start: str, att_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly more than 10:00 (600s)
    after shift_start. When late, late_minutes = whole minutes (floor) since shift_start.
    """
    sy, sm, sd = map(int, att_date.split("-"))
    sh, smin = map(int, shift_start.split(":"))
    shift_start_dt = datetime(sy, sm, sd, sh, smin, 0, tzinfo=IST)
    delta_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if delta_seconds > 600:
        return int(delta_seconds // 60)
    return 0


def compute_overtime(punch_out_ist: datetime, shift_start: str, shift_end: str, att_date: str) -> int:
    """
    R3: Overtime = whole minutes (floor) from shift_end to punch_out,
    counted only if at least 30; otherwise 0.
    Overnight shifts: shift ends on the next calendar day (R1).
    """
    sy, sm, sd = map(int, att_date.split("-"))
    eh, emin = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    if is_overnight:
        shift_end_dt = datetime(sy, sm, sd, eh, emin, 0, tzinfo=IST) + timedelta(days=1)
    else:
        shift_end_dt = datetime(sy, sm, sd, eh, emin, 0, tzinfo=IST)

    delta_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    ot_minutes = int(delta_seconds // 60)
    return ot_minutes if ot_minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    delta_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    val = Decimal(delta_seconds) / Decimal(3600)
    return round_half_up(val, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: Half day = true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format employee document for API responses according to OpenAPI contract."""
    res = dict(doc)
    res.pop("_id", None)
    if isinstance(res.get("created_at"), datetime):
        res["created_at"] = utc_dt_to_ms(res["created_at"])
    return res


def format_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Format an audit history entry converting BSON dates to epoch ms."""
    formatted_entry = {
        "at": utc_dt_to_ms(entry["at"]) if isinstance(entry.get("at"), datetime) else entry.get("at"),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {}
    }
    raw_changes = entry.get("changes", {})
    for k, v in raw_changes.items():
        if isinstance(v, dict):
            from_val = v.get("from")
            to_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = utc_dt_to_ms(from_val)
                if isinstance(to_val, datetime):
                    to_val = utc_dt_to_ms(to_val)
            formatted_entry["changes"][k] = {
                "from": from_val,
                "to": to_val
            }
        else:
            formatted_entry["changes"][k] = v
    return formatted_entry


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format attendance document according to AttendanceRecord schema."""
    res = dict(doc)
    res.pop("_id", None)
    res["punch_in"] = utc_dt_to_ms(res.get("punch_in"))
    res["punch_out"] = utc_dt_to_ms(res.get("punch_out"))
    res["late_minutes"] = res.get("late_minutes", 0)
    res["overtime_minutes"] = res.get("overtime_minutes", 0)
    res["half_day"] = bool(res.get("half_day", False))
    raw_history = res.get("history") or []
    res["history"] = [format_history_entry(h) for h in raw_history]
    return res


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe. Returns 200 only when MongoDB answers; else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB unavailable"
        )


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforce unique emp_code. Set created_at with server clock."""
    now_epoch_ms = now_ms()
    doc = body.model_dump()
    doc["created_at"] = ms_to_utc_dt(now_epoch_ms)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees. Filter by exact department. Sort by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    """Record a punch-in. Atomic insertion guarded by unique index (emp_code, date)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    p_ms = truncate_ms(body.punched_at) if body.punched_at is not None else now_ms()
    punch_in_utc = ms_to_utc_dt(p_ms)
    punch_in_ist = punch_in_utc.astimezone(IST)

    att_date = get_attendance_date(punch_in_ist, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_ist, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date"
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out. Closes most recent record whose punch_in <= punched_at.
    Handles overnight shifts and concurrent punch-outs atomically.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    p_ms = truncate_ms(body.punched_at) if body.punched_at is not None else now_ms()
    punch_out_utc = ms_to_utc_dt(p_ms)
    punch_out_ist = punch_out_utc.astimezone(IST)

    # Find the most recent record with punch_in for this employee
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", DESCENDING)]
    )

    if not record or record.get("punch_in") is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    # Validation: punched_at <= punch_in or more than 24 hours after it -> 422
    delta_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if delta_seconds <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punched_at must be strictly after punch_in")
    if delta_seconds > 86400:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punched_at cannot be more than 24 hours after punch_in")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record already punched out")

    # Compute derived fields
    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    half_day = compute_half_day(work_hours)
    overtime_minutes = compute_overtime(punch_out_ist, emp["shift_start"], emp["shift_end"], record["date"])

    # Atomic conditional update to guard against simultaneous punch-out races
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records. Paginated, backed by compound index, fast at 100k records."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from cannot be after date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """
    Regularize one day's record. Recompute derived fields, append to audit history.
    Prevents concurrent edits from silently losing history entries.
    """
    # Validate date pattern
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$", date):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    # Rule: setting status ABSENT or LEAVE clears both punch times, so supplying punch_in or punch_out with it is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE"
        )

    target_status = body.status if body.status is not None else record.get("status")

    if target_status in ("ABSENT", "LEAVE"):
        target_punch_in = None
        target_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # A presence status requires a punch_in (supplied or already stored)
        if body.punch_in is not None:
            target_punch_in = ms_to_utc_dt(truncate_ms(body.punch_in))
        else:
            target_punch_in = record.get("punch_in")

        if target_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in"
            )

        if target_punch_in.tzinfo is None:
            target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

        if body.punch_out is not None:
            target_punch_out = ms_to_utc_dt(truncate_ms(body.punch_out))
        else:
            target_punch_out = record.get("punch_out")

        if target_punch_out is not None and target_punch_out.tzinfo is None:
            target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)

        # Rule: punch_in must stay on the record's attendance date (R1)
        punch_in_ist = target_punch_in.astimezone(IST)
        calc_date = get_attendance_date(punch_in_ist, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in must stay on the record's attendance date"
            )

        # Rule: punch_out must be after punch_in and within 24 hours
        if target_punch_out is not None:
            delta_sec = (target_punch_out - target_punch_in).total_seconds()
            if delta_sec <= 0:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be strictly after punch_in")
            if delta_sec > 86400:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out cannot be more than 24 hours after punch_in")

        # Recompute derived values
        new_late_minutes = compute_late_minutes(punch_in_ist, emp["shift_start"], date)

        if target_punch_out is not None:
            new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            new_half_day = compute_half_day(new_work_hours)
            new_overtime_minutes = compute_overtime(
                target_punch_out.astimezone(IST),
                emp["shift_start"],
                emp["shift_end"],
                date
            )
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

    # Build changes dictionary comparing previous vs new
    changes: Dict[str, Dict[str, Any]] = {}

    def normalize_dt(dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        return dt.replace(tzinfo=timezone.utc, microsecond=0)

    old_status = record.get("status")
    if old_status != target_status:
        changes["status"] = {"from": old_status, "to": target_status}

    old_pi = normalize_dt(record.get("punch_in"))
    new_pi = normalize_dt(target_punch_in)
    if old_pi != new_pi:
        changes["punch_in"] = {"from": old_pi, "to": new_pi}

    old_po = normalize_dt(record.get("punch_out"))
    new_po = normalize_dt(target_punch_out)
    if old_po != new_po:
        changes["punch_out"] = {"from": old_po, "to": new_po}

    old_wh = record.get("work_hours")
    if old_wh != new_work_hours:
        changes["work_hours"] = {"from": old_wh, "to": new_work_hours}

    old_lm = record.get("late_minutes", 0)
    if old_lm != new_late_minutes:
        changes["late_minutes"] = {"from": old_lm, "to": new_late_minutes}

    old_ot = record.get("overtime_minutes", 0)
    if old_ot != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_overtime_minutes}

    old_hd = bool(record.get("half_day", False))
    if old_hd != new_half_day:
        changes["half_day"] = {"from": old_hd, "to": new_half_day}

    # Rule: a request that changes nothing is 422
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request changes nothing"
        )

    # Create history entry
    hist_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check: match by history length to prevent lost updates
    curr_history_len = len(record.get("history") or [])
    filter_q = {
        "_id": record["_id"],
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, curr_history_len]}
    }
    update_op = {
        "$set": {
            "status": target_status,
            "punch_in": target_punch_in,
            "punch_out": target_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {"history": hist_entry}
    }

    updated = db.attendance_logs.find_one_and_update(
        filter_q,
        update_op,
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent regularization modification conflict"
        )

    return format_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_summary(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    # Calculate working days (R7): Mon-Fri starting from joined_on for mid-month joiners
    working_days = 0
    joined_on = emp["joined_on"]
    for d in range(1, num_days + 1):
        cur_date_str = f"{year:04d}-{mon:02d}-{d:02d}"
        if cur_date_str >= joined_on:
            if datetime(year, mon, d).weekday() < 5:
                working_days += 1

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$dayOfWeek", 2]},
                                    {"$lte": ["$dayOfWeek", 6]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        data = agg_res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late_minutes = int(data.get("total_late_minutes", 0))
        total_overtime_minutes = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month. Headcount includes employees with zero logs (R9)."""
    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department is not None:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$addFields": {
                "dayOfWeek": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$dayOfWeek", 2]},
                                    {"$lte": ["$dayOfWeek", 6]}
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$eq": ["$logs.status", "LEAVE"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$logs", False]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": "$present_days",
                "raw_work_hours": "$work_hours_list",
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count"
            }
        },
        {"$sort": {"department": ASCENDING}}
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        wh_list = r.pop("raw_work_hours", [])
        if wh_list:
            avg_wh = round_half_up(sum(wh_list) / len(wh_list), 2)
        else:
            avg_wh = None
        r["avg_work_hours"] = avg_wh
        r["present_days"] = float(r.get("present_days", 0.0))
        items.append(r)

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard with standard competition ranking and cutoff ties handling."""
    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$match": {"emp": {"$ne": []}}},
        {
            "$addFields": {
                "employee": {"$arrayElemAt": ["$emp", 0]}
            }
        }
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "emp_code": ASCENDING
            }
        }
    ])

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """Daily attendance trend with gap filling ($densify) and 7-day moving average."""
    # Check department exists (at least one employee)
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    from_d = dt_date.fromisoformat(from_date)
    to_d = dt_date.fromisoformat(to_date)

    if to_d < from_d:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to date must be >= from date")

    if (to_d - from_d).days > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range cannot exceed 92 days")

    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$match": {"emp.department": department}},
        {
            "$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$date"}}
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt]
                }
            }
        },
        {
            "$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$status", False]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ifNull": ["$status", False]},
                                    {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [{"$match": {"department": department}}],
                "as": "dept_emps"
            }
        },
        {
            "$addFields": {
                "date": "$_id",
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$_id"]}
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$and": [
                        {"$gte": ["$dayOfWeek", 2]},
                        {"$lte": ["$dayOfWeek", 6]}
                    ]
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        }
    ]

    raw_items = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for it in raw_items:
        rate = it.get("attendance_rate")
        m_avg = it.get("moving_avg_7d")
        it["attendance_rate"] = round_half_up(rate, 4) if rate is not None else None
        it["moving_avg_7d"] = round_half_up(m_avg, 4) if m_avg is not None else None
        items.append(it)

    return {
        "department": department,
        "items": items,
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = 1,
    page_size: Optional[int] = 20,
):
    """
    MongoDB explain output with executionStats verbosity for an endpoint's main query.
    Validates required query parameters for the explained endpoint.
    """
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown endpoint '{endpoint}'. Must be one of {valid_endpoints}"
        )

    month_regex = r"^\d{4}-(0[1-9]|1[0-2])$"

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from > date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip_val = max(0, ((page or 1) - 1) * (page_size or 20))
        limit_val = page_size or 20

        explain_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip_val,
            "limit": limit_val
        }
        res = db.command("explain", explain_cmd, verbosity="executionStats")
        return res

    elif endpoint == "employee_monthly":
        if not emp_code or not month or not re.match(month_regex, month):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and valid month (YYYY-MM) are required for employee_monthly"
            )
        year, mon = map(int, month.split("-"))
        num_days = calendar.monthrange(year, mon)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
            {
                "$project": {
                    "date": 1,
                    "status": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$gte": ["$dayOfWeek", 2]},
                                        {"$lte": ["$dayOfWeek", 6]}
                                    ]
                                },
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"}
                }
            }
        ]
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return res

    elif endpoint == "department_summary":
        if not month or not re.match(month_regex, month):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="valid month (YYYY-MM) is required for department_summary"
            )
        year, mon = map(int, month.split("-"))
        num_days = calendar.monthrange(year, mon)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        emp_match: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
        if department is not None:
            emp_match["department"] = department

        pipeline = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", start_date]},
                                        {"$lte": ["$date", end_date]}
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$addFields": {
                    "dayOfWeek": {
                        "$cond": [
                            {"$ifNull": ["$logs.date", False]},
                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                            None
                        ]
                    }
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "emp_codes": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ifNull": ["$logs", False]},
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$gte": ["$dayOfWeek", 2]},
                                        {"$lte": ["$dayOfWeek", 6]}
                                    ]
                                },
                                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                                0.0
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ifNull": ["$logs", False]},
                                        {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ifNull": ["$logs", False]},
                                        {"$eq": ["$logs.status", "LEAVE"]}
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ifNull": ["$logs", False]},
                                        {"$eq": ["$logs.status", "ON_DUTY"]}
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    }
                }
            },
            {"$sort": {"_id": ASCENDING}}
        ]
        res = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return res

    elif endpoint == "late_leaderboard":
        if not month or not re.match(month_regex, month):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="valid month (YYYY-MM) is required for late_leaderboard"
            )
        year, mon = map(int, month.split("-"))
        num_days = calendar.monthrange(year, mon)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp"
                }
            },
            {"$match": {"emp": {"$ne": []}}},
            {
                "$addFields": {
                    "employee": {"$arrayElemAt": ["$emp", 0]}
                }
            }
        ]
        if department:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}}
                }
            },
            {"$match": {"rank": {"$lte": limit or 10}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}}
        ])
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return res

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to dates are required for department_trend"
            )
        from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
        to_dt = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1)

        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp"
                }
            },
            {"$match": {"emp.department": department}},
            {"$addFields": {"date_dt": {"$dateFromString": {"dateString": "$date"}}}},
            {
                "$densify": {
                    "field": "date_dt",
                    "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt]}
                }
            },
            {
                "$group": {
                    "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ifNull": ["$status", False]},
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                    ]
                                },
                                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                0.0
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ifNull": ["$status", False]},
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    }
                }
            },
            {"$sort": {"_id": ASCENDING}}
        ]
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return res
