"""
collectors/calendar_evt.py — Economic event calendar fetcher.

Output schema (arsitektur §4):
{
  "as_of_utc": "2026-06-01T07:00:00Z",
  "events": [
    {
      "ts_utc":   "2026-06-01T12:30:00Z",
      "ts_wib":   "2026-06-01 19:30",
      "currency": "USD",
      "impact":   "HIGH",
      "name":     "Core PCE m/m",
      "forecast": 0.26,
      "previous": 0.30,
      "actual":   null,
      "status":   "upcoming"
    },
    ...
  ]
}

SUMBER YANG DIPAKAI: Dua sumber dengan fallback hierarki.

  PRIMARY — ForexFactory via faireconomy.media JSON mirror:
    URL: https://nfs.faireconomy.media/ff_calendar_thisweek.json
         https://nfs.faireconomy.media/ff_calendar_nextweek.json
    Dokumentasi: https://github.com/jblanked/Free-Forex-API (lihat README "Calendar")
    Response shape:
        [
          {
            "title":    "Core PCE m/m",
            "country":  "USD",
            "date":     "2026-06-01T12:30:00-04:00",   ← EST/EDT
            "impact":   "High",                          ← "High"|"Medium"|"Low"|"Non-Economic"
            "forecast": "0.3%",                          ← string dengan %, bisa ""
            "previous": "0.2%",                          ← string dengan %, bisa ""
            "actual":   "0.3%"                           ← "" kalau belum rilis
          },
          ...
        ]
    Timezone: date menggunakan offset EST (-05:00) atau EDT (-04:00) — konversi ke UTC.
    Refresh: tersedia this-week dan next-week. Kita ambil keduanya dan merge.

  FALLBACK — Myfxbook Economic Calendar API:
    URL: https://www.myfxbook.com/api/get-economic-calendar.json
    Params: session, start (YYYY-MM-DD), end (YYYY-MM-DD)
    Docs: https://www.myfxbook.com/help/api
    Response shape:
        {
          "error": false,
          "calendar": [
            {
              "date":     "2026-06-01 12:30",   ← UTC
              "currency": "USD",
              "impact":   "3",                  ← "3"=High, "2"=Medium, "1"=Low
              "name":     "Core PCE m/m",
              "actual":   "0.3",
              "forecast": "0.3",
              "previous": "0.2"
            }
          ]
        }
    Catatan: requires myfxbook session token.

WINDOW:
  - Fetch 48 jam ke depan + 24 jam ke belakang (event yang baru rilis hari ini).
  - status: "upcoming" kalau ts_utc > now, "released" kalau <= now.
  - Filter impact: semua impact diambil (HIGH/MED/LOW), app.py bisa filter sendiri.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from utils.timeutils import (
    event_status,
    fmt_iso_utc,
    fmt_wib_display,
    now_utc,
    parse_iso_utc,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanta
# ---------------------------------------------------------------------------

_TIMEOUT: int = 12
_MAX_RETRY: int = 2
_RETRY_BACKOFF: float = 1.5

_FAIRECONOMY_THIS_WEEK_URL: str = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
)
_FAIRECONOMY_NEXT_WEEK_URL: str = (
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json"
)
_FAIRECONOMY_LAST_WEEK_URL: str = (
    "https://nfs.faireconomy.media/ff_calendar_lastweek.json"
)

_MYFXBOOK_CALENDAR_URL: str = (
    "https://www.myfxbook.com/api/get-economic-calendar.json"
)

# Mapping impact string → standar output (HIGH/MED/LOW)
_IMPACT_MAP_FAIRECONOMY: dict[str, str] = {
    "high":          "HIGH",
    "medium":        "MED",
    "low":           "LOW",
    "non-economic":  "LOW",
    "holiday":       "LOW",
}
_IMPACT_MAP_MYFXBOOK: dict[str, str] = {
    "3": "HIGH",
    "2": "MED",
    "1": "LOW",
    "0": "LOW",
}

# Currency yang relevan (semua FX major + XAU proxy)
_RELEVANT_CURRENCIES: set[str] = {
    "USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF",
    # XAU tidak punya event calendar sendiri; event USD/global memengaruhinya.
    # Tambahkan broad market / multi-currency:
    "ALL", "GLOBAL",
}

# Window fetch: acuan ±jam dari sekarang
_WINDOW_PAST_HOURS: int = 24 * 14   # 2 minggu ke belakang (historis dgn aktual)
_WINDOW_FUTURE_HOURS: int = 24 * 8  # ~1 minggu ke depan


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get_json_with_retry(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = _TIMEOUT,
    max_retry: int = _MAX_RETRY,
) -> Any:
    """GET request, return parsed JSON. Raise RuntimeError setelah semua retry gagal."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, */*",
    }
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(max_retry):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            if attempt < max_retry - 1:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
            logger.debug("Calendar retry %d/%d %s: %s", attempt + 1, max_retry, url, exc)
    raise RuntimeError(f"GET {url} gagal: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_number(raw: str | int | float | None) -> float | None:
    """Parse nilai numerik dari string (mungkin berisi '%', ' bps', dll.).

    Return None kalau kosong atau tidak bisa diparse.
    Contoh: "0.3%" → 0.3, "1.25" → 1.25, "" → None, None → None.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return None
    # Hapus suffix persen, 'k', 'M', 'bps', dll.
    s_clean = re.sub(r"[%kKMBbps\s]+$", "", s).strip()
    # Handle 'K' = ribuan (misal "-12.3K")
    multiplier = 1.0
    if s.upper().endswith("K"):
        multiplier = 1_000.0
    elif s.upper().endswith("M"):
        multiplier = 1_000_000.0
    try:
        return float(s_clean) * multiplier
    except ValueError:
        return None


def _parse_faireconomy_datetime(raw_date: str) -> datetime | None:
    """Parse datetime dari faireconomy, yang pakai offset EST/EDT.

    Format contoh:
        "2026-06-01T12:30:00-04:00"  ← EDT
        "2026-06-01T12:30:00-05:00"  ← EST
        "2026-06-01T00:00:00-05:00"  ← All-day event (midnight)

    Return datetime aware UTC.
    """
    try:
        dt = datetime.fromisoformat(raw_date)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        pass
    # Fallback: coba parse_iso_utc (handle Z suffix)
    try:
        return parse_iso_utc(raw_date)
    except ValueError:
        logger.debug("Gagal parse faireconomy datetime: %r", raw_date)
        return None


def _normalize_impact(raw: str | None, mapping: dict[str, str]) -> str:
    """Normalisasi string impact ke HIGH/MED/LOW."""
    if raw is None:
        return "LOW"
    return mapping.get(str(raw).strip().lower(), "LOW")


# ---------------------------------------------------------------------------
# SOURCE A — faireconomy.media (ForexFactory mirror)
# ---------------------------------------------------------------------------

def _fetch_faireconomy_week(url: str) -> list[dict]:
    """Fetch satu endpoint faireconomy (this-week atau next-week).

    Return list raw event dict dari JSON. Raises RuntimeError bila gagal.
    """
    raw_list = _get_json_with_retry(url)
    if not isinstance(raw_list, list):
        raise RuntimeError(f"faireconomy: response bukan list (type={type(raw_list).__name__})")
    return raw_list


def _parse_faireconomy_events(
    raw_list: list[dict],
    window_start: datetime,
    window_end: datetime,
) -> list[dict]:
    """Parse dan filter event dari faireconomy ke schema output.

    Args:
        raw_list:     Output JSON dari faireconomy.
        window_start: Awal window (UTC, aware).
        window_end:   Akhir window (UTC, aware).

    Returns list event sesuai schema §4.
    """
    events: list[dict] = []
    now = now_utc()

    for item in raw_list:
        if not isinstance(item, dict):
            continue

        # Timestamp
        raw_date = item.get("date") or item.get("datetime") or item.get("time")
        if not raw_date:
            continue
        ts_utc = _parse_faireconomy_datetime(str(raw_date))
        if ts_utc is None:
            continue

        # Filter window
        if ts_utc < window_start or ts_utc > window_end:
            continue

        # Currency / country
        currency = str(item.get("country") or item.get("currency") or "").upper().strip()
        if not currency:
            continue
        # faireconomy pakai "USD", "EUR", dll. Sudah uppercase.

        # Impact
        raw_impact = item.get("impact") or item.get("importance") or ""
        impact = _normalize_impact(str(raw_impact).lower(), _IMPACT_MAP_FAIRECONOMY)

        # Name / title
        name = str(item.get("title") or item.get("name") or item.get("event") or "").strip()
        if not name:
            continue

        # Values — semua bisa string dengan '%' atau kosong
        forecast = _parse_number(item.get("forecast"))
        previous = _parse_number(item.get("previous"))
        actual_raw = item.get("actual")
        actual = _parse_number(actual_raw)
        # actual=None kalau string kosong (belum rilis)
        if isinstance(actual_raw, str) and not actual_raw.strip():
            actual = None

        # Status
        status = event_status(ts_utc, now)

        events.append({
            "ts_utc":   fmt_iso_utc(ts_utc),
            "ts_wib":   fmt_wib_display(ts_utc),
            "currency": currency,
            "impact":   impact,
            "name":     name,
            "forecast": forecast,
            "previous": previous,
            "actual":   actual,
            "status":   status,
        })

    return events


def _fetch_faireconomy(
    window_start: datetime,
    window_end: datetime,
) -> list[dict]:
    """Fetch dari kedua endpoint faireconomy (this-week + next-week) dan merge.

    Dedup berdasarkan (ts_utc, currency, name) untuk menghindari duplikat
    event yang muncul di kedua file.
    """
    all_raw: list[dict] = []
    errors: list[str] = []

    # lastweek dulu (historis), lalu thisweek, lalu nextweek. Toleran kalau 404.
    for url in [_FAIRECONOMY_LAST_WEEK_URL, _FAIRECONOMY_THIS_WEEK_URL, _FAIRECONOMY_NEXT_WEEK_URL]:
        try:
            raw = _fetch_faireconomy_week(url)
            all_raw.extend(raw)
            logger.info("faireconomy %s: %d raw events", url.split("/")[-1], len(raw))
        except Exception as exc:
            errors.append(f"{url.split('/')[-1]}: {exc}")
            logger.warning("faireconomy fetch gagal untuk %s: %s (lanjut endpoint lain)", url, exc)

    if not all_raw and errors:
        raise RuntimeError(f"faireconomy: semua endpoint gagal — {'; '.join(errors)}")

    events = _parse_faireconomy_events(all_raw, window_start, window_end)

    # Dedup
    seen: set[tuple] = set()
    unique: list[dict] = []
    for ev in events:
        key = (ev["ts_utc"], ev["currency"], ev["name"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)

    return unique


# ---------------------------------------------------------------------------
# SOURCE B — Myfxbook Economic Calendar (fallback)
# ---------------------------------------------------------------------------

def _fetch_myfxbook_calendar(
    session: str,
    window_start: datetime,
    window_end: datetime,
) -> list[dict]:
    """Fetch calendar dari Myfxbook API.

    API endpoint: GET https://www.myfxbook.com/api/get-economic-calendar.json
    Params: session, start (YYYY-MM-DD), end (YYYY-MM-DD)

    Response shape:
        {
          "error": false,
          "calendar": [
            {
              "date":     "2026-06-01 12:30",   ← UTC
              "currency": "USD",
              "impact":   "3",                  ← "3"=High, "2"=Medium, "1"=Low
              "name":     "Core PCE m/m",
              "actual":   "0.3",
              "forecast": "0.3",
              "previous": "0.2"
            }
          ]
        }

    Raises RuntimeError bila gagal atau error flag dari API.
    """
    start_str = window_start.strftime("%Y-%m-%d")
    end_str = window_end.strftime("%Y-%m-%d")

    data = _get_json_with_retry(
        _MYFXBOOK_CALENDAR_URL,
        params={"session": session, "start": start_str, "end": end_str},
    )

    if data.get("error"):
        raise RuntimeError(f"Myfxbook calendar API error: {data.get('message', 'unknown')}")

    raw_events: list[dict] = data.get("calendar", [])
    now = now_utc()
    events: list[dict] = []

    for item in raw_events:
        if not isinstance(item, dict):
            continue

        # Timestamp: "2026-06-01 12:30" — diasumsi UTC per dokumentasi
        raw_date = str(item.get("date") or "").strip()
        if not raw_date:
            continue
        try:
            ts_utc = datetime.strptime(raw_date, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                ts_utc = datetime.fromisoformat(raw_date).replace(tzinfo=timezone.utc)
            except ValueError:
                logger.debug("Myfxbook calendar: gagal parse date %r", raw_date)
                continue

        if ts_utc < window_start or ts_utc > window_end:
            continue

        currency = str(item.get("currency") or "").upper().strip()
        if not currency:
            continue

        raw_impact = str(item.get("impact") or "").strip()
        impact = _IMPACT_MAP_MYFXBOOK.get(raw_impact, "LOW")

        name = str(item.get("name") or item.get("title") or "").strip()
        if not name:
            continue

        forecast = _parse_number(item.get("forecast"))
        previous = _parse_number(item.get("previous"))
        actual_raw = item.get("actual")
        actual = _parse_number(actual_raw)
        if isinstance(actual_raw, str) and not actual_raw.strip():
            actual = None

        status = event_status(ts_utc, now)

        events.append({
            "ts_utc":   fmt_iso_utc(ts_utc),
            "ts_wib":   fmt_wib_display(ts_utc),
            "currency": currency,
            "impact":   impact,
            "name":     name,
            "forecast": forecast,
            "previous": previous,
            "actual":   actual,
            "status":   status,
        })

    return events


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_calendar(
    *,
    myfxbook_session: str | None = None,
) -> dict:
    """Fetch economic calendar events untuk window -24h s/d +48h dari sekarang.

    Strategy:
    1. Coba faireconomy.media (ForexFactory mirror) — tidak perlu auth.
    2. Kalau gagal, fallback ke Myfxbook Calendar API (perlu session token).
    3. Kalau keduanya gagal → return events=[] + error key (tidak crash).

    Args:
        myfxbook_session: Session token Myfxbook untuk fallback. Kalau None,
                          dicoba dari st.secrets (MYFXBOOK_SESSION).

    Returns dict sesuai schema §4 calendar:
        {
          "as_of_utc": str,
          "events": [
            {
              "ts_utc":   str,
              "ts_wib":   str,
              "currency": str,
              "impact":   "HIGH"|"MED"|"LOW",
              "name":     str,
              "forecast": float|null,
              "previous": float|null,
              "actual":   float|null,
              "status":   "upcoming"|"released"
            },
            ...
          ]
        }

    Events diurutkan ascending by ts_utc (paling dekat duluan).
    """
    ts_now = now_utc()
    window_start = ts_now - timedelta(hours=_WINDOW_PAST_HOURS)
    window_end = ts_now + timedelta(hours=_WINDOW_FUTURE_HOURS)

    errors: list[str] = []

    # ---- SOURCE A: faireconomy.media ----
    try:
        events = _fetch_faireconomy(window_start, window_end)
        events.sort(key=lambda e: e["ts_utc"])
        logger.info("Calendar faireconomy: %d events dalam window", len(events))
        return {
            "as_of_utc": fmt_iso_utc(ts_now),
            "events": events,
        }
    except Exception as exc:
        errors.append(f"faireconomy: {exc}")
        logger.warning("Calendar faireconomy gagal: %s", exc)

    # ---- SOURCE B: Myfxbook Calendar (fallback) ----
    session = myfxbook_session
    if not session:
        try:
            import streamlit as st  # type: ignore
            session = (
                st.secrets.get("MYFXBOOK_SESSION")
                or st.secrets.get("myfxbook_session")
            )
        except (ImportError, Exception):
            pass

    if session:
        try:
            events = _fetch_myfxbook_calendar(session, window_start, window_end)
            events.sort(key=lambda e: e["ts_utc"])
            logger.info("Calendar Myfxbook fallback: %d events dalam window", len(events))
            return {
                "as_of_utc": fmt_iso_utc(ts_now),
                "events": events,
            }
        except Exception as exc:
            errors.append(f"myfxbook: {exc}")
            logger.warning("Calendar Myfxbook fallback gagal: %s", exc)
    else:
        errors.append("myfxbook: session tidak tersedia (no fallback credentials)")
        logger.debug("Myfxbook session tidak ada, skip fallback.")

    # ---- Semua sumber gagal ----
    logger.error("get_calendar: semua sumber gagal — %s", "; ".join(errors))
    return {
        "as_of_utc": fmt_iso_utc(ts_now),
        "events": [],
        "error": "; ".join(errors),
    }
