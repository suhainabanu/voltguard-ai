"""The seven tools the agent can call. Each returns a plain dict and never touches equipment."""
from . import detect


def get_readings(db, circuit_id, n=48):
    rows = db.readings(circuit_id, n)
    return {"circuit_id": circuit_id, "count": len(rows), "latest": rows[-1] if rows else None, "readings": rows}


def get_history(db, circuit_id, hours=24 * 30):
    rows = db.readings(circuit_id, hours)
    temps = [r["temp"] for r in rows if r["temp"] is not None]
    cur = [r["current"] for r in rows]
    return {"circuit_id": circuit_id, "hours": len(rows),
            "temp_mean": round(sum(temps) / len(temps), 1), "temp_max": max(temps),
            "current_mean": round(sum(cur) / len(cur), 1), "current_max": max(cur)}


def check_rules(db, circuit_id, cfg=None):
    """Electrician-set fixed limits + data validation."""
    circ = db.circuit(circuit_id)
    a = detect.analyze(db.readings(circuit_id), circ, cfg)
    return {"fixed_status": a["fixed_status"], "rules_hit": [m for _, m in a["rules_hit"]],
            "sensor_issues": a["sensor_issues"], "overload_ratio": a["overload_ratio"]}


def trend_analysis(db, circuit_id, cfg=None):
    """Learned per-circuit baseline: how far is the circuit from its own normal, and which way is it moving?"""
    circ = db.circuit(circuit_id)
    a = detect.analyze(db.readings(circuit_id), circ, cfg)
    return {"severity": a["severity"], "pattern": a["pattern"], "excess_c": a["excess"], "slope_c_per_reading": a["slope"],
            "expected_temp": a["expected"], "confidence": a["confidence"], "fit_r2": a["fit_r2"], "evidence": a["evidence"]}


def search_maintenance_log(db, circuit_id):
    c = db.circuit(circuit_id)
    cases = db.q("SELECT id, severity, status, opened_min FROM cases WHERE circuit_id=?", circuit_id)
    return {"prior_alerts": c["prior_alerts"] + len([x for x in cases if x["status"] == "resolved"]),
            "days_since_inspection": c["insp_days_ago"],
            "open_work_orders": len(db.q("SELECT wo.id FROM work_orders wo JOIN cases ca ON ca.id=wo.case_id "
                                         "WHERE ca.circuit_id=? AND wo.status!='closed'", circuit_id)),
            "note": ("No inspection on record." if c["insp_days_ago"] is None else f"Last inspection {c['insp_days_ago']} days ago.")}


def ask_user(db, case_id, question):
    db.x("UPDATE cases SET question=?, status='awaiting_user' WHERE id=?", question, case_id)
    return {"asked": question}


def create_work_order(db, case_id, title, body, priority):
    n = len(db.q("SELECT id FROM work_orders")) + 1041
    code = f"WO-{n + 1}"
    # one open work order per case: update instead of duplicating
    ex = db.one("SELECT id, code FROM work_orders WHERE case_id=? AND status!='closed'", case_id)
    if ex:
        db.x("UPDATE work_orders SET title=?, body=?, priority=? WHERE id=?", title, body, priority, ex["id"])
        return {"code": ex["code"], "updated": True, "priority": priority}
    db.x("INSERT INTO work_orders(case_id,code,title,body,priority,status,created_min) VALUES(?,?,?,?,?,?,?)",
         case_id, code, title, body, priority, "draft - awaiting approval", db.now_min)
    return {"code": code, "updated": False, "priority": priority}


def schedule_followup(db, case_id, minutes):
    due = db.now_min + minutes
    db.x("UPDATE cases SET due_min=? WHERE id=?", due, case_id)
    return {"due_in_min": minutes, "due_min": due}


TOOLS = {f.__name__: f for f in (get_readings, get_history, check_rules, trend_analysis,
                                 search_maintenance_log, ask_user, create_work_order, schedule_followup)}
