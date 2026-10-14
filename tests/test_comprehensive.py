import calendar
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app, db, IST, round_half_up, init_indexes

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    """Ensure indexes."""
    init_indexes()
    yield


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_employee_crud_and_concurrency():
    emp_code = "EMP9901"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    payload = {
        "emp_code": emp_code,
        "name": "Test User",
        "email": "test.user@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }

    # 1. Successful creation
    resp = client.post("/employees", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["emp_code"] == emp_code
    assert isinstance(data["created_at"], int)
    assert "_id" not in data

    # 2. Sequential duplicate creation -> 409
    resp_dup = client.post("/employees", json=payload)
    assert resp_dup.status_code == 409

    # 3. Validation error -> 422 (invalid email)
    bad_payload = dict(payload, email="bad-email", emp_code="EMP9902")
    resp_bad = client.post("/employees", json=bad_payload)
    assert resp_bad.status_code == 422

    # 4. Concurrent creation with same code -> exactly one 201, rest 409
    emp_code_conc = "EMP9903"
    db.employees.delete_many({"emp_code": emp_code_conc})
    payload_conc = dict(payload, emp_code=emp_code_conc, email="conc@example.com")

    results = []
    def create_emp():
        r = client.post("/employees", json=payload_conc)
        results.append(r.status_code)

    threads = [threading.Thread(target=create_emp) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(201) == 1
    assert results.count(409) == 4

    # 5. List employees with department filter and pagination
    list_resp = client.get("/employees?department=Engineering&page=1&page_size=10")
    assert list_resp.status_code == 200
    l_data = list_resp.json()
    assert "items" in l_data
    assert "total" in l_data
    assert l_data["page"] == 1
    for it in l_data["items"]:
        assert it["department"] == "Engineering"
        assert "_id" not in it
        assert isinstance(it["created_at"], int)


def test_punch_in_and_race_safety():
    emp_code = "EMP9904"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Punch Tester",
        "email": "punch@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Non-existent employee punch in -> 404
    r_404 = client.post("/attendance/punch-in", json={"emp_code": "EMP0000"})
    assert r_404.status_code == 404

    # Punch in with specific time: 2026-07-06 09:40:00 IST -> late_minutes = 0
    dt_940 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    ms_940 = int(dt_940.timestamp() * 1000)

    r_in = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": ms_940,
        "status": "PRESENT"
    })
    assert r_in.status_code == 201
    rec = r_in.json()
    assert rec["date"] == "2026-07-06"
    assert rec["late_minutes"] == 0
    assert rec["punch_in"] == ms_940
    assert rec["work_hours"] is None
    assert "_id" not in rec

    # Duplicate punch in on same day -> 409
    r_dup = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": ms_940 + 5000,
        "status": "PRESENT"
    })
    assert r_dup.status_code == 409

    # Concurrent punch in on a new date: 2026-07-07
    dt_next = datetime(2026, 7, 7, 4, 10, 0, tzinfo=timezone.utc)
    ms_next = int(dt_next.timestamp() * 1000)

    results = []
    def do_punch():
        res = client.post("/attendance/punch-in", json={
            "emp_code": emp_code,
            "punched_at": ms_next,
            "status": "PRESENT"
        })
        results.append(res.status_code)

    threads = [threading.Thread(target=do_punch) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(201) == 1
    assert results.count(409) == 4


def test_business_rules_r1_to_r5():
    emp_night = "EMP9905"
    db.employees.delete_many({"emp_code": emp_night})
    db.attendance_logs.delete_many({"emp_code": emp_night})

    client.post("/employees", json={
        "emp_code": emp_night,
        "name": "Night Owl",
        "email": "night@example.com",
        "department": "Security",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })

    # Punch in at 05:45 IST on 2026-07-07 -> Earlier than shift_end 06:00, belongs to 2026-07-06!
    dt_night_in = datetime(2026, 7, 7, 0, 15, 0, tzinfo=timezone.utc)
    ms_night_in = int(dt_night_in.timestamp() * 1000)

    r_night = client.post("/attendance/punch-in", json={
        "emp_code": emp_night,
        "punched_at": ms_night_in,
        "status": "PRESENT"
    })
    assert r_night.status_code == 201
    assert r_night.json()["date"] == "2026-07-06"

    # R2 boundary test: Shift 09:30
    emp_r2 = "EMP9906"
    db.employees.delete_many({"emp_code": emp_r2})
    db.attendance_logs.delete_many({"emp_code": emp_r2})
    client.post("/employees", json={
        "emp_code": emp_r2,
        "name": "R2 Tester",
        "email": "r2@example.com",
        "department": "Ops",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Test 09:40:01 IST -> exactly 10 minutes late
    dt_40_01 = datetime(2026, 7, 8, 4, 10, 1, tzinfo=timezone.utc)
    r_late_10 = client.post("/attendance/punch-in", json={
        "emp_code": emp_r2,
        "punched_at": int(dt_40_01.timestamp() * 1000),
        "status": "PRESENT"
    })
    assert r_late_10.status_code == 201
    assert r_late_10.json()["late_minutes"] == 10

    # Test 10:15:59 IST -> 45 minutes late
    dt_15_59 = datetime(2026, 7, 9, 4, 45, 59, tzinfo=timezone.utc)
    r_late_45 = client.post("/attendance/punch-in", json={
        "emp_code": emp_r2,
        "punched_at": int(dt_15_59.timestamp() * 1000),
        "status": "PRESENT"
    })
    assert r_late_45.status_code == 201
    assert r_late_45.json()["late_minutes"] == 45


def test_punch_out_and_concurrency():
    emp_code = "EMP9907"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Punchout Tester",
        "email": "po@example.com",
        "department": "Finance",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in at 09:28 IST (03:58 UTC) on 2026-07-10
    dt_in = datetime(2026, 7, 10, 3, 58, 0, tzinfo=timezone.utc)
    ms_in = int(dt_in.timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms_in})

    # 1. Invalid punch-out: before punch-in -> 422
    r_early = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ms_in - 10000})
    assert r_early.status_code == 422

    # 2. Invalid punch-out: > 24 hours later -> 422
    r_late24 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ms_in + 86401000})
    assert r_late24.status_code == 422

    # 3. Overtime test: Shift ends 18:30. Punch out at 18:59 IST (29m -> 0 overtime)
    dt_in_2 = datetime(2026, 7, 11, 3, 58, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(dt_in_2.timestamp() * 1000)})

    dt_out_29 = datetime(2026, 7, 11, 13, 29, 0, tzinfo=timezone.utc)
    r_ot29 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": int(dt_out_29.timestamp() * 1000)})
    assert r_ot29.status_code == 200
    assert r_ot29.json()["overtime_minutes"] == 0

    # Punch in on 2026-07-12, punch out at 19:00 IST (30m -> 30 overtime)
    dt_in_3 = datetime(2026, 7, 12, 3, 58, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(dt_in_3.timestamp() * 1000)})

    dt_out_30 = datetime(2026, 7, 12, 13, 30, 0, tzinfo=timezone.utc)
    r_ot30 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": int(dt_out_30.timestamp() * 1000)})
    assert r_ot30.status_code == 200
    assert r_ot30.json()["overtime_minutes"] == 30

    # 4. Half day calculation: 4.49 hours -> half_day = True
    dt_in_hd = datetime(2026, 7, 13, 4, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(dt_in_hd.timestamp() * 1000)})

    dt_out_hd = dt_in_hd + timedelta(seconds=16164)
    r_hd = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": int(dt_out_hd.timestamp() * 1000)})
    assert r_hd.status_code == 200
    assert r_hd.json()["work_hours"] == 4.49
    assert r_hd.json()["half_day"] is True

    # 5. Concurrent punch out race safety
    dt_in_conc = datetime(2026, 7, 14, 4, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(dt_in_conc.timestamp() * 1000)})
    dt_out_conc = dt_in_conc + timedelta(hours=9)
    ms_out_conc = int(dt_out_conc.timestamp() * 1000)

    results = []
    def do_punchout():
        res = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ms_out_conc})
        results.append(res.status_code)

    threads = [threading.Thread(target=do_punchout) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1
    assert results.count(409) == 4


def test_patch_regularization_and_history():
    emp_code = "EMP9908"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Audit Tester",
        "email": "audit@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    dt_in = datetime(2026, 7, 15, 4, 35, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(dt_in.timestamp() * 1000)})

    # 1. Changing nothing -> 422
    r_nochange = client.patch(f"/attendance/{emp_code}/2026-07-15", json={
        "reason": "testing no change",
        "regularized_by": "admin"
    })
    assert r_nochange.status_code == 422

    # 2. Status ABSENT with punch_in -> 422
    r_bad_absent = client.patch(f"/attendance/{emp_code}/2026-07-15", json={
        "status": "ABSENT",
        "punch_in": int(dt_in.timestamp() * 1000),
        "reason": "testing absent with time",
        "regularized_by": "admin"
    })
    assert r_bad_absent.status_code == 422

    # 3. Successful correction
    new_in = datetime(2026, 7, 15, 3, 58, 0, tzinfo=timezone.utc)
    new_out = datetime(2026, 7, 15, 13, 0, 0, tzinfo=timezone.utc)
    ms_new_in = int(new_in.timestamp() * 1000)
    ms_new_out = int(new_out.timestamp() * 1000)

    r_patch = client.patch(f"/attendance/{emp_code}/2026-07-15", json={
        "punch_in": ms_new_in,
        "punch_out": ms_new_out,
        "reason": "biometric error",
        "regularized_by": "hr.manager"
    })
    assert r_patch.status_code == 200
    patched = r_patch.json()
    assert patched["late_minutes"] == 0
    assert patched["work_hours"] == 9.03
    assert len(patched["history"]) == 1
    h = patched["history"][0]
    assert h["by"] == "hr.manager"
    assert h["reason"] == "biometric error"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 35
    assert h["changes"]["late_minutes"]["to"] == 0
    assert "punch_in" in h["changes"]

    # Clean up test user
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})


def test_leaderboard_competition_ranking_ties():
    # Setup test dataset for leaderboard ties
    os.system(f'"{sys.executable}" sample_seed.py')

    # Seed two employees with tied late minutes
    db.employees.delete_many({"emp_code": {"$in": ["EMPT01", "EMPT02", "EMPT03"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMPT01", "EMPT02", "EMPT03"]}})

    for code in ["EMPT01", "EMPT02", "EMPT03"]:
        db.employees.insert_one({
            "emp_code": code,
            "name": f"User {code}",
            "email": f"{code}@test.com",
            "department": "TieDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc)
        })

    # EMPT01: 50 late minutes (Rank 1)
    # EMPT02: 40 late minutes (Rank 2)
    # EMPT03: 40 late minutes (Rank 2)
    db.attendance_logs.insert_one({
        "emp_code": "EMPT01",
        "date": "2026-07-01",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 1, 4, 50, 0, tzinfo=timezone.utc),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 50,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    })
    db.attendance_logs.insert_one({
        "emp_code": "EMPT02",
        "date": "2026-07-01",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 1, 4, 40, 0, tzinfo=timezone.utc),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 40,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    })
    db.attendance_logs.insert_one({
        "emp_code": "EMPT03",
        "date": "2026-07-01",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 1, 4, 40, 0, tzinfo=timezone.utc),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 40,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    })

    # Query with limit=2 inside TieDept
    # Because EMPT02 and EMPT03 tie at rank 2 <= 2, BOTH must be returned!
    # Expected rows: 3 rows returned (ranks 1, 2, 2)
    r_tie = client.get("/analytics/leaderboard/late?month=2026-07&limit=2&department=TieDept")
    assert r_tie.status_code == 200
    items = r_tie.json()["items"]
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMPT01" and items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMPT02" and items[1]["rank"] == 2
    assert items[2]["emp_code"] == "EMPT03" and items[2]["rank"] == 2


def test_mid_month_joiner_and_weekends():
    # Employee joining mid-month: 2026-07-15
    emp_mid = "EMP9909"
    db.employees.delete_many({"emp_code": emp_mid})
    db.attendance_logs.delete_many({"emp_code": emp_mid})

    db.employees.insert_one({
        "emp_code": emp_mid,
        "name": "Mid Month",
        "email": "mid@test.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-15",
        "created_at": datetime.now(timezone.utc)
    })

    # July 15 to July 31 2026:
    # Weekdays in 15..31: 15(Wed), 16(Thu), 17(Fri), 20(Mon), 21(Tue), 22(Wed), 23(Thu), 24(Fri), 27(Mon), 28(Tue), 29(Wed), 30(Thu), 31(Fri)
    # Total = 13 working days!
    # Let's add a weekend attendance record: 2026-07-18 (Saturday) with 20 min late
    db.attendance_logs.insert_one({
        "emp_code": emp_mid,
        "date": "2026-07-18",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 18, 4, 20, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 18, 13, 0, 0, tzinfo=timezone.utc),
        "work_hours": 8.67,
        "late_minutes": 20,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    })

    # Add a weekday attendance record: 2026-07-20 (Monday)
    db.attendance_logs.insert_one({
        "emp_code": emp_mid,
        "date": "2026-07-20",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 20, 13, 0, 0, tzinfo=timezone.utc),
        "work_hours": 9.00,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    })

    res = client.get(f"/analytics/employees/{emp_mid}/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 13
    # Weekend record does NOT add to present_days (only 1 weekday present -> 1.0)
    assert data["present_days"] == 1.0
    # Weekend record DOES add to total_late_minutes (20)
    assert data["total_late_minutes"] == 20
    assert data["late_count"] == 1
    assert data["attendance_pct"] == round_half_up((1.0 / 13) * 100, 2)


def test_analytics_endpoints_and_seed_data():
    # Reload sample seed data
    os.system(f'"{sys.executable}" sample_seed.py')

    # 1. Employee Monthly
    r_m = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert r_m.status_code == 200
    data_m = r_m.json()
    assert data_m["emp_code"] == "EMP0001"
    assert data_m["month"] == "2026-07"
    assert data_m["working_days"] == 23 # July 2026 has 23 weekdays
    assert data_m["present_days"] == 2.0
    assert data_m["attendance_pct"] == round_half_up((2.0 / 23) * 100, 2)

    # 2. Department Summary
    r_ds = client.get("/analytics/departments/summary?month=2026-07")
    assert r_ds.status_code == 200
    data_ds = r_ds.json()
    assert data_ds["month"] == "2026-07"
    depts = {it["department"]: it for it in data_ds["items"]}
    assert "Engineering" in depts
    assert "Sales" in depts
    assert "Support" in depts
    assert depts["Engineering"]["headcount"] == 2
    assert depts["Sales"]["headcount"] == 2
    assert depts["Support"]["headcount"] == 2

    # 3. Late Leaderboard
    r_lb = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert r_lb.status_code == 200
    data_lb = r_lb.json()
    assert len(data_lb["items"]) >= 1
    top = data_lb["items"][0]
    assert top["emp_code"] == "EMP0003"
    assert top["rank"] == 1
    assert top["total_late_minutes"] == 35

    # 4. Department Trend
    r_tr = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-08")
    assert r_tr.status_code == 200
    data_tr = r_tr.json()
    assert data_tr["department"] == "Engineering"
    assert len(data_tr["items"]) == 3
    assert [it["date"] for it in data_tr["items"]] == ["2026-07-06", "2026-07-07", "2026-07-08"]
    assert data_tr["items"][0]["present_count"] == 1.0
    assert data_tr["items"][1]["present_count"] == 1.0
    assert data_tr["items"][2]["present_count"] == 0.0

    # 5. Explain endpoints
    for ep, q_params in [
        ("attendance_list", "page=1&page_size=10"),
        ("employee_monthly", "emp_code=EMP0001&month=2026-07"),
        ("department_summary", "month=2026-07"),
        ("late_leaderboard", "month=2026-07"),
        ("department_trend", "department=Engineering&from=2026-07-06&to=2026-07-08")
    ]:
        resp_exp = client.get(f"/admin/explain/{ep}?{q_params}")
        assert resp_exp.status_code == 200
        assert "executionStats" in resp_exp.json() or "stages" in resp_exp.json()
