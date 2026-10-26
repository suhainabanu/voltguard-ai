"""The VoltGuard agent: decides which tools to call, asks the user questions, drafts work orders,
schedules follow-ups and escalates. It advises and records only - it never operates equipment.

Planning is a transparent policy (no hidden state); every tool call is logged as a case step.
An optional LLM can rewrite the plain-language explanation (see `polish`), but severity, guidance
and escalation are always decided by deterministic code so a model cannot talk the agent into
downgrading a hazard.
"""
import os

from . import tools
from .detect import SEV_ORDER

QUESTION = ("Are there any signs of burning smell, discolouration, sparks, unusual noise or a tripped "
            "breaker near this circuit?")
OPTIONS = {
    "danger": "Yes - I see or smell something unusual",
    "none": "No, nothing unusual",
    "new_load": "New equipment was recently connected",
    "unsure": "Not sure",
}
GUIDANCE = {
    "critical": ("Keep people away from the equipment area. Do not open panels or touch wiring. If there is smoke, "
                 "a burning smell or sparks, move away and call emergency services (997 Civil Defence for fire). "
                 "Contact a qualified electrician immediately."),
    "caution": ("Do not open panels or touch wiring, and avoid adding new load to this circuit. "
                "Arrange a qualified electrical inspection today."),
    "sensor_check": ("This data does not indicate an electrical hazard, but the reading cannot be trusted. "
                     "Have maintenance check the sensor. Until then treat the circuit as unmonitored and keep routine inspections."),
}
PRIORITY = {"critical": "URGENT", "caution": "HIGH", "sensor_check": "LOW"}
FOLLOWUP_MIN = {"critical": 30, "caution": 60, "sensor_check": 120}
MAX_ESCALATION = 3
ESCALATION_LABEL = ["", "facility supervisor", "operations manager", "senior management"]


# ----------------------------------------------------------------------------- helpers
def log(db, case_id, tool, summary, detail=None):
    db.x("INSERT INTO steps(case_id,t_min,tool,summary,detail) VALUES(?,?,?,?,?)",
         case_id, db.now_min, tool, summary, detail)


def open_case(db, circuit_id):
    return db.one("SELECT * FROM cases WHERE circuit_id=? AND status!='resolved' ORDER BY id DESC LIMIT 1", circuit_id)


def explain(tr, answer=None):
    p = tr["pattern"]
    if p in ("thermal_drift", "overheating"):
        t = ("This circuit is running hotter than it normally does at the same load and ambient temperature. "
             "This may indicate overheating or a developing fault, but the readings alone cannot confirm the cause.")
    elif p == "overload":
        t = ("Current is above what this circuit is rated for, and temperature is rising with it. "
             "This may indicate an overload; the readings alone cannot show which equipment is responsible.")
    elif p == "intermittent_heating":
        t = ("Temperature is spiking and falling back repeatedly, with matching voltage dips. "
             "This pattern is sometimes seen with loose or degrading connections, but only an inspection can confirm it.")
    elif p == "sensor_fault":
        t = "These readings look like a sensor problem rather than an electrical one."
    else:
        t = "Readings are within this circuit's normal range."
    if answer == "danger":
        t += " The person on site reported warning signs, so the case has been raised."
    elif answer == "new_load":
        t += " Newly connected equipment may explain part of this; it should be confirmed by an electrician."
    t += f" Confidence in this assessment: {tr['confidence']:.0%}."
    return polish(t, tr.get("evidence", []))


def polish(text, evidence):
    """Optional: let an LLM rephrase the explanation. Falls back to the template on any problem.
    Needs `pip install anthropic` and ANTHROPIC_API_KEY. Not used by the tests or the evaluation."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return text
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=os.environ.get("VOLTGUARD_MODEL", "claude-sonnet-5-5"), max_tokens=300,
            system=("Rewrite this electrical-alert explanation for a non-expert facility manager in 3 sentences or fewer. "
                    "Keep every fact, keep the uncertainty and the confidence figure. Never add instructions, "
                    "never claim a diagnosis, never tell anyone to touch or open equipment."),
            messages=[{"role": "user", "content": text + "\nEvidence: " + " ".join(evidence)}])
        out = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        return out or text
    except Exception:
        return text


def _wo(db, case, circ, tr, hist, answer=None):
    sev = case["severity"]
    title = {"sensor_check": f"Check temperature sensor - {circ['site']} / {circ['name']}"}.get(
        sev, f"{'URGENT inspection' if sev == 'critical' else 'Inspection'} - {circ['site']} / {circ['name']}")
    lines = [f"Severity: {sev.upper()} (confidence {case['confidence']:.0%})", f"Pattern: {tr['pattern']}",
             "Evidence:"] + [f" - {e}" for e in tr["evidence"]]
    lines.append(f"History: {hist['prior_alerts']} earlier alert(s). {hist['note']}")
    if answer:
        lines.append(f"Reported on site: {OPTIONS[answer]}")
    lines += ["Safe guidance: " + GUIDANCE[sev], "DRAFT - requires human approval before sending."]
    return tools.create_work_order(db, case["id"], title, "\n".join(lines), PRIORITY[sev])


def _set_severity(db, case_id, sev, reason):
    c = db.one("SELECT severity FROM cases WHERE id=?", case_id)
    if SEV_ORDER[sev] > SEV_ORDER[c["severity"]]:
        db.x("UPDATE cases SET severity=?, guidance=? WHERE id=?", sev, GUIDANCE[sev], case_id)
        log(db, case_id, "rank_severity", f"Raised {c['severity']} -> {sev}: {reason}")


def _finish_plan(db, case_id, tr, hist, answer=None):
    """Draft the work order and schedule the follow-up for the case's current severity."""
    case = db.one("SELECT * FROM cases WHERE id=?", case_id)
    circ = db.circuit(case["circuit_id"])
    wo = _wo(db, case, circ, tr, hist, answer)
    log(db, case_id, "create_work_order",
        f"{'Updated' if wo['updated'] else 'Drafted'} {wo['code']} ({wo['priority']}) - awaiting human approval")
    mins = FOLLOWUP_MIN[case["severity"]]
    if answer == "new_load" and case["severity"] != "critical":
        mins = 120
    s = tools.schedule_followup(db, case_id, mins)
    log(db, case_id, "schedule_followup", f"Re-check in {mins} min; escalates if not acknowledged")
    db.x("UPDATE cases SET explanation=? WHERE id=?", explain(tr, answer), case_id)


# ----------------------------------------------------------------------------- main entry points
def process(db, circuit_id):
    """Run the agent on a circuit's newest readings. Returns a short summary dict."""
    circ = db.circuit(circuit_id)
    rd = tools.get_readings(db, circuit_id, 48)
    rules = tools.check_rules(db, circuit_id)
    tr = tools.trend_analysis(db, circuit_id)
    sev = tr["severity"]
    case = open_case(db, circuit_id)

    if sev == "normal":
        if case:
            log(db, case["id"], "trend_analysis", "Re-check: readings are back within this circuit's normal range. "
                "Case stays open until a person confirms.")
        return {"circuit_id": circuit_id, "status": "normal"}

    hist = tools.search_maintenance_log(db, circuit_id)
    if case:  # existing case: only escalate, never silently downgrade
        log(db, case["id"], "trend_analysis", f"Re-check: {sev} / {tr['pattern']} (excess {tr['excess_c']:+.1f} C)")
        if SEV_ORDER[sev] > SEV_ORDER[case["severity"]]:
            _set_severity(db, case["id"], sev, "readings worsened")
            _finish_plan(db, case["id"], tr, hist, case["answer"])
        return {"circuit_id": circuit_id, "status": "case", "case_id": case["id"]}

    cid = db.x("INSERT INTO cases(circuit_id,status,severity,pattern,confidence,excess,slope,opened_min,guidance)"
               " VALUES(?,?,?,?,?,?,?,?,?)", circuit_id, "open", sev, tr["pattern"], tr["confidence"],
               tr["excess_c"], tr["slope_c_per_reading"], db.now_min, GUIDANCE[sev])
    log(db, cid, "get_readings", f"Read latest {rd['count']} readings for {circ['site']} / {circ['name']}")
    log(db, cid, "check_rules", ("; ".join(rules["rules_hit"]) or "No fixed limit crossed") +
        (" Data issues: " + "; ".join(rules["sensor_issues"]) if rules["sensor_issues"] else ""))
    log(db, cid, "trend_analysis", f"Pattern {tr['pattern']}: {tr['excess_c']:+.1f} C vs this circuit's baseline, "
        f"trend {tr['slope_c_per_reading']:+.1f} C/reading, confidence {tr['confidence']:.0%}")
    log(db, cid, "search_maintenance_log", f"{hist['prior_alerts']} earlier alert(s). {hist['note']}")
    log(db, cid, "rank_severity", f"{sev.upper()} - " + (tr["evidence"][0] if tr["evidence"] else "see evidence"))
    db.x("UPDATE cases SET explanation=? WHERE id=?", explain(tr), cid)

    if sev == "sensor_check":
        _finish_plan(db, cid, tr, hist)
    elif sev == "critical":
        _finish_plan(db, cid, tr, hist)                     # do not wait for an answer on a critical case
        tools.ask_user(db, cid, QUESTION)
        log(db, cid, "ask_user", "Asked for on-site warning signs (provisional work order already drafted)")
    else:
        tools.ask_user(db, cid, QUESTION)
        log(db, cid, "ask_user", "Asked for on-site warning signs before drafting the work order")
        tools.schedule_followup(db, cid, 15)                # if nobody answers, the follow-up proceeds conservatively
    return {"circuit_id": circuit_id, "status": "case", "case_id": cid}


def answer(db, case_id, key):
    if key not in OPTIONS:
        raise ValueError("unknown answer")
    case = db.one("SELECT * FROM cases WHERE id=?", case_id)
    if not case or case["status"] == "resolved":
        raise ValueError("no open case")
    db.x("UPDATE cases SET answer=?, status='open' WHERE id=?", key, case_id)
    log(db, case_id, "ask_user", f"Answer received: {OPTIONS[key]}")
    tr = tools.trend_analysis(db, case["circuit_id"])
    hist = tools.search_maintenance_log(db, case["circuit_id"])
    if key == "danger":
        _set_severity(db, case_id, "critical", "warning signs reported on site")
    elif key == "new_load":
        conf = round(max(0.3, case["confidence"] - 0.15), 2)
        db.x("UPDATE cases SET confidence=? WHERE id=?", conf, case_id)
        tr["confidence"] = conf
        log(db, case_id, "rank_severity", "Severity kept; confidence lowered because a load change is a possible explanation")
    elif key == "unsure":
        log(db, case_id, "rank_severity", "Severity kept; an unsure answer is treated cautiously")
    else:
        log(db, case_id, "rank_severity", "Severity kept; absence of visible signs does not rule out a fault")
    _finish_plan(db, case_id, tr, hist, key)
    return db.one("SELECT * FROM cases WHERE id=?", case_id)


def acknowledge(db, case_id):
    db.x("UPDATE cases SET ack_min=?, status=CASE WHEN status='awaiting_user' THEN status ELSE 'acknowledged' END WHERE id=?",
         db.now_min, case_id)
    log(db, case_id, "acknowledge", "Facility manager acknowledged the alert")
    db.x("UPDATE work_orders SET status='approved - sent to electrician' WHERE case_id=? AND status LIKE 'draft%'", case_id)
    if db.one("SELECT 1 AS x FROM work_orders WHERE case_id=? AND status LIKE 'approved%'", case_id):
        log(db, case_id, "create_work_order", "Work order approved by a human and sent")


def resolve(db, case_id, note="Inspection completed"):
    db.x("UPDATE cases SET status='resolved', closed_min=?, note=?, due_min=NULL WHERE id=?", db.now_min, note, case_id)
    db.x("UPDATE work_orders SET status='closed' WHERE case_id=?", case_id)
    log(db, case_id, "resolve", f"Resolved: {note}")


def tick(db, minutes):
    """Advance the demo clock; run follow-ups that came due."""
    db.set_meta("clock_min", db.now_min + minutes)
    fired = []
    for c in db.q("SELECT * FROM cases WHERE status!='resolved' AND due_min IS NOT NULL AND due_min<=?", db.now_min):
        cid = c["id"]
        if c["status"] == "awaiting_user":
            log(db, cid, "schedule_followup", "No answer received; proceeding conservatively (treated as 'not sure')")
            answer(db, cid, "unsure")
            c = db.one("SELECT * FROM cases WHERE id=?", cid)
        if c["ack_min"] is None:
            esc = min(MAX_ESCALATION, c["escalation"] + 1)
            db.x("UPDATE cases SET escalation=? WHERE id=?", esc, cid)
            log(db, cid, "schedule_followup", f"Follow-up due and still not acknowledged -> escalated to {ESCALATION_LABEL[esc]}")
            if esc >= 2:
                _set_severity(db, cid, "critical", "no acknowledgement after repeated escalation")
        else:
            log(db, cid, "schedule_followup", "Follow-up due: re-checking latest readings")
            process(db, c["circuit_id"])
        db.x("UPDATE cases SET due_min=? WHERE id=?", db.now_min + 30, cid)
        fired.append(cid)
    return fired


# ----------------------------------------------------------------------------- triage
def triage(db):
    out = []
    for c in db.q("SELECT * FROM cases WHERE status!='resolved'"):
        circ = db.circuit(c["circuit_id"])
        w = {"critical": 100, "caution": 50, "sensor_check": 15}[c["severity"]]
        reasons = [f"{c['severity']} ({c['confidence']:.0%} confidence)"]
        score = w * (0.6 + 0.4 * c["confidence"])
        score += 10 * circ["criticality"]
        if circ["criticality"] == 3:
            reasons.append("critical area")
        if c["slope"] and c["slope"] > 0:
            score += min(c["slope"], 3) * 5
            if c["slope"] >= 1:
                reasons.append("worsening trend")
        if c["ack_min"] is None:
            hrs = min(10, (db.now_min - c["opened_min"]) / 60)
            score += hrs * 2
            if hrs >= 0.5:
                reasons.append("unacknowledged")
        if circ["prior_alerts"]:
            score += min(15, circ["prior_alerts"] * 3)
            reasons.append(f"{circ['prior_alerts']} earlier alerts")
        if circ["insp_days_ago"] is None:
            score += 10
            reasons.append("never inspected")
        elif circ["insp_days_ago"] > 120:
            score += 6
            reasons.append(f"inspected {circ['insp_days_ago']} days ago")
        score += c["escalation"] * 10
        out.append({"case_id": c["id"], "circuit_id": c["circuit_id"], "circuit": circ["name"], "site": circ["site"],
                    "severity": c["severity"], "score": round(score, 1), "reasons": reasons, "status": c["status"],
                    "escalation": c["escalation"]})
    out.sort(key=lambda r: -r["score"])
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out
