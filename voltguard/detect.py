"""Detection engine: validation, per-circuit baseline, trend and severity.

Pure functions (no database) so they can be unit-tested and evaluated.
All thresholds are DEMO values; real limits must be set by a licensed electrician.
"""
import math
import numpy as np

SEV_ORDER = {"normal": 0, "sensor_check": 1, "caution": 2, "critical": 3}
WINDOW = 6          # readings analysed for the current state
HOLDOUT = 12        # newest readings excluded when fitting the baseline

DEFAULTS = {
    "temp_caution": 65.0, "temp_critical": 80.0,       # fixed demo limits (°C)
    "excess_caution": 5.0, "excess_critical": 12.0,    # °C above learned baseline
    "overload_caution": 1.0, "overload_critical": 1.15,  # x rated current
    "volt_low": 207.0, "volt_high": 253.0,
}


def _features(r):
    return [r["ambient"], (r["current"]) ** 2, 1.0]


def fit_baseline(history, rated_a):
    """Robust least squares: temp ~ a*ambient + b*(I/rated)^2 + c. Returns coef, resid_std, r2."""
    rows = [r for r in history if r.get("temp") is not None]
    X = np.array([[r["ambient"], (r["current"] / rated_a) ** 2, 1.0] for r in rows])
    y = np.array([r["temp"] for r in rows])
    keep = np.ones(len(y), bool)
    for _ in range(3):
        coef, *_ = np.linalg.lstsq(X[keep], y[keep], rcond=None)
        res = y - X @ coef
        sd = res[keep].std() or 1e-6
        keep = np.abs(res) < 3 * sd
    ss_res = float(((y[keep] - X[keep] @ coef) ** 2).sum())
    ss_tot = float(((y[keep] - y[keep].mean()) ** 2).sum()) or 1e-6
    return coef, float(sd), 1 - ss_res / ss_tot


def expected_temp(coef, reading, rated_a):
    return float(coef[0] * reading["ambient"] + coef[1] * (reading["current"] / rated_a) ** 2 + coef[2])


def sensor_quality(window):
    issues = []
    temps = [r.get("temp") for r in window]
    if sum(t is None for t in temps) >= 2:
        issues.append("temperature readings missing")
    vals = [t for t in temps if t is not None]
    if any(t < -10 or t > 150 for t in vals):
        issues.append("temperature outside physical range")
    if len(vals) >= 5 and max(vals) - min(vals) == 0:
        issues.append("temperature value stuck (flat-lined)")
    if any(r["voltage"] <= 0 for r in window if r.get("voltage") is not None):
        issues.append("voltage reads zero while current flows")
    return issues


def analyze(readings, circ, cfg=None):
    """Analyse the newest readings of a circuit against its own history.

    Returns a dict with severity, confidence, pattern, evidence and the numbers behind them.
    """
    cfg = {**DEFAULTS, **(cfg or {})}
    rated = circ["rated_a"]
    window = readings[-WINDOW:]
    history = readings[:-HOLDOUT] if len(readings) > HOLDOUT + 48 else readings[:-WINDOW]
    evidence, rules_hit = [], []
    result = {"severity": "normal", "confidence": 0.0, "pattern": "normal", "evidence": evidence,
              "rules_hit": rules_hit, "excess": 0.0, "slope": 0.0, "expected": None,
              "fixed_status": "normal", "sensor_issues": [], "overload_ratio": 0.0, "fit_r2": 0.0}

    # 1) validate the data before trusting it
    issues = sensor_quality(window)
    result["sensor_issues"] = issues
    if len(history) < 48:
        result.update(severity="sensor_check", pattern="insufficient_data", confidence=0.3)
        evidence.append("Not enough history to learn this circuit's baseline yet.")
        return result
    if issues:
        result.update(severity="sensor_check", pattern="sensor_fault", confidence=0.8)
        evidence.extend(i[0].upper() + i[1:] + "." for i in issues)
        evidence.append("The data may not reflect a real electrical condition; the sensor should be checked first.")
        return result

    # 2) learn the circuit's normal and measure deviation
    coef, sd, r2 = fit_baseline(history, rated)
    result["fit_r2"] = round(r2, 3)
    resid = [r["temp"] - expected_temp(coef, r, rated) for r in window]
    excess_last3 = float(np.mean(resid[-3:]))
    result["excess"] = round(excess_last3, 1)
    result["expected"] = round(expected_temp(coef, window[-1], rated), 1)
    result["slope"] = round(float(np.polyfit(range(len(resid)), resid, 1)[0]), 2)

    # 3) fixed (electrician-set) rules
    last = window[-1]
    ratio = last["current"] / rated
    result["overload_ratio"] = round(ratio, 2)
    if last["temp"] >= cfg["temp_critical"]:
        rules_hit.append(("critical", f"Temperature {last['temp']}°C is at or above the critical limit ({cfg['temp_critical']}°C)."))
    elif last["temp"] >= cfg["temp_caution"]:
        rules_hit.append(("caution", f"Temperature {last['temp']}°C is above the caution limit ({cfg['temp_caution']}°C)."))
    if ratio > cfg["overload_critical"]:
        rules_hit.append(("critical", f"Current is {ratio:.0%} of rated current."))
    elif ratio > cfg["overload_caution"]:
        rules_hit.append(("caution", f"Current is {ratio:.0%} of rated current."))
    if last["voltage"] < cfg["volt_low"] or last["voltage"] > cfg["volt_high"]:
        rules_hit.append(("caution", f"Voltage {last['voltage']} V is outside {cfg['volt_low']:.0f}-{cfg['volt_high']:.0f} V."))
    result["fixed_status"] = max((s for s, _ in rules_hit), key=lambda s: SEV_ORDER[s], default="normal")

    # 4) pattern classification
    spikes = sum(1 for e in resid if e >= 8.0)
    falls = sum(1 for i in range(len(resid) - 1) if resid[i] >= 8.0 and resid[i + 1] < 4.0)
    intermittent = spikes >= 2 and falls >= 1          # spikes that fall back, unlike a steady drift
    sev = "normal"
    if intermittent:
        sev, pattern = "caution", "intermittent_heating"
        evidence.append(f"{spikes} of the last {WINDOW} readings spiked 8°C+ above baseline and fell back, "
                        "a pattern sometimes seen with loose or degrading connections.")
        if max(resid) >= cfg["excess_critical"] + 2:
            sev = "caution"
    elif excess_last3 >= cfg["excess_critical"] or result["fixed_status"] == "critical":
        sev, pattern = "critical", ("overload" if ratio > cfg["overload_critical"] else "overheating")
    elif (excess_last3 >= cfg["excess_caution"] or (result["slope"] >= 1.0 and resid[-1] >= 4.0)
          or result["fixed_status"] == "caution"):
        sev = "caution"
        pattern = "overload" if ratio > cfg["overload_caution"] else "thermal_drift"
    else:
        pattern = "normal"
    result["pattern"] = pattern

    if sev != "normal":
        if pattern in ("thermal_drift", "overheating", "overload") and result["expected"] is not None:
            evidence.append(f"Temperature is {last['temp']}°C against an expected {result['expected']}°C for this circuit at "
                            f"{ratio:.0%} load and {last['ambient']}°C ambient (about {excess_last3:+.1f}°C, trend {result['slope']:+.1f}°C per reading).")
        for _, msg in rules_hit:
            evidence.append(msg)
    elif last["ambient"] - np.mean([r["ambient"] for r in history[-24 * 7:]]) > 4:
        evidence.append("Ambient temperature is unusually high; readings are normal for this heat.")

    # 5) confidence: how well the baseline fits, how many readings agree, data quality
    agree = sum(1 for e in resid if e >= cfg["excess_caution"]) / len(resid) if sev != "normal" else 1.0
    conf = 0.45 + 0.3 * max(0.0, min(1.0, r2)) + 0.25 * agree
    result["confidence"] = round(min(0.97, conf), 2)
    result["severity"] = sev
    return result
