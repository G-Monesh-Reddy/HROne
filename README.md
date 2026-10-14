# Employee Attendance & Analytics API

FastAPI and MongoDB backend implementation for employee attendance tracking and analytics, conforming to Contract v2.0 (`openapi.yaml`) and business rules R1–R10.

## Tech Stack
- **Python:** 3.11+ (tested on Python 3.12)
- **Framework:** FastAPI
- **Database:** MongoDB 6.0+ (tested on MongoDB 8.0)
- **Driver:** PyMongo 4.7+

---

## Setup & Running

### 1. Environment Configuration
Set the following environment variables (or define them in a local `.env` file):
```bash
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*(Environment variables take precedence over `.env`)*

### 2. Install Dependencies
```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Seed Sample Data
```bash
python sample_seed.py
```

### 4. Start Server
```bash
uvicorn app.main:app --port 8000
```
- API Base URL: `http://localhost:8000`
- Interactive Swagger UI: `http://localhost:8000/docs`
- Health/Readiness Probe: `http://localhost:8000/health`

---

## Testing

Run the automated test suite covering all business rules, endpoints, edge cases, and concurrency guarantees:
```bash
python -m pytest tests/test_comprehensive.py -v
```

---

## Architectural Highlights
- **Single File API:** All application logic resides in `app/main.py`.
- **Concurrency Safety:** Database-level unique compound indexes and atomic conditional updates (`find_one_and_update`) prevent race conditions during parallel punch-ins, punch-outs, and employee creation.
- **Audit History:** Append-only history with optimistic concurrency control tracking only modified fields.
- **Analytics Pipelines:** Pure MongoDB aggregation pipelines utilizing `$densify`, `$setWindowFields` (`$rank`, moving averages), and cross-collection joins without loading large recordsets into application memory.
- **Explain Endpoint:** `GET /admin/explain/{endpoint}` returns execution plans with `IXSCAN` index usage.
