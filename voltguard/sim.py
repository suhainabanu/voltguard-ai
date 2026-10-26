"""Pure simulator: generates circuit history and injects labelled fault scenarios.

Physical model (simplified, for simulation only):
    temp = ambient + 6 + 20 * kf * (current / rated)^2 + noise
Readings are hourly. Values are illustrative, not real electrical limits.
"""
import math
import random

SCENARIOS = ["normal", "drift", "overload", "loose_connection", "sensor_fault", "heatwave"]
SCENARIO_LABELS = {
    "normal": "Normal operation",
    "drift": "Slow thermal drift",
    "overload": "Overload",
    "loose_connection": "Intermittent loose-connection pattern",
    "sensor_fault": "Sensor fault",
    "heatwave": "Heat-wave day (healthy circuit)",
}
# Ground truth used by the evaluation: does the scenario deserve an electrical alert?
EXPECTS_ALERT = {"normal": False, "heatwave": False, "sensor_fault": False,
                 "drift": True, "overload": True, "loose_connection": True}

HISTORY_HOURS = 24 * 30
FAULT_STEPS = 8          # readings appended by a scenario


def ambient_at(hour, summer=True, rng=None):
    base = 39.0 if summer else 26.0
    amp = 5.0
    v = base + amp * math.sin(2 * math.pi * ((hour % 24) - 9) / 24)
    if rng:
        v += rng.gauss(0, 0.4)
    return v


def load_fraction(hour, profile, rng):
    h = hour % 24
    if profile == "datacenter":
        f = 0.55 + 0.05 * math.sin(2 * math.pi * h / 24)
    elif profile == "camp":      # evening peak
        f = 0.35 + 0.4 * math.exp(-((h - 20) ** 2) / 18) + 0.1 * math.exp(-((h - 6) ** 2) / 8)
    elif profile == "mall":      # daytime peak
        f = 0.25 + 0.5 * math.exp(-((h - 15) ** 2) / 30)
    else:                        # residential
        f = 0.3 + 0.35 * math.exp(-((h - 19) ** 2) / 14) + 0.15 * math.exp(-((h - 7) ** 2) / 6)
    return max(0.05, min(1.1, f + rng.gauss(0, 0.03)))


def temp_model(ambient, frac, kf):
    return ambient + 6.0 + 20.0 * kf * frac ** 2


def make_reading(hour, circ, rng, ambient_shift=0.0, frac_override=None, temp_excess=0.0, summer=True):
    amb = ambient_at(hour, summer, rng) + ambient_shift
    frac = frac_override if frac_override is not None else load_fraction(hour, circ["profile"], rng)
    cur = circ["rated_a"] * frac + rng.gauss(0, 0.05 * circ["rated_a"] * 0.1)
    temp = temp_model(amb, frac, circ["kf"]) + temp_excess + rng.gauss(0, 0.7)
    volt = 230 + rng.gauss(0, 1.8)
    return {"hour": hour, "voltage": round(volt, 1), "current": round(cur, 2),
            "temp": round(temp, 1), "ambient": round(amb, 1)}


def make_history(circ, rng, hours=HISTORY_HOURS, summer=True):
    return [make_reading(h, circ, rng, summer=summer) for h in range(hours)]


def scenario_readings(circ, scenario, start_hour, rng, summer=True, steps=FAULT_STEPS):
    """Return `steps` new readings (one per hour) following `scenario`."""
    out = []
    slope = rng.uniform(1.2, 2.0)                      # drift °C per hour
    over_end = rng.uniform(1.18, 1.3)                  # overload end ratio
    stuck_val = None
    for i in range(steps):
        h = start_hour + i
        if scenario == "normal":
            r = make_reading(h, circ, rng, summer=summer)
        elif scenario == "heatwave":
            r = make_reading(h, circ, rng, ambient_shift=rng.uniform(6.0, 8.0), summer=summer)
        elif scenario == "drift":
            r = make_reading(h, circ, rng, temp_excess=slope * (i + 1), summer=summer)
        elif scenario == "overload":
            frac = 0.75 + (over_end - 0.75) * (i + 1) / steps
            r = make_reading(h, circ, rng, frac_override=frac, summer=summer)
        elif scenario == "loose_connection":
            spike = (i % 2 == 1)
            r = make_reading(h, circ, rng, temp_excess=rng.uniform(10, 16) if spike else rng.uniform(0, 2), summer=summer)
            if spike:
                r["voltage"] = round(r["voltage"] - rng.uniform(6, 12), 1)
        elif scenario == "sensor_fault":
            r = make_reading(h, circ, rng, summer=summer)
            if i < steps - 6:
                pass
            else:
                if stuck_val is None:
                    stuck_val = r["temp"]
                r["temp"] = stuck_val                     # temperature probe flat-lines
        else:
            raise ValueError("unknown scenario " + scenario)
        out.append(r)
    return out


def fixed_rule_status(reading, circ):
    """Naive fixed-threshold monitor used as the comparison baseline."""
    if reading.get("temp") is None:
        return "normal"
    t, c = reading["temp"], reading["current"]
    if t >= circ.get("temp_critical", 80) or c > 1.15 * circ["rated_a"]:
        return "critical"
    if t >= circ.get("temp_caution", 65) or c > circ["rated_a"]:
        return "caution"
    return "normal"
