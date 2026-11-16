"""
test_forecast.py — Predictive risk model (offline, synthetic city, no network).

Covers: hotspots and their peak hours are learned, quiet places stay low, probabilities are
calibrated against the simulated rate, the near-repeat effect, zone and night priors, the
"still learning" state, the area forecast, backtest accuracy beating chance, the proactive
warning, safety-score and safe-route integration, and speed on a large dataset.

Run:  python tests/test_forecast.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make `travel_buddy` importable

import random
import sys
import time
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from travel_buddy.safety import forecast as F
from travel_buddy.safety.monitor import compute_safety_score
from travel_buddy.safety.risk_intel import assess_route

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)   # 17:00 local in Jaipur (solar time)
LNG = 75.82
MARKET = (26.9239, 75.8267)      # evening theft hotspot
FORT = (26.9124, 75.7873)        # afternoon scam hotspot
QUIET = (26.8800, 75.7500)


def at_local(hour: float, days_ago: int) -> str:
    t = (NOW - timedelta(days=days_ago)).replace(hour=0, minute=0) + timedelta(hours=hour - LNG / 15)
    return t.isoformat()


def synthetic_city(seed: int = 7, days: int = 180) -> list[dict]:
    rnd = random.Random(seed)
    incs, iid = [], 0
    spots = [(MARKET, 0.25, [20, 21, 22], "theft", "medium"), (FORT, 0.12, [13, 14, 15], "scam", "low")]
    for day in range(1, days):
        for (lat, lng), p, hours, kind, sev in spots:
            if rnd.random() < p:
                iid += 1
                incs.append({"incident_id": iid, "lat": lat + rnd.gauss(0, 0.0018), "lng": lng + rnd.gauss(0, 0.0018),
                             "created_at": at_local(rnd.choice(hours) + rnd.uniform(0, 0.9), day),
                             "severity": sev, "incident_type": kind})
        if rnd.random() < 0.3:  # city-wide background noise
            iid += 1
            incs.append({"incident_id": iid, "lat": 26.85 + rnd.random() * 0.15, "lng": 75.72 + rnd.random() * 0.18,
                         "created_at": at_local(rnd.randrange(24), day), "severity": "low", "incident_type": "other"})
    return incs


def main():
    print("=" * 68)
    print(">>> PREDICTIVE RISK MODEL TESTS")
    print("=" * 68)
    incs = synthetic_city()
    model = F.RiskModel(incs, NOW)

    # 1. Learns where: hotspots high, quiet places low
    market, fort, quiet = (model.predict(*p) for p in (MARKET, FORT, QUIET))
    assert market["peak"]["index"] >= 60 and quiet["peak"]["index"] <= 15, (market["peak"], quiet["peak"])
    assert market["p_24h"] > 5 * max(quiet["p_24h"], 0.01)
    assert market["confidence"] == "high" and quiet["confidence"] == "low"
    print(f"[OK] hotspots learned: market peak {market['peak']['index']}/100 vs quiet {quiet['peak']['index']}/100")

    # 2. Learns when: market peaks in the evening, fort in the afternoon
    assert 19 <= market["peak"]["hour"] <= 23, market["peak"]
    fort_full = model.predict(*FORT, hours=24)
    fort_peak = max(fort_full["hourly"], key=lambda h: h["data_index"])["hour"]
    assert 12 <= fort_peak <= 16, fort_peak
    assert any("peak around" in d for d in market["drivers"])
    print(f"[OK] peak hours learned: market {market['peak']['label']}, fort {fort_peak:02d}:00")

    # 3. Calibrated: simulated market rate ≈ 0.25/day → P(≥1 in 24 h within 500 m) ≈ 1 − e^−0.25… (+ noise)
    assert 0.15 <= market["p_24h"] <= 0.5, market["p_24h"]
    print(f"[OK] calibrated probability: {round(market['p_24h'] * 100)}% in 24 h at the market (simulated hotspot ≈ 22% + background)")

    # 4. Near-repeat: a fresh incident raises risk around it for a few days, then fades
    fresh = [{"incident_id": 1, "lat": QUIET[0], "lng": QUIET[1], "created_at": (NOW - timedelta(hours=6)).isoformat(),
              "severity": "high", "incident_type": "harassment"}]
    old = [{**fresh[0], "created_at": (NOW - timedelta(days=40)).isoformat()}]
    filler = [{"incident_id": 100 + i, "lat": 27.5, "lng": 76.5 + i * 0.01, "created_at": (NOW - timedelta(days=20)).isoformat(),
               "severity": "low", "incident_type": "other"} for i in range(6)]
    r_fresh = F.RiskModel(fresh + filler, NOW).daily_rate(*QUIET)[0]
    r_old = F.RiskModel(old + filler, NOW).daily_rate(*QUIET)[0]
    assert r_fresh > 3 * r_old, (r_fresh, r_old)
    assert any("Near-repeat" in d for d in F.RiskModel(fresh + filler, NOW).predict(*QUIET)["drivers"])
    print(f"[OK] near-repeat effect: fresh report raises local rate {r_fresh / r_old:.1f}× vs a 40-day-old one")

    # 5. Priors: no data → still learning, zones and night still count
    empty = F.RiskModel([], NOW)
    e = empty.predict(*QUIET)
    assert e["confidence"] == "learning" and e["p_6h"] == 0 and e["data_index"] == 0
    zone = {"zone_id": 1, "name": "Test", "center_lat": QUIET[0], "center_lng": QUIET[1], "radius_m": 1000,
            "risk_level": "high", "advisory": ""}
    z = empty.predict(*QUIET, zones=[zone])
    assert z["index"] >= 35 and any("high-risk zone" in d for d in z["drivers"])
    night = max(e["hourly"], key=lambda h: h["index"])
    assert night["hour"] >= 22 or night["hour"] < 5
    print(f"[OK] empty model: 'learning', zone prior {z['index']}/100, night prior {night['index']}/100")

    # 6. Area forecast: next 6 h (17:00–23:00 local) → the evening market, not the afternoon fort
    spots = model.predicted_hotspots(hours=6)
    assert spots, "expected predicted hotspots"
    from travel_buddy.core.tracker import haversine_distance_meters as dist
    assert dist(spots[0]["lat"], spots[0]["lng"], *MARKET) < 700, spots[0]
    assert all(dist(s["lat"], s["lng"], *FORT) > 600 for s in spots if s["index"] >= 40), spots
    morning = model.predicted_hotspots(hours=6, now=NOW.replace(hour=7))  # 12:00–18:00 local
    assert any(dist(s["lat"], s["lng"], *FORT) < 700 for s in morning), morning
    print(f"[OK] area forecast follows the clock: evening → market ({spots[0]['index']}), afternoon → fort")

    # 7. Backtest: beats chance by a wide margin, time-of-day skill > 1
    bt = F.backtest(incs, NOW)
    assert bt["ready"] and bt["pai"] >= 3 and bt["hit_rate"] >= 0.4 and bt["hour_lift"] > 1.5, bt
    assert not F.backtest(incs[:5], NOW)["ready"]
    print(f"[OK] backtest: top 10% of area caught {round(bt['hit_rate'] * 100)}% of new incidents "
          f"(PAI {bt['pai']}, baseline {bt['baseline_pai']}), hour skill {bt['hour_lift']}×")

    # 8. Proactive warning
    evening = model.predict(*MARKET, hours=4, now=NOW.replace(hour=15))   # 20:00 local
    assert F.proactive_warning(evening) and "Predicted risk" in F.proactive_warning(evening)
    before = model.predict(*MARKET, hours=4, now=NOW.replace(hour=13))   # 18:00 local, rising soon
    w = F.proactive_warning(before)
    assert w and ("rising" in w or "Predicted risk" in w), w
    assert F.proactive_warning(model.predict(*QUIET, hours=4)) is None
    assert F.proactive_warning(empty.predict(*QUIET, zones=[zone], hours=4)) is None  # zones have their own alerts
    print("[OK] proactive warnings: now-elevated and rising-soon detected, quiet/no-data stay silent")

    # 9. Integration: safety score takes the stronger of hotspot vs forecast (no double count)
    s1 = compute_safety_score(hotspot_risk="caution", forecast_level="high")
    s2 = compute_safety_score(hotspot_risk="high", forecast_level="high")
    assert s1["score"] == 85 and s2["score"] == 85, (s1, s2)
    assert any("predicted" in f for f in s1["factors"]) and compute_safety_score(forecast_level=None)["score"] == 100
    print("[OK] safety score uses the stronger of learned-hotspot and forecast risk")

    # 10. Integration: safe-route penalises a route through predicted evening risk
    path = [(MARKET[0] - 0.01, MARKET[1]), (MARKET[0] + 0.01, MARKET[1])]
    m_eve = F.RiskModel(incs, NOW.replace(hour=15))
    with_model = assess_route(path, [], [], local_hour=20, model=m_eve)
    without = assess_route(path, [], [], local_hour=20)
    assert with_model["score"] < without["score"] and with_model["predicted"], (with_model, without)
    print(f"[OK] safe-route: {without['score']} → {with_model['score']} with the model ({with_model['reasons'][-1][:60]}…)")

    # 11. Text output renders
    html = F.report_html(market, "Johari Bazaar")
    assert "Risk forecast" in html and "Safest window" in html and "Why:" in html
    assert "now" in F.summary_line(market)
    print("[OK] Telegram report and copilot summary render")

    # 12. Speed on a large dataset (well beyond a pilot deployment)
    big = [inc for seed in range(20) for inc in synthetic_city(seed=seed, days=180)]
    big = [{**inc, "incident_id": n + 1} for n, inc in enumerate(big)]
    t = time.perf_counter()
    bm = F.RiskModel(big, NOW)
    fit_ms = (time.perf_counter() - t) * 1000
    t = time.perf_counter()
    for _ in range(50):
        bm.predict(*MARKET)
    pred_ms = (time.perf_counter() - t) * 1000 / 50
    t = time.perf_counter()
    bm.predicted_hotspots(hours=6)
    area_ms = (time.perf_counter() - t) * 1000
    assert fit_ms < 500 and pred_ms < 50 and area_ms < 3000, (fit_ms, pred_ms, area_ms)
    print(f"[OK] speed with {len(big)} incidents: fit {fit_ms:.0f} ms, predict {pred_ms:.1f} ms, area {area_ms:.0f} ms")

    print("\nALL FORECAST TESTS PASSED")


if __name__ == "__main__":
    main()
