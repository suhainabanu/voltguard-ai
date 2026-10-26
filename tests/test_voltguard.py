import random
import sys
import unittest

sys.path.insert(0, ".")
from voltguard import agent, detect, sim, tools          # noqa: E402
from voltguard.app import create_app                     # noqa: E402
from voltguard.db import DB                              # noqa: E402

CIRC = {"profile": "mall", "rated_a": 63, "kf": 1.0}


def analyze(sc, seed=3):
    rng = random.Random(seed)
    h = sim.make_history(CIRC, rng)
    return detect.analyze(h + sim.scenario_readings(CIRC, sc, len(h), random.Random(seed + 1)), CIRC)


class Detection(unittest.TestCase):
    def test_normal_and_heatwave_do_not_alert(self):
        for sc in ("normal", "heatwave"):
            self.assertEqual(analyze(sc)["severity"], "normal", sc)

    def test_faults_alert(self):
        for sc in ("drift", "overload", "loose_connection"):
            self.assertIn(analyze(sc)["severity"], ("caution", "critical"), sc)

    def test_sensor_fault_is_not_an_electrical_alert(self):
        a = analyze("sensor_fault")
        self.assertEqual(a["severity"], "sensor_check")
        self.assertEqual(a["pattern"], "sensor_fault")

    def test_drift_is_not_misread_as_intermittent(self):
        self.assertNotEqual(analyze("drift")["pattern"], "intermittent_heating")


class AgentFlow(unittest.TestCase):
    def setUp(self):
        self.db = DB()
        self.db.reset()
        self.cid = 7

    def inject(self, sc):
        c = self.db.circuit(self.cid)
        self.db.append_readings(self.cid, sim.scenario_readings(c, sc, self.db.readings(self.cid)[-1]["hour"] + 1, random.Random(4)))
        return agent.process(self.db, self.cid)

    def test_normal_creates_no_case(self):
        self.assertEqual(self.inject("normal")["status"], "normal")
        self.assertEqual(self.db.q("SELECT * FROM cases"), [])

    def test_caution_asks_then_drafts_work_order(self):
        r = self.inject("loose_connection")
        case = self.db.one("SELECT * FROM cases WHERE id=?", r["case_id"])
        self.assertEqual(case["status"], "awaiting_user")
        self.assertIsNone(self.db.one("SELECT * FROM work_orders"))
        agent.answer(self.db, case["id"], "none")
        wo = self.db.one("SELECT * FROM work_orders")
        self.assertTrue(wo["status"].startswith("draft"))             # never auto-sent
        self.assertIn("DRAFT", wo["body"])

    def test_danger_answer_raises_to_critical(self):
        r = self.inject("loose_connection")
        agent.answer(self.db, r["case_id"], "danger")
        self.assertEqual(self.db.one("SELECT severity FROM cases")["severity"], "critical")

    def test_answer_never_lowers_severity(self):
        r = self.inject("overload")
        before = self.db.one("SELECT severity FROM cases")["severity"]
        agent.answer(self.db, r["case_id"], "none")
        self.assertEqual(self.db.one("SELECT severity FROM cases")["severity"], before)

    def test_unacknowledged_alert_escalates(self):
        r = self.inject("overload")
        agent.tick(self.db, 31)
        self.assertGreaterEqual(self.db.one("SELECT escalation FROM cases WHERE id=?", r["case_id"])["escalation"], 1)

    def test_no_answer_proceeds_conservatively(self):
        r = self.inject("loose_connection")
        agent.tick(self.db, 20)
        c = self.db.one("SELECT * FROM cases WHERE id=?", r["case_id"])
        self.assertEqual(c["answer"], "unsure")
        self.assertIsNotNone(self.db.one("SELECT * FROM work_orders"))

    def test_sensor_fault_gets_low_priority_sensor_work_order(self):
        self.inject("sensor_fault")
        wo = self.db.one("SELECT * FROM work_orders")
        self.assertEqual(wo["priority"], "LOW")
        self.assertIn("sensor", wo["title"].lower())

    def test_triage_ranks_critical_first(self):
        self.inject("overload")
        c2 = self.db.circuit(2)
        self.db.append_readings(2, sim.scenario_readings(c2, "sensor_fault", self.db.readings(2)[-1]["hour"] + 1, random.Random(1)))
        agent.process(self.db, 2)
        t = agent.triage(self.db)
        self.assertEqual(t[0]["circuit_id"], self.cid)
        self.assertEqual(t[0]["rank"], 1)

    def test_guidance_never_tells_users_to_touch_equipment(self):
        for g in agent.GUIDANCE.values():
            self.assertIn("not open panels" if "open panels" in g else "", g)
            self.assertNotIn("reset the breaker", g.lower())

    def test_tools_are_read_or_draft_only(self):
        self.assertEqual(set(tools.TOOLS), {"get_readings", "get_history", "check_rules", "trend_analysis",
                                            "search_maintenance_log", "ask_user", "create_work_order", "schedule_followup"})


class Api(unittest.TestCase):
    def test_end_to_end(self):
        app = create_app()
        c = app.test_client()
        self.assertEqual(c.get("/api/state").status_code, 200)
        r = c.post("/api/inject", json={"circuit_id": 7, "scenario": "overload", "seed": 5}).get_json()
        cid = r["case_id"]
        d = c.get(f"/api/cases/{cid}").get_json()
        self.assertTrue(d["steps"] and d["series"])
        self.assertEqual(c.post(f"/api/cases/{cid}/answer", json={"answer": "bogus"}).status_code, 400)
        self.assertEqual(c.post(f"/api/cases/{cid}/answer", json={"answer": "none"}).status_code, 200)
        c.post(f"/api/cases/{cid}/ack")
        c.post(f"/api/cases/{cid}/resolve")
        self.assertIn("VOLTGUARD", c.get("/api/report").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
