"""Builds tests/fixtures/history/reactions.json (run from the repository root): synthetic series with a known beta
of 2 and known own moves after each case. Not a test: the reference data of test_cases.py."""
import json
from datetime import date, timedelta

BETA = 2.0
def business_days(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out

days = business_days(date(2023, 1, 2), date(2027, 12, 31))
cases = [  # id, day, status, own extra per session for 20 sessions, factor rise per session in the window
    {"key": "A", "day": date(2024, 6, 3), "status": "verified", "extra": 0.005, "rise": 0.0025},
    {"key": "B", "day": date(2024, 6, 10), "status": "verified", "extra": 0.0, "rise": 0.0},
    {"key": "C", "day": date(2025, 8, 4), "status": "verified", "extra": -0.002, "rise": 0.003},
    {"key": "D", "day": date(2025, 12, 1), "status": "draft", "extra": 0.01, "rise": 0.0},
    {"key": "E", "day": date(2027, 3, 1), "status": "verified", "extra": 0.003, "rise": -0.002},
    {"key": "R", "day": date(2025, 3, 3), "status": "rejected", "extra": 0.0, "rise": 0.0},
]
window = {}
for c in cases:
    i0 = next(i for i, d in enumerate(days) if d >= c["day"])  # first session on or after the report
    for k in range(i0, i0 + 20):
        window[k] = c
brent, mat = [80.0], [100.0]
for i in range(1, len(days)):
    c = window.get(i)
    f = c["rise"] if c else (0.01 if i % 2 else -0.01)
    e = c["extra"] if c else 0.0
    brent.append(brent[-1] * (1 + f))
    mat.append(mat[-1] * (1 + BETA * f + e))
expected = {}
for c in cases:
    base_i = max(i for i, d in enumerate(days) if d < c["day"])
    out = {}
    for h in (1, 5, 20, 60):
        raw = 100 * (mat[base_i + h] / mat[base_i] - 1)
        fac = 100 * (brent[base_i + h] / brent[base_i] - 1)
        out[str(h)] = {"raw": round(raw, 2), "own": round(raw - BETA * fac, 2)}
    expected[c["key"]] = out
clean = sorted(expected[k]["20"]["own"] for k in ("C", "E"))
data = {
    "_comment": "Synthetic, built by a script: material daily returns = 2 x Brent returns, plus a known own move "
                "during the 20 sessions after each case. A and B are 5 business days apart (overlap); D is a draft; "
                "R is rejected.",
    "beta": BETA,
    "material_series": [[d.isoformat(), round(v, 6)] for d, v in zip(days, mat, strict=True)],
    "brent_series": [[d.isoformat(), round(v, 6)] for d, v in zip(days, brent, strict=True)],
    "cases": [{"key": c["key"], "first_report_date": c["day"].isoformat(), "status": c["status"]} for c in cases],
    "expected": expected,
    "expected_median_20": {"n": 2, "median": round(sum(clean) / 2, 2),
                           "low": round(clean[0] + 0.1 * (clean[1] - clean[0]), 2),
                           "high": round(clean[0] + 0.9 * (clean[1] - clean[0]), 2),
                           "excluded_overlap": 2},
}
with open("tests/fixtures/history/reactions.json", "w") as f:
    json.dump(data, f, indent=0)
print(len(days), {k: v["20"] for k, v in expected.items()}, data["expected_median_20"])
