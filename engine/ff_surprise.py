"""FF surprise → poin bias per currency (faktor F) + util WIB + skor per-event.

Formula (disetujui user):
    poin_event = z × polaritas × bobot_impact × freshness
    z          = (actual − forecast) / σ_event   (σ + polaritas dari sigma_table)
    bobot_impact = high 1.0 / medium 0.5 / low 0.15
    freshness  = exp(−hari_sejak_rilis / 1.5)     (hari ini≈1, 2hr≈0.26 → "priced in")
Skor currency = clamp(Σ poin_event × SCALE, −1, +1).

Waktu FF sumber = UTC−5 (empiris: ADP 7:15am scraper = 8:15 ET; NFP 7:30am = 8:30 ET)
→ dikonversi ke WIB (UTC+7) = +12 jam. Lihat to_wib().
σ/polaritas/scale = PLACEHOLDER sampai dikalibrasi dari histori.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from engine.sigma_table import sigma_polarity
from utils.timeutils import parse_iso_utc

_IMPACT_W = {"high": 1.0, "medium": 0.5, "low": 0.15, "holiday": 0.0, "": 0.0}
_FF_SCALE = 0.5          # PLACEHOLDER — skala poin→[-1,1]
_Z_CLAMP = 3.0
_FRESH_TAU_DAYS = 1.5    # PLACEHOLDER — decay freshness (hari)
_SRC_OFFSET_H = -5       # TZ sumber scraper FF (empiris UTC−5)
_WIB_OFFSET_H = 7


def _parse_num(s: Any) -> float | None:
    """'4.8%'→4.8, '122K'→122000, '-8.0M'→-8e6, '1.79B'→1.79e9."""
    if s is None:
        return None
    txt = str(s).strip().replace(",", "").replace("%", "")
    if not txt or txt in ("-", "—"):
        return None
    mult = 1.0
    if txt[-1:].upper() in ("K", "M", "B", "T"):
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[txt[-1].upper()]
        txt = txt[:-1]
    try:
        return float(txt) * mult
    except ValueError:
        return None


def to_wib(date_str: str, time_str: str, year: int | None = None) -> datetime | None:
    """('Wed Jun 3','7:15am') dari sumber UTC−5 → datetime WIB (naive). None kalau gagal."""
    if not date_str:
        return None
    year = year or datetime.utcnow().year
    base = None
    for fmt in ("%a %b %d", "%b %d", "%a %B %d", "%A %b %d"):
        try:
            base = datetime.strptime(date_str.strip(), fmt).replace(year=year)
            break
        except ValueError:
            continue
    if base is None:
        return None
    hh, mm = 0, 0
    t = (time_str or "").strip().lower().replace(" ", "")
    if t and t not in ("allday", "tentative", "—", "-", ""):
        try:
            ampm = t[-2:]
            core = t[:-2] if ampm in ("am", "pm") else t
            parts = core.split(":")
            hh = int(parts[0])
            mm = int(parts[1]) if len(parts) > 1 else 0
            if ampm == "pm" and hh != 12:
                hh += 12
            if ampm == "am" and hh == 12:
                hh = 0
        except Exception:
            hh, mm = 0, 0
    src_dt = base.replace(hour=hh, minute=mm)
    return src_dt + timedelta(hours=_WIB_OFFSET_H - _SRC_OFFSET_H)


def _now_wib_naive(now: Any = None) -> datetime:
    if isinstance(now, datetime):
        return now.replace(tzinfo=None)
    # default: WIB sekarang
    return (datetime.utcnow() + timedelta(hours=_WIB_OFFSET_H))


def score_event(e: dict, now_wib: datetime | None = None) -> dict | None:
    """Skor satu event FF. None kalau bukan surprise terukur (belum rilis / tak ada σ / impact 0)."""
    now = _now_wib_naive(now_wib)
    ccy = (e.get("currency") or "").upper()
    actual = _parse_num(e.get("actual"))
    forecast = _parse_num(e.get("forecast"))
    if not ccy or actual is None or forecast is None:
        return None
    sigma, polarity = sigma_polarity(e.get("name", ""), ccy)
    if sigma is None or polarity is None or sigma == 0:
        return None
    impact_w = _IMPACT_W.get((e.get("impact") or "").lower(), 0.0)
    if impact_w == 0.0:
        return None
    z = max(-_Z_CLAMP, min(_Z_CLAMP, (actual - forecast) / sigma))
    wib = to_wib(e.get("date", ""), e.get("time", ""), now.year)
    if wib is not None:
        days_ago = max(0.0, (now - wib).total_seconds() / 86400.0)
    else:
        days_ago = 1.0
    fresh = math.exp(-days_ago / _FRESH_TAU_DAYS)
    pts = z * polarity * impact_w * fresh
    return {
        "ccy": ccy, "points": round(pts, 4), "z": round(z, 2),
        "freshness": round(fresh, 3), "days_ago": round(days_ago, 1),
        "impact_w": impact_w, "polarity": polarity,
    }


def _aggregate_scored(items: list[dict]) -> dict[str, dict]:
    """Agregasi item ber-skor → {CCY:{score,detail,n}}.

    ANTI-DOUBLE-COUNT (#2): rilis berkorelasi pada TIMESTAMP yang sama
    (mis. NFP+AHE+Unemployment Rate jam 8:30) dihitung SEKALI — ambil |points|
    TERBESAR per (ccy, group_key). Lalu jumlahkan antar timestamp BERBEDA
    (rilis di hari/jam beda = surprise beda, sudah ter-decay freshness).
    Konsisten dengan konvensi max-|z| di score_R_hard.

    item wajib punya: ccy, points, z, freshness, group_key, label.
    """
    best: dict[str, dict[str, dict]] = {}
    for it in items:
        ccy = it["ccy"]
        gk = it["group_key"]
        cur = best.setdefault(ccy, {}).get(gk)
        if cur is None or abs(it["points"]) > abs(cur["points"]):
            best[ccy][gk] = it

    out: dict[str, dict] = {}
    for ccy, groups in best.items():
        chosen = list(groups.values())
        total = sum(c["points"] for c in chosen)
        chosen.sort(key=lambda c: abs(c["points"]), reverse=True)
        details = [
            f"{c['label']} {c['points']:+.2f} (z{c['z']:+.1f}×fr{c['freshness']:.2f})"
            for c in chosen
        ]
        out[ccy] = {
            "score": round(max(-1.0, min(1.0, total * _FF_SCALE)), 4),
            "detail": " | ".join(details[:4]) + (
                f" (+{len(details) - 4} lain)" if len(details) > 4 else ""),
            "n": len(chosen),
        }
    return out


def compute_ff_surprise(ff_events: list[dict], now: Any = None) -> dict[str, dict]:
    """Agregasi per currency dari event hasil scrape ForexFactory (format string).
    now = datetime/date WIB (opsional). Return {CCY:{score,detail,n}}.
    Group anti-double-count = (date|time) baris kalender. Lihat _aggregate_scored.
    """
    now_wib = _now_wib_naive(now)
    items: list[dict] = []
    for e in ff_events or []:
        if not isinstance(e, dict):
            continue
        sc = score_event(e, now_wib)
        if sc is None:
            continue
        items.append({
            "ccy": sc["ccy"], "points": sc["points"], "z": sc["z"],
            "freshness": sc["freshness"],
            "group_key": f"{e.get('date','')}|{e.get('time','')}",
            "label": e.get("name", ""),
        })
    return _aggregate_scored(items)


def _days_ago_from_iso(ts_iso: str, now_utc_dt: datetime | None) -> float:
    """Umur event (hari) dari ts_utc ISO. now_utc_dt opsional (utk test deterministik)."""
    if not ts_iso:
        return 1.0
    try:
        ev = parse_iso_utc(ts_iso)
        ref = now_utc_dt or datetime.now(timezone.utc)
        return max(0.0, (ref - ev).total_seconds() / 86400.0)
    except Exception:
        return 1.0


def _score_calendar_event(e: dict, now_utc_dt: datetime | None) -> dict | None:
    """Skor satu released-event kalender faireconomy (sudah float bersih + ter-enrich σ).

    Beda dari score_event (yg parse string FF): input di sini sudah
    actual/forecast float, ts_utc ISO, dan historical_std + surprise_polarity
    dari engine/sigma_table. None kalau bukan surprise terukur.
    """
    ccy = (e.get("currency") or "").upper()
    actual = e.get("actual")
    forecast = e.get("forecast")
    sigma = e.get("historical_std")
    polarity = e.get("surprise_polarity")
    if not ccy or actual is None or forecast is None:
        return None
    if sigma is None or polarity is None:
        return None
    try:
        sigma_f = float(sigma)
    except (TypeError, ValueError):
        return None
    if sigma_f == 0.0:
        return None
    impact_w = _IMPACT_W.get((e.get("impact") or "").lower(), 0.0)
    if impact_w == 0.0:
        return None
    z = max(-_Z_CLAMP, min(_Z_CLAMP, (float(actual) - float(forecast)) / sigma_f))
    ts = e.get("ts_utc") or ""
    days_ago = _days_ago_from_iso(ts, now_utc_dt)
    fresh = math.exp(-days_ago / _FRESH_TAU_DAYS)
    pts = z * float(polarity) * impact_w * fresh
    return {
        "ccy": ccy, "points": round(pts, 4), "z": round(z, 2),
        "freshness": round(fresh, 3),
        "group_key": ts,                       # ts_utc = penanda batch rilis
        "label": e.get("name", ""),
    }


def compute_ff_surprise_from_calendar(
    released_events: list[dict], now: Any = None
) -> dict[str, dict]:
    """Faktor F OTOMATIS dari kalender faireconomy — jalur default (tanpa scrape manual).

    released_events = event status=released yang sudah punya actual + historical_std
    (di-enrich engine/sigma_table.enrich_surprise_fields). Polarity unemployment/claims
    sudah ter-set di surprise_polarity. Return {CCY:{score,detail,n}}.

    now = datetime UTC opsional (default: sekarang) — untuk test deterministik.
    """
    now_utc_dt = now if isinstance(now, datetime) else None
    items: list[dict] = []
    for e in released_events or []:
        if not isinstance(e, dict):
            continue
        sc = _score_calendar_event(e, now_utc_dt)
        if sc is None:
            continue
        items.append(sc)
    return _aggregate_scored(items)
