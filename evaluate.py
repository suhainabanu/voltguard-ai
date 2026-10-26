"""Evaluate the VoltGuard detector against labelled simulated scenarios and a fixed-threshold baseline.

    python evaluate.py [--n 120] [--seed 1]

IMPORTANT: scenarios are synthetic. The numbers show how the method behaves on this simulator
(including some deliberately harder cases); they are NOT evidence of real-world accuracy.
Re-run on recorded sensor data before claiming anything beyond this.
"""
import argparse
import json
import random
from collections import defaultdict

from voltguard import detect, sim

HARD = ["mild_drift", "load_step", "noisy_normal", "dropout"]
ALL = sim.SCENARIOS + HARD
EXPECT = {**sim.EXPECTS_ALERT, "mild_drift": True, "load_step": False, "noisy_normal": False, "dropout": False}
SENSOR = {"sensor_fault", "dropout"}


def gen(circ, sc, start, rng):
    if sc in sim.SCENARIOS:
        return sim.scenario_readings(circ, sc, start, rng)
    out = []
    slope = rng.uniform(0.5, 0.9)
    for i in range(sim.FAULT_STEPS):
        h = start + i
        if sc == "mild_drift":
            r = sim.make_reading(h, circ, rng, temp_excess=slope * (i + 1))
        elif sc == "load_step":                      # perfectly healthy but heavily loaded
            r = sim.make_reading(h, circ, rng, frac_override=rng.uniform(0.9, 0.98))
        elif sc == "noisy_normal":
            r = sim.make_reading(h, circ, rng)
            r["temp"] = round(r["temp"] + rng.gauss(0, 2.2), 1)
        else:                                        # dropout
            r = sim.make_reading(h, circ, rng)
            if i >= sim.FAULT_STEPS - 4:
                r["temp"] = None
        out.append(r)
    return out


def fixed_first_flag(readings, circ, start):
    for i, r in enumerate(readings[start:], 1):
        if sim.fixed_rule_status(r, circ) != "normal":
            return i
    return None


def agent_first_flag(hist, new, circ):
    for k in range(1, len(new) + 1):
        a = detect.analyze(hist + new[:k], circ)
        if a["severity"] in ("caution", "critical"):
            return k
    return None


def run(n, seed):
    rng = random.Random(seed)
    stats = defaultdict(lambda: defaultdict(int))
    lead = []
    never_fixed = 0
    fault_n = 0
    for i in range(n):
        sc = ALL[i % len(ALL)]
        circ = {"profile": rng.choice(["mall", "camp", "datacenter", "residential"]),
                "rated_a": rng.choice([32, 40, 63, 100, 125]), "kf": rng.uniform(0.85, 1.25)}
        hist = sim.make_history(circ, rng)
        new = gen(circ, sc, len(hist), rng)
        a = detect.analyze(hist + new, circ)
        agent_alert = a["severity"] in ("caution", "critical")
        fixed_alert = sim.fixed_rule_status(new[-1], circ) != "normal"
        s = stats[sc]
        s["n"] += 1
        s["agent_alert"] += agent_alert
        s["fixed_alert"] += fixed_alert
        s["agent_sensor_flag"] += a["severity"] == "sensor_check"
        if EXPECT[sc]:
            fault_n += 1
            fa = agent_first_flag(hist, new, circ)
            ff = fixed_first_flag(new, circ, 0)
            if ff is None:
                never_fixed += 1
            if fa is not None and ff is not None:
                lead.append(ff - fa)
            elif fa is not None and ff is None:
                lead.append(None)
    return stats, lead, never_fixed, fault_n


def report(stats, lead, never_fixed, fault_n):
    rows = []
    tot = defaultdict(int)
    print(f"{'scenario':18} {'n':>4} {'expects':>10} {'agent alerts':>13} {'fixed-rule alerts':>18}")
    for sc in ALL:
        s = stats[sc]
        if not s["n"]:
            continue
        exp = "ALERT" if EXPECT[sc] else ("sensor chk" if sc in SENSOR else "no alert")
        print(f"{sc:18} {s['n']:4d} {exp:>10} {s['agent_alert']:>9d}/{s['n']:<3d} {s['fixed_alert']:>13d}/{s['n']:<3d}")
        key = "fault" if EXPECT[sc] else "ok"
        tot[key + "_n"] += s["n"]
        tot[key + "_agent"] += s["agent_alert"]
        tot[key + "_fixed"] += s["fixed_alert"]
        if sc in SENSOR:
            tot["sensor_n"] += s["n"]
            tot["sensor_caught"] += s["agent_sensor_flag"]
    pct = lambda a, b: (100.0 * a / b) if b else float("nan")
    out = {
        "scenarios": sum(stats[s]["n"] for s in ALL),
        "agent_detection_rate": pct(tot["fault_agent"], tot["fault_n"]),
        "fixed_detection_rate": pct(tot["fault_fixed"], tot["fault_n"]),
        "agent_false_alarm_rate": pct(tot["ok_agent"], tot["ok_n"]),
        "fixed_false_alarm_rate": pct(tot["ok_fixed"], tot["ok_n"]),
        "agent_missed_rate": 100 - pct(tot["fault_agent"], tot["fault_n"]),
        "fixed_missed_rate": 100 - pct(tot["fault_fixed"], tot["fault_n"]),
        "sensor_fault_identified": pct(tot["sensor_caught"], tot["sensor_n"]),
        "fault_scenarios_fixed_rule_never_fired": pct(never_fixed, fault_n),
    }
    measured = [x for x in lead if x is not None]
    out["mean_lead_hours_vs_fixed_when_both_fire"] = (sum(measured) / len(measured)) if measured else None
    print("\nNOTE: synthetic scenarios, detector tuned on the same simulator; fixed-rule baseline uses demo limits "
          "(65/80 C, rated current). Treat as a sanity check, not real-world accuracy.\n")
    for k, v in out.items():
        print(f"{k:45} {v if not isinstance(v, float) else round(v, 1)}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    res = report(*run(a.n, a.seed))
    json.dump(res, open("evaluation_results.json", "w"), indent=2)
