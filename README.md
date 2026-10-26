# VoltGuard AI - prototype

Electrical-safety triage agent for facility managers (buildings, labour accommodation, malls, small data rooms).
Simulation-first, decision-support only. It never operates equipment.

## Run
    pip install -r requirements.txt
    python run.py                  # open http://localhost:5000
    python -m unittest discover -s tests -v
    python evaluate.py --n 200     # labelled-scenario evaluation vs a fixed-threshold baseline

## 2-minute demo
1. Pick "Mall A / Chiller plant feeder" + "Slow thermal drift" -> Inject. The agent flags it before any fixed limit trips.
2. Read the evidence + chart (measured vs expected for THIS circuit). Answer the agent's question.
3. "Yes - I see or smell something" raises the case to CRITICAL and drafts an URGENT work order.
4. Press "Advance 30 min" without acknowledging -> the agent escalates (supervisor, then operations manager).
5. Inject a different scenario on another circuit -> triage re-ranks. Open "Report" for the audit trail.
Try "Heat-wave day" (no alert) and "Sensor fault" (sensor work order, not an electrical alarm).

## What is real vs. planned
Real in this repo: simulator, per-circuit learned baseline (ambient + load aware), rule/trend/pattern detection,
sensor-fault detection, confidence scores, the 7 tools, agent policy (ask -> rank -> draft work order -> follow-up ->
escalate), triage ranking, SQLite log, web dashboard, tests, evaluation harness.
Not done: real sensors, real data, an LLM that plans tool calls (planning is a deterministic policy; an LLM may only
reword the explanation if ANTHROPIC_API_KEY is set - untested), notifications (escalation is logged, nothing is sent),
authentication, multi-user. Thresholds are demo values and must be set by a licensed electrician.

## Layout
voltguard/sim.py (data + scenarios) | detect.py (engine) | tools.py (7 tools) | agent.py (policy, triage) | db.py | app.py (API)
static/index.html (dashboard) | assets/ (logo files) | evaluate.py | tests/
