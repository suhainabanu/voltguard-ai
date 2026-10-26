"""Flask API + static dashboard."""
import os
import random

from flask import Flask, jsonify, request, send_from_directory

from . import agent, detect, sim
from .db import DB

HERE = os.path.dirname(os.path.abspath(__file__))


def create_app(db_path=None):
    app = Flask(__name__, static_folder=os.path.join(HERE, "..", "static"), static_url_path="/static")
    db = DB(db_path or os.environ.get("VOLTGUARD_DB", ":memory:"))
    if not db.one("SELECT 1 AS x FROM sites"):
        db.reset()
    app.db = db

    def circuit_status(c):
        rd = db.readings(c["id"])
        a = detect.analyze(rd, c)
        case = agent.open_case(db, c["id"])
        last = rd[-1]
        return {"id": c["id"], "name": c["name"], "rated_a": c["rated_a"], "last": last,
                "severity": case["severity"] if case else a["severity"],
                "fixed_status": sim.fixed_rule_status(last, c), "case_id": case["id"] if case else None}

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/state")
    def state():
        sites = []
        for s in db.q("SELECT * FROM sites"):
            circs = [circuit_status(c) | {} for c in db.q("SELECT * FROM circuits WHERE site_id=?", s["id"])]
            sites.append({"id": s["id"], "name": s["name"], "kind": s["kind"], "circuits": circs})
        return jsonify({"clock_min": db.now_min, "sites": sites, "triage": agent.triage(db),
                        "scenarios": sim.SCENARIO_LABELS,
                        "llm": bool(os.environ.get("ANTHROPIC_API_KEY"))})

    @app.post("/api/inject")
    def inject():
        b = request.get_json(force=True)
        cid, sc = int(b["circuit_id"]), b["scenario"]
        if sc not in sim.SCENARIOS:
            return jsonify({"error": "unknown scenario"}), 400
        circ = db.circuit(cid)
        rd = db.readings(cid)
        rng = random.Random(b.get("seed") or random.randrange(10**6))
        new = sim.scenario_readings(circ, sc, rd[-1]["hour"] + 1, rng)
        db.append_readings(cid, new)
        res = agent.process(db, cid)
        return jsonify(res)

    @app.post("/api/run")
    def run():
        b = request.get_json(silent=True) or {}
        ids = [int(b["circuit_id"])] if b.get("circuit_id") else [c["id"] for c in db.q("SELECT id FROM circuits")]
        return jsonify([agent.process(db, i) for i in ids])

    @app.get("/api/cases/<int:cid>")
    def case(cid):
        c = db.one("SELECT * FROM cases WHERE id=?", cid)
        if not c:
            return jsonify({"error": "not found"}), 404
        circ = db.circuit(c["circuit_id"])
        rd = db.readings(c["circuit_id"], 24 * 7 + 12)
        hist = db.readings(c["circuit_id"])[:-12]
        coef, sd, r2 = detect.fit_baseline(hist, circ["rated_a"])
        series = [{"hour": r["hour"], "temp": r["temp"], "current": r["current"], "ambient": r["ambient"],
                   "expected": round(detect.expected_temp(coef, r, circ["rated_a"]), 1)} for r in rd[-48:]]
        return jsonify({"case": c, "circuit": circ, "series": series, "options": agent.OPTIONS,
                        "steps": db.q("SELECT * FROM steps WHERE case_id=? ORDER BY id", cid),
                        "work_order": db.one("SELECT * FROM work_orders WHERE case_id=? ORDER BY id DESC", cid),
                        "limits": {"temp_caution": detect.DEFAULTS["temp_caution"], "temp_critical": detect.DEFAULTS["temp_critical"]},
                        "clock_min": db.now_min})

    @app.post("/api/cases/<int:cid>/answer")
    def answer(cid):
        try:
            agent.answer(db, cid, request.get_json(force=True)["answer"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True})

    @app.post("/api/cases/<int:cid>/ack")
    def ack(cid):
        agent.acknowledge(db, cid)
        return jsonify({"ok": True})

    @app.post("/api/cases/<int:cid>/resolve")
    def resolve(cid):
        agent.resolve(db, cid, (request.get_json(silent=True) or {}).get("note", "Inspection completed"))
        return jsonify({"ok": True})

    @app.post("/api/tick")
    def tick():
        m = int((request.get_json(silent=True) or {}).get("minutes", 30))
        return jsonify({"fired": agent.tick(db, m), "clock_min": db.now_min})

    @app.post("/api/reset")
    def reset():
        db.reset()
        return jsonify({"ok": True})

    @app.get("/api/report")
    def report():
        lines = ["VOLTGUARD AI - INCIDENT REPORT", f"Demo clock: +{db.now_min} min", ""]
        for c in db.q("SELECT * FROM cases ORDER BY id"):
            circ = db.circuit(c["circuit_id"])
            lines += [f"Case {c['id']}: {circ['site']} / {circ['name']}",
                      f"  Severity {c['severity'].upper()} | status {c['status']} | confidence {c['confidence']:.0%}",
                      f"  {c['explanation']}"]
            wo = db.one("SELECT * FROM work_orders WHERE case_id=?", c["id"])
            if wo:
                lines.append(f"  Work order {wo['code']} [{wo['priority']}] {wo['status']}")
            for s in db.q("SELECT * FROM steps WHERE case_id=? ORDER BY id", c["id"]):
                lines.append(f"    +{s['t_min']}m {s['tool']}: {s['summary']}")
            lines.append("")
        lines.append("Decision-support only. Not a certified protection system.")
        return app.response_class("\n".join(lines), mimetype="text/plain")

    return app
