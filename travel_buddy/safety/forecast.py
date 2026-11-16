"""
forecast.py — Predictive risk model: where and when incidents are likely next.

A spatio-temporal incident-forecasting model of the kind used for predictive policing / crime
hotspot forecasting, sized for a single small server: it fits in milliseconds on thousands of
reports, needs no ML libraries, and retrains itself automatically as new incidents arrive.

    rate(x, h) = Σ_i  K(x, x_i) · [ w_long(age_i) + β · w_short(age_i) ]  ·  P(h | x)
                 └──────── expected incidents per day near x ────────┘   └ hour share ┘

  K(x, x_i)  Gaussian spatial kernel (σ = 300 m): how far one incident spreads risk.
  w_long     Background rate. Exponential memory (τ = 45 days), normalised so Σ w_long ≈
             incidents per day: an area with 1 report every 2 weeks predicts ≈ 0.07 / day.
  w_short    Near-repeat effect (self-exciting, Hawkes-style). Incidents cluster in space and
             time, so a fresh report raises risk nearby for a few days (τ = 3 days, β = 0.3).
  P(h | x)   Hour-of-day profile. The local, kernel-weighted hour histogram is shrunk (empirical
             Bayes) toward the city-wide profile, which itself is shrunk toward a tourist-safety
             prior (evenings and nights riskier). This keeps sparse areas from overfitting.

Outputs per location
  - probability of ≥ 1 reported incident within ~500 m in the next 6 / 24 hours (Poisson)
  - a 0–100 risk index per hour for the next 24 h. It blends the data with zone advisories and
    time of night: index = 1 − (1 − c_data)(1 − c_zone)(1 − c_time)
  - drivers (plain-language reasons), peak and safest hours, and a confidence level

Self-evaluation (backtest)
  Trains on everything before a cut-off, forecasts the following 14 days, and scores the top 10%
  of the area. It reports the hit rate and the Predictive Accuracy Index (PAI = hit rate / area
  share; 1 = no better than chance), compared with a naive "count past incidents" baseline.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from travel_buddy.core.tracker import haversine_distance_meters
from travel_buddy.safety.geofence import RISK_ORDER, distance_to_zone_edge
from travel_buddy.safety.monitor import approx_local_hour

# ── model hyper-parameters ──────────────────────────────────────────────────
SIGMA_M = 300.0              # spatial kernel bandwidth
CUTOFF_M = 3 * SIGMA_M       # ignore incidents further than this (kernel < 1.2%)
TAU_LONG_D = 45.0            # background memory
TAU_SHORT_D = 3.0            # near-repeat memory
BETA_SHORT = 0.3             # strength of the near-repeat boost
HISTORY_DAYS = 180           # training window
PRIOR_STRENGTH_CITY = 12.0   # pseudo-counts pulling the city profile toward the prior
PRIOR_STRENGTH_LOCAL = 4.0   # pseudo-counts pulling a local profile toward the city profile
HARM_SCALE = 16.6            # 1 medium incident / day nearby at an average hour → index 50
NEAR_M = 500.0               # radius used for "probability of an incident near you"
MIN_EVENTS = 5               # below this the model says it is still learning

SEVERITY = {"low": 0.5, "medium": 1.0, "high": 1.6, "critical": 2.2}
ZONE_PRIOR = {"caution": 0.15, "high": 0.35, "restricted": 0.5}
LEVELS = [(60, "high", "🔴"), (40, "elevated", "🟠"), (20, "moderate", "🟡"), (0, "low", "🟢")]
TYPE_LABEL = {"sos": "SOS", "geofence_breach": "zone breach", "no_response": "missed check-in"}

# Tourist-safety prior for the time of day (relative weights, normalised below).
_PRIOR_RAW = [1.6, 1.6, 1.6, 1.5, 1.3, 0.8, 0.7, 0.7, 0.8, 0.9, 0.9, 0.9,
              0.9, 0.9, 0.9, 0.9, 1.0, 1.1, 1.3, 1.3, 1.4, 1.4, 1.6, 1.6]
PRIOR_HOURS = [w / sum(_PRIOR_RAW) for w in _PRIOR_RAW]


def time_prior(hour: int) -> float:
    """Extra risk from the time of day alone (used even with no incident data)."""
    if hour >= 22 or hour < 5:
        return 0.10
    if hour >= 18:
        return 0.04
    return 0.0


def level_for(index: float) -> tuple[str, str]:
    for threshold, name, emoji in LEVELS:
        if index >= threshold:
            return name, emoji
    return "low", "🟢"


def _smooth(hist: list[float]) -> list[float]:
    """Circular [¼, ½, ¼] smoothing: a 21:00 report also says something about 20:00 and 22:00."""
    return [0.25 * hist[h - 1] + 0.5 * hist[h] + 0.25 * hist[(h + 1) % 24] for h in range(24)]


def _parse_time(iso: str | None) -> datetime | None:
    try:
        t = datetime.fromisoformat(iso)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fmt_hour(h: int) -> str:
    return f"{h % 24:02d}:00"


@dataclass(slots=True)
class Event:
    lat: float
    lng: float
    age_d: float
    hour: int
    sev: float
    kind: str
    incident_id: int


class RiskModel:
    """Fitted forecaster. Build with RiskModel(incidents, now); query with predict()."""

    def __init__(self, incidents: list[dict], now: datetime | None = None):
        self.now = now or datetime.now(timezone.utc)
        self.events: list[Event] = []
        for inc in incidents:
            if inc.get("lat") is None or inc.get("lng") is None:
                continue
            t = _parse_time(inc.get("created_at"))
            if t is None or t > self.now:
                continue
            age = (self.now - t).total_seconds() / 86400
            if age > HISTORY_DAYS:
                continue
            self.events.append(Event(
                float(inc["lat"]), float(inc["lng"]), age, int(approx_local_hour(inc["lng"], t)),
                SEVERITY.get(inc.get("severity"), 1.0), inc.get("incident_type") or "other",
                int(inc.get("incident_id") or 0)))
        self._build_index()
        self._fit_city_profile()

    # ── spatial index: uniform grid, one shared east-west scale (no neighbour can be missed) ──
    def _build_index(self) -> None:
        self.max_abs_lat = max((abs(e.lat) for e in self.events), default=0.0)
        self.cell_lat = CUTOFF_M / 111_320.0
        self.cell_lng = CUTOFF_M / (111_320.0 * max(0.01, math.cos(math.radians(min(89.9, self.max_abs_lat)))))
        self.grid: dict[tuple[int, int], list[Event]] = {}
        for e in self.events:
            self.grid.setdefault(self._key(e.lat, e.lng), []).append(e)

    def _key(self, lat: float, lng: float) -> tuple[int, int]:
        return int(math.floor(lat / self.cell_lat)), int(math.floor(lng / self.cell_lng))

    def _nearby(self, lat: float, lng: float) -> list[tuple[Event, float]]:
        """(event, kernel weight) for events within CUTOFF_M."""
        if not self.events:
            return []
        cos_q = max(0.01, math.cos(math.radians(min(89.9, max(abs(lat), self.max_abs_lat) + 0.02))))
        span_x = max(1, math.ceil(CUTOFF_M / (self.cell_lng * 111_320.0 * cos_q)))
        out = []
        lngs = [lng] + ([lng - 360] if lng > 179 else []) + ([lng + 360] if lng < -179 else [])
        for qlng in lngs:  # also look across the ±180° meridian
            cy, cx = self._key(lat, qlng)
            for dy in (-1, 0, 1):
                for dx in range(-span_x, span_x + 1):
                    for e in self.grid.get((cy + dy, cx + dx), ()):
                        d = haversine_distance_meters(lat, lng, e.lat, e.lng)
                        if d <= CUTOFF_M:
                            out.append((e, math.exp(-(d * d) / (2 * SIGMA_M * SIGMA_M))))
        return out

    # ── temporal profiles ──
    def _fit_city_profile(self) -> None:
        hist = [0.0] * 24
        for e in self.events:
            hist[e.hour] += 1.0
        hist = _smooth(hist)
        n = len(self.events)
        self.city_hours = [(hist[h] + PRIOR_STRENGTH_CITY * PRIOR_HOURS[h]) / (n + PRIOR_STRENGTH_CITY)
                           for h in range(24)]

    def _local_profile(self, near: list[tuple[Event, float]]) -> list[float]:
        hist = [0.0] * 24
        mass = 0.0
        for e, k in near:
            hist[e.hour] += k
            mass += k
        hist = _smooth(hist)
        return [(hist[h] + PRIOR_STRENGTH_LOCAL * self.city_hours[h]) / (mass + PRIOR_STRENGTH_LOCAL)
                for h in range(24)]

    # ── core rate ──
    @staticmethod
    def _weights(age_d: float) -> float:
        return (math.exp(-age_d / TAU_LONG_D) / TAU_LONG_D
                + BETA_SHORT * math.exp(-age_d / TAU_SHORT_D) / TAU_SHORT_D)

    def daily_rate(self, lat: float, lng: float, near: list | None = None) -> tuple[float, float]:
        """(expected incidents per day near x, severity-weighted equivalent)."""
        near = self._nearby(lat, lng) if near is None else near
        rate = harm = 0.0
        for e, k in near:
            w = k * self._weights(e.age_d)
            rate += w
            harm += w * e.sev
        return rate, harm

    # ── public API ──
    def predict(self, lat: float, lng: float, zones: list[dict] | None = None,
                hours: int = 24, now: datetime | None = None) -> dict:
        now = now or self.now
        near = self._nearby(lat, lng)
        rate, harm = self.daily_rate(lat, lng, near)
        profile = self._local_profile(near)
        start = int(approx_local_hour(lng, now))

        inside = [z for z in zones or [] if distance_to_zone_edge(lat, lng, z) <= 0
                  and RISK_ORDER.get(z["risk_level"], 0) >= RISK_ORDER["caution"]]
        worst_zone = max(inside, key=lambda z: RISK_ORDER[z["risk_level"]], default=None)
        c_zone = ZONE_PRIOR.get(worst_zone["risk_level"], 0.0) if worst_zone else 0.0

        hourly, expected = [], []
        for k in range(hours):
            h = (start + k) % 24
            lam = rate * profile[h]                       # expected incidents in this hour
            c_data = 1 - math.exp(-harm * profile[h] * HARM_SCALE)
            index = 100 * (1 - (1 - c_data) * (1 - c_zone) * (1 - time_prior(h)))
            hourly.append({"hour": h, "label": fmt_hour(h), "index": round(index),
                           "data_index": round(100 * c_data)})
            expected.append(lam)

        # Probability for the NEAR_M disc: the kernel covers ≈ 2πσ² ≈ 0.57 km²; scale to the disc.
        area_ratio = (math.pi * NEAR_M ** 2) / (2 * math.pi * SIGMA_M ** 2)
        p6 = 1 - math.exp(-sum(expected[:6]) * area_ratio)
        p24 = 1 - math.exp(-sum(expected[:24]) * area_ratio)

        n_eff = sum(k for _, k in near)
        if len(self.events) < MIN_EVENTS:
            confidence = "learning"
        elif n_eff >= 5:
            confidence = "high"
        elif n_eff >= 1:
            confidence = "medium"
        else:
            confidence = "low"

        now_idx = hourly[0]["index"] if hourly else 0
        level, emoji = level_for(now_idx)
        peak = max(hourly, key=lambda x: x["index"]) if hourly else None
        safest = self._safest_window(hourly)
        drivers = self._drivers(lat, lng, near, profile, worst_zone, start, confidence)
        types: dict[str, float] = {}
        for e, k in near:
            types[e.kind] = types.get(e.kind, 0.0) + k
        return {
            "index": now_idx, "level": level, "emoji": emoji,
            "data_index": hourly[0]["data_index"] if hourly else 0,
            "data_level": level_for(hourly[0]["data_index"] if hourly else 0)[0],
            "p_6h": round(p6, 3), "p_24h": round(p24, 3),
            "expected_per_day": round(rate * area_ratio, 3),
            "hourly": hourly,
            "peak": peak, "safest": safest,
            "confidence": confidence,
            "dominant_type": max(types, key=types.get) if types else None,
            "drivers": drivers,
            "zone": worst_zone["name"] if worst_zone else None,
            "trained_on": len(self.events),
        }

    @staticmethod
    def _safest_window(hourly: list[dict], width: int = 3) -> dict | None:
        """Lowest-risk block of `width` consecutive daytime-or-evening hours in the next 24 h."""
        best = None
        for i in range(0, max(0, len(hourly) - width + 1)):
            block = hourly[i:i + width]
            if any(b["hour"] < 6 for b in block):  # don't recommend the small hours
                continue
            avg = sum(b["index"] for b in block) / width
            if best is None or avg < best["index"]:
                best = {"from": block[0]["label"], "to": fmt_hour(block[-1]["hour"] + 1), "index": round(avg)}
        return best

    def _drivers(self, lat, lng, near, profile, zone, start_hour, confidence) -> list[str]:
        out = []
        recent = [(e, haversine_distance_meters(lat, lng, e.lat, e.lng)) for e, _ in near]
        within = [(e, d) for e, d in recent if d <= NEAR_M and e.age_d <= 90]
        if within:
            kinds: dict[str, int] = {}
            for e, _ in within:
                kinds[e.kind] = kinds.get(e.kind, 0) + 1
            top = max(kinds, key=kinds.get)
            out.append(f"{len(within)} incident report(s) within {int(NEAR_M)} m in the last 90 days"
                       f" (mostly {TYPE_LABEL.get(top, top.replace('_', ' '))})")
        fresh = sorted((x for x in recent if x[0].age_d <= 3 and x[1] <= NEAR_M), key=lambda x: x[0].age_d)
        if fresh:
            e, d = fresh[0]
            ago = "today" if e.age_d < 1 else f"{int(e.age_d)} day(s) ago"
            out.append(f"Near-repeat risk: a {TYPE_LABEL.get(e.kind, e.kind.replace('_', ' '))} was reported "
                       f"{d:.0f} m away {ago}; incidents often repeat nearby within days")
        if confidence in ("medium", "high"):
            peak = max(range(24), key=lambda h: profile[h])
            if profile[peak] > 1.4 / 24:
                out.append(f"Reports here peak around {fmt_hour(peak)}–{fmt_hour(peak + 2)}")
        if zone:
            out.append(f"Inside {zone['risk_level']}-risk zone '{zone['name']}'")
        if time_prior(start_hour) >= 0.1:
            out.append("Late-night hours carry extra risk")
        if not out:
            out.append("No incidents reported near here recently")
        return out

    # ── area forecast for the command center ──
    def predicted_hotspots(self, hours: int = 6, cell_m: float = 250.0, limit: int = 8,
                           min_index: int = 20, now: datetime | None = None) -> list[dict]:
        """Highest-risk places for the next `hours`, from cells around recent incidents."""
        if not self.events:
            return []
        now = now or self.now
        cands = self._candidate_cells(cell_m, radius_m=NEAR_M)
        scored = []
        for lat, lng in cands:
            near = self._nearby(lat, lng)
            if not near:
                continue
            rate, harm = self.daily_rate(lat, lng, near)
            if harm <= 0:
                continue
            profile = self._local_profile(near)
            start = int(approx_local_hour(lng, now))
            hs = [(start + k) % 24 for k in range(hours)]
            harm_window = harm * sum(profile[h] for h in hs)
            peak_h = max(hs, key=lambda h: profile[h])
            index = 100 * (1 - math.exp(-harm * profile[peak_h] * HARM_SCALE))
            if index < min_index:
                continue
            types: dict[str, float] = {}
            for e, k in near:
                types[e.kind] = types.get(e.kind, 0.0) + k
            area_ratio = (math.pi * NEAR_M ** 2) / (2 * math.pi * SIGMA_M ** 2)
            scored.append({"lat": round(lat, 5), "lng": round(lng, 5), "index": round(index),
                           "level": level_for(index)[0], "peak": fmt_hour(peak_h),
                           "p_window": round(1 - math.exp(-rate * sum(profile[h] for h in hs) * area_ratio), 3),
                           "dominant_type": max(types, key=types.get), "_harm": harm_window})
        scored.sort(key=lambda c: -c["_harm"])
        picked: list[dict] = []
        for c in scored:  # keep places at least ~2 cells apart (non-maximum suppression)
            if all(haversine_distance_meters(c["lat"], c["lng"], p["lat"], p["lng"]) > 2.4 * cell_m for p in picked):
                picked.append(c)
            if len(picked) >= limit:
                break
        for p in picked:
            p.pop("_harm", None)
        return picked

    def _candidate_cells(self, cell_m: float, radius_m: float, extra: list[Event] | None = None) -> list[tuple[float, float]]:
        """Centres of cell_m-sized cells within radius_m of any event. Each latitude row has its own
        longitude step, so cells are ~cell_m wide in true metres everywhere."""
        dlat = cell_m / 111_320.0
        steps = int(math.ceil(radius_m / cell_m))
        seen: set[tuple[int, int]] = set()
        out = []
        for e in list(self.events) + list(extra or []):
            cy = int(math.floor(e.lat / dlat))
            for row in range(cy - steps, cy + steps + 1):
                row_lat = (row + 0.5) * dlat
                row_dlng = cell_m / (111_320.0 * max(0.01, math.cos(math.radians(row_lat))))
                cx = int(math.floor(e.lng / row_dlng))
                for col in range(cx - steps, cx + steps + 1):
                    if (row, col) not in seen:
                        seen.add((row, col))
                        out.append((row_lat, (col + 0.5) * row_dlng))
        return out


# ═════════════════════════════════════════════
#  Backtest: does the model actually predict?
# ═════════════════════════════════════════════

def backtest(incidents: list[dict], now: datetime | None = None, test_days: int = 14,
             top_share: float = 0.10, cell_m: float = 250.0) -> dict:
    """Train before (now - test_days), forecast the next test_days, score against what happened."""
    now = now or datetime.now(timezone.utc)
    cut = now - timedelta(days=test_days)
    rows = [i for i in incidents if i.get("lat") is not None and _parse_time(i.get("created_at"))]
    train = [i for i in rows if _parse_time(i["created_at"]) < cut]
    test = [i for i in rows if cut <= _parse_time(i["created_at"]) <= now]
    if len(train) < 8 or len(test) < 3:
        return {"ready": False, "train": len(train), "test": len(test),
                "note": f"Needs ≥ 8 earlier and ≥ 3 recent incident reports with a location "
                        f"(has {len(train)} and {len(test)})."}

    model = RiskModel(train, now=cut)
    test_events = [Event(float(i["lat"]), float(i["lng"]), 0.0, 0, 1.0, "", 0) for i in test]
    cells = model._candidate_cells(cell_m, radius_m=NEAR_M, extra=test_events)
    dlat = cell_m / 111_320.0

    def cell_of(lat, lng):
        row = int(math.floor(lat / dlat))
        row_lat = (row + 0.5) * dlat
        row_dlng = cell_m / (111_320.0 * max(0.01, math.cos(math.radians(row_lat))))
        return row, int(math.floor(lng / row_dlng))

    keys = [cell_of(lat, lng) for lat, lng in cells]
    model_score = [model.daily_rate(lat, lng)[0] for lat, lng in cells]
    # Naive baseline: number of past incidents in the same cell (what a plain pin map shows).
    counts: dict[tuple[int, int], int] = {}
    for i in train:
        k = cell_of(float(i["lat"]), float(i["lng"]))
        counts[k] = counts.get(k, 0) + 1
    base_score = [counts.get(k, 0) + 1e-9 * s for k, s in zip(keys, model_score)]  # ties → model order

    test_cells = [cell_of(float(i["lat"]), float(i["lng"])) for i in test]
    n_top = max(1, int(round(len(cells) * top_share)))

    def hit_rate(scores):
        order = sorted(range(len(cells)), key=lambda j: -scores[j])[:n_top]
        top = {keys[j] for j in order}
        return sum(1 for c in test_cells if c in top) / len(test_cells)

    hr_model, hr_base = hit_rate(model_score), hit_rate(base_score)
    area_share = n_top / len(cells)
    # Time-of-day skill: how much more likely the model rated each test incident's actual hour
    # than a flat 1/24 guess (> 1 means the hour profile helps).
    lift = []
    for i in test:
        lat, lng = float(i["lat"]), float(i["lng"])
        h = int(approx_local_hour(lng, _parse_time(i["created_at"])))
        lift.append(24 * model._local_profile(model._nearby(lat, lng))[h])
    return {
        "ready": True, "train": len(train), "test": len(test), "test_days": test_days,
        "cells": len(cells), "top_share": round(area_share, 3),
        "hit_rate": round(hr_model, 3), "pai": round(hr_model / area_share, 2),
        "baseline_hit_rate": round(hr_base, 3), "baseline_pai": round(hr_base / area_share, 2),
        "hour_lift": round(sum(lift) / len(lift), 2),
    }


# ═════════════════════════════════════════════
#  Cached model shared by the bot, portal and command center
# ═════════════════════════════════════════════

MODEL_REFIT_S = 600       # refit at least this often (ages shift, near-repeat effects fade)
BACKTEST_CACHE_S = 3600
_model_cache: dict = {"key": None, "ts": 0.0, "model": None}
_backtest_cache: dict = {"key": None, "ts": 0.0, "data": None}
_hotspot_forecast_cache: dict = {"key": None, "ts": 0.0, "data": []}


def _key(incidents: list[dict]) -> tuple:
    return len(incidents), max((i.get("incident_id") or 0 for i in incidents), default=0)


def history_since() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).isoformat()


def cached_model(incidents: list[dict]) -> RiskModel:
    key = _key(incidents)
    if _model_cache["model"] is None or key != _model_cache["key"] or time.time() - _model_cache["ts"] > MODEL_REFIT_S:
        _model_cache.update(key=key, ts=time.time(), model=RiskModel(incidents))
    return _model_cache["model"]


MODEL_FRESH_S = 30  # reuse without re-reading incidents; new incidents call invalidate()


async def get_model() -> RiskModel:
    from travel_buddy.safety.store import get_incidents_with_location  # local import avoids cycles
    if _model_cache["model"] is not None and _model_cache["key"] is not None             and time.time() - _model_cache["ts"] < MODEL_FRESH_S:
        return _model_cache["model"]
    return cached_model(await get_incidents_with_location(history_since()))


def cached_backtest(incidents: list[dict]) -> dict:
    key = _key(incidents)
    if _backtest_cache["data"] is None or key != _backtest_cache["key"] or time.time() - _backtest_cache["ts"] > BACKTEST_CACHE_S:
        _backtest_cache.update(key=key, ts=time.time(), data=backtest(incidents))
    return _backtest_cache["data"]


def cached_hotspot_forecast(incidents: list[dict], hours: int = 6) -> list[dict]:
    key = (_key(incidents), hours)
    if key != _hotspot_forecast_cache["key"] or time.time() - _hotspot_forecast_cache["ts"] > 300:
        data = cached_model(incidents).predicted_hotspots(hours=hours)
        _hotspot_forecast_cache.update(key=key, ts=time.time(), data=data)
    return _hotspot_forecast_cache["data"]


def invalidate() -> None:
    _model_cache["key"] = _backtest_cache["key"] = _hotspot_forecast_cache["key"] = None


# ═════════════════════════════════════════════
#  Text for Telegram / copilot
# ═════════════════════════════════════════════

def summary_line(f: dict) -> str:
    """One line for the AI copilot context."""
    parts = [f"now {f['level']} (index {f['index']}/100)",
             f"{round(f['p_6h'] * 100)}% chance of a reported incident within 500 m in the next 6 h"]
    if f.get("peak"):
        parts.append(f"riskiest hour {f['peak']['label']}")
    if f.get("safest"):
        parts.append(f"safest window {f['safest']['from']}–{f['safest']['to']}")
    parts.append(f"confidence {f['confidence']}")
    return "; ".join(parts) + ". Drivers: " + "; ".join(f["drivers"])


def bar(index: int) -> str:
    return "▁▂▃▄▅▆▇█"[min(7, int(index / 12.5))]


def proactive_warning(f: dict) -> str | None:
    """A heads-up when incident risk here is elevated now or predicted to rise within 3 hours.
    Only data-driven risk counts (zones and night already have their own alerts)."""
    if f["confidence"] not in ("medium", "high") or not f["hourly"]:
        return None
    kind = (f["dominant_type"] or "incident").replace("_", " ")
    tip = "Keep valuables out of sight, stay on busy, well-lit streets and use registered taxis."
    if f["data_level"] in ("elevated", "high"):
        return (f"📈 <b>Predicted risk: {f['data_level']}</b>. The model expects incidents around here right now "
                f"(mostly {kind}; {round(f['p_6h'] * 100)}% chance of a report within 500 m in the next 6 h).\n💡 {tip}")
    soon = max(f["hourly"][1:4], key=lambda h: h["data_index"], default=None)
    if soon and soon["data_index"] >= 40:
        return (f"📈 <b>Risk rising later</b>: incidents here usually pick up around <b>{soon['label']}</b> "
                f"(mostly {kind}). Plan to move on before then.\n💡 {tip}")
    return None


def report_html(f: dict, place: str | None = None) -> str:
    """Full forecast for Telegram (HTML)."""
    from html import escape
    lines = [f"🔮 <b>Risk forecast{(' · ' + escape(place)) if place else ''}</b>",
             f"<b>Now:</b> {f['emoji']} {f['level'].title()} ({f['index']}/100)",
             f"<b>Chance of a reported incident within 500 m:</b> {round(f['p_6h'] * 100)}% next 6 h · "
             f"{round(f['p_24h'] * 100)}% next 24 h"]
    hourly = f["hourly"]
    if hourly:
        blocks = [hourly[i:i + 3] for i in range(0, len(hourly), 3)]
        lines += ["", "<b>Next 24 hours</b> (3-hour blocks):",
                  "<code>" + " ".join(f"{b[0]['label'][:2]}" for b in blocks) + "</code>",
                  "<code>" + " ".join(bar(max(x["index"] for x in b)) * 2 for b in blocks) + "</code>"]
    if f.get("peak") and f["peak"]["index"] >= 20:
        lines.append(f"⚠️ Riskiest: around <b>{f['peak']['label']}</b> ({f['peak']['index']}/100)")
    if f.get("safest"):
        lines.append(f"✅ Safest window: <b>{f['safest']['from']}–{f['safest']['to']}</b>")
    lines += ["", "<b>Why:</b>"] + [f"• {escape(d)}" for d in f["drivers"]]
    note = {"learning": "The model is still learning: too few incident reports yet, so it leans on zones and time of day.",
            "low": "Few reports near here, so confidence is low.",
            "medium": "Moderate confidence.", "high": "High confidence: plenty of nearby reports."}[f["confidence"]]
    lines += ["", f"<i>Predictive model trained on {f['trained_on']} incident report(s) from the last "
                  f"{HISTORY_DAYS} days, updated automatically. {note}</i>"]
    return "\n".join(lines)
