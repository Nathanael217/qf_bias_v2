"""
collectors/macro.py — Macro data collector: central bank rates + surprise structure.

Sources:
  Policy rates → FRED (St. Louis Fed) REST API.
      URL: https://api.stlouisfed.org/fred/series/observations
      Auth: FRED_API_KEY from Streamlit secrets or env var.
  Rate differentials → computed in-module from policy rates.
  Surprise z-scores → computed from calendar-fed actual/forecast pairs.

FRED series used (one per currency):
  USD : DFEDTARU   — FOMC Fed Funds Rate Upper Target
  EUR : ECBDFR     — ECB Deposit Facility Rate
  GBP : BOERUKM    — Bank of England Official Bank Rate
  JPY : IRSTCI01JPM156N — Japan Overnight Call Money Rate (policy proxy)
  AUD : RBATCTR    — Reserve Bank of Australia Cash Rate Target
  NZD : RBNZOCR    — RBNZ Official Cash Rate
  CAD : BOCR       — Bank of Canada Overnight Rate
  CHF : SARON      — Swiss Average Rate ON (SNB operational target proxy)
         ↑ Note: SNB targets SARON corridor; SARON is the market realisation.
           If SARON unavailable in FRED, fallback to IR3TIB01CHM156N (3M CHF IBOR).

XAU, BTC, ETH have no policy rate → their "rate" entries are null / omitted.

Schema returned (§4 macro):
{
  "as_of_utc": "...",
  "rates": {
    "USD": 4.55, "EUR": 2.25, "GBP": 4.10, "JPY": 0.50,
    "AUD": 3.85, "NZD": 3.00, "CAD": 3.25, "CHF": 1.00
  },
  "rate_diff": {
    "EURUSD": -2.30, "GBPUSD": -0.45, ...   (base_rate - quote_rate per pair)
  },
  "surprises": {
    "USD": [
      {"event": "Core PCE m/m", "actual": 0.30, "forecast": 0.26,
       "z": 1.2, "ts_utc": "..."}
    ]
  },
  "_meta": {"sources_ok": [...], "sources_failed": [...]}
}

Failure contract:
  - FRED key missing → rates all null, flag in _meta.
  - Per-currency FRED failure → that currency rate null, others unaffected.
  - surprises → always a dict of lists (empty list if no data fed in).
  - Never raises to caller.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from utils.timeutils import fmt_iso_utc, now_utc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FRED series IDs per currency
# ---------------------------------------------------------------------------

# (series_id, human_description)
# Per currency: daftar KANDIDAT series_id (coba berurutan sampai ada yang berhasil).
# ID OECD "IRSTCB01{cc}M156N" = immediate/overnight central bank rate (andal lintas negara).
# CATATAN: RBATCTR/RBNZOCR/BOCR/SARON dari versi lama TERBUKTI 400 "series does not exist"
# (dikonfirmasi via FRED API) → diganti. Yang gagal tetap graceful (null), tidak crash.
_FRED_SERIES_CANDIDATES: dict[str, list[tuple[str, str]]] = {
    # USD/EUR pakai policy rate resmi (terbukti jalan di log). Lainnya pakai
    # IRSTCI01 (call money/immediate, OECD) — paling konsisten ada lintas negara.
    "USD": [("DFEDTARU", "Fed Funds Upper Target")],
    "EUR": [("ECBDFR", "ECB Deposit Facility")],
    "GBP": [("IRSTCI01GBM156N", "UK call money OECD"),
            ("IR3TIB01GBM156N", "UK 3M interbank OECD")],
    "JPY": [("IRSTCI01JPM156N", "Japan call money OECD")],
    "AUD": [("IRSTCI01AUM156N", "Australia call money OECD"),
            ("IR3TIB01AUM156N", "Australia 3M interbank OECD")],
    "NZD": [("IRSTCI01NZM156N", "NZ call money OECD"),
            ("IR3TIB01NZM156N", "NZ 3M interbank OECD")],
    "CAD": [("IRSTCI01CAM156N", "Canada call money OECD"),
            ("IR3TIB01CAM156N", "Canada 3M interbank OECD")],
    "CHF": [("IRSTCI01CHM156N", "Swiss call money OECD"),
            ("IR3TIB01CHM156N", "Swiss 3M interbank OECD")],
}

# Kompat: tetap ekspos _FRED_SERIES (id utama) untuk kode lain yang mungkin refer.
_FRED_SERIES: dict[str, tuple[str, str]] = {
    ccy: cands[0] for ccy, cands in _FRED_SERIES_CANDIDATES.items()
}

# Currencies with no policy rate (no FRED series)
_NO_RATE: set[str] = {"XAU", "BTC", "ETH"}

# Pairs for which we compute rate differentials (base, quote)
_RATE_DIFF_PAIRS: list[tuple[str, str]] = [
    ("EUR", "USD"),
    ("GBP", "USD"),
    ("USD", "JPY"),
    ("AUD", "USD"),
    ("NZD", "USD"),
    ("USD", "CAD"),
    ("USD", "CHF"),
    ("XAU", "USD"),   # Gold vs USD — XAU rate will be null; diff = null
    ("BTC", "USD"),
    ("ETH", "USD"),
]

_FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
_FRED_TIMEOUT = 12          # seconds per request
_FRED_RETRIES = 1           # retries on transient failure


# ---------------------------------------------------------------------------
# FRED API key helper
# ---------------------------------------------------------------------------

def _get_fred_key() -> str | None:
    """
    Resolve FRED API key from (in priority order):
      1. Streamlit st.secrets["FRED_API_KEY"]
      2. Environment variable FRED_API_KEY
    Returns None if neither is available.
    """
    # Try Streamlit secrets first (safe import — may not be installed)
    try:
        import streamlit as st  # type: ignore
        key = st.secrets.get("FRED_API_KEY")
        if key:
            return str(key)
    except Exception:
        pass

    # Fallback to environment variable
    return os.environ.get("FRED_API_KEY") or None


# ---------------------------------------------------------------------------
# FRED single-series fetch
# ---------------------------------------------------------------------------

def _fetch_fred_latest(
    series_id: str,
    api_key: str,
    session: requests.Session,
    retries: int = _FRED_RETRIES,
) -> float | None:
    """
    Fetch the most recent observation value for a FRED series.

    FRED API docs: https://fred.stlouisfed.org/docs/api/fred/series_observations.html
    Uses sort_order=desc&limit=5 to get latest, skips "." (missing) values.

    Returns float rate value or None on failure.
    """
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 5,           # fetch a few to skip over missing "." values
        "observation_start": "2020-01-01",  # broad enough for any rate series
    }

    attempt = 0
    while attempt <= retries:
        try:
            resp = session.get(
                _FRED_BASE_URL, params=params, timeout=_FRED_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()

            observations = data.get("observations", [])
            for obs in observations:
                val_str = obs.get("value", ".")
                if val_str != ".":
                    return float(val_str)

            logger.warning("FRED %s: all recent obs are missing ('.')", series_id)
            return None

        except requests.exceptions.Timeout:
            logger.warning("FRED %s timeout (attempt %d/%d)", series_id, attempt + 1, retries + 1)
        except requests.exceptions.HTTPError as exc:
            code = getattr(exc.response, "status_code", None)
            if code == 429:      # rate-limited → tunggu & retry
                logger.warning("FRED %s 429 — tunggu 2s & retry", series_id)
                time.sleep(2.0)
                attempt += 1
                continue
            logger.error("FRED %s HTTP error: %s", series_id, exc)
            return None          # 4xx lain → no retry
        except Exception as exc:
            logger.error("FRED %s unexpected error: %s", series_id, exc)

        attempt += 1
        if attempt <= retries:
            time.sleep(1.5)

    return None


# ---------------------------------------------------------------------------
# Rate differential helper
# ---------------------------------------------------------------------------

def _compute_rate_diffs(
    rates: dict[str, float | None]
) -> dict[str, float | None]:
    """
    Compute rate_diff for each pair: base_rate − quote_rate.
    Returns None for a pair if either leg's rate is unavailable.
    """
    diffs: dict[str, float | None] = {}
    for base, quote in _RATE_DIFF_PAIRS:
        pair = f"{base}{quote}"
        r_base = rates.get(base)
        r_quote = rates.get(quote)
        if r_base is not None and r_quote is not None:
            diffs[pair] = round(r_base - r_quote, 4)
        else:
            diffs[pair] = None
    return diffs


# ---------------------------------------------------------------------------
# Surprise z-score computation
# ---------------------------------------------------------------------------

def _compute_surprise_z(
    actual: float,
    forecast: float,
    historical_std: float | None,
) -> float | None:
    """
    Compute z-score surprise: (actual - forecast) / historical_std.

    If historical_std is None or zero, returns None (no normalization possible).
    Caller must supply historical_std from their own dataset.
    """
    if historical_std is None or abs(historical_std) < 1e-9:
        return None
    return round((actual - forecast) / historical_std, 3)


def build_surprises(
    calendar_events: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """
    Convert a list of released calendar events into the schema §4 surprises dict.

    Expected input per event (from collectors/calendar_evt.py or manual feed):
    {
        "currency":      "USD",
        "name":          "Core PCE m/m",
        "actual":        0.30,
        "forecast":      0.26,
        "historical_std": 0.15,   # optional — caller supplies from own history
        "ts_utc":        "2026-06-01T12:30:00Z"
    }

    Returns:
    {
        "USD": [{"event": "Core PCE m/m", "actual": 0.30, "forecast": 0.26,
                 "z": 1.2, "ts_utc": "..."}],
        ...
    }

    Rules:
      - Events without actual value (actual is None) are skipped.
      - Events without forecast get z=null.
      - historical_std defaults to (actual - forecast) raw delta if not provided
        → z = sign(actual - forecast) [unit-free directional, NOT magnitude-correct]
        → clearly flagged so caller knows to replace with real std.

    NOTE (§0 Prinsip 9): z-scores here are STRUCTURAL PLACEHOLDERS.
    Proper historical_std must be computed from a real economic data series
    before this drives sizing. JANGAN pakai z tanpa validasi.
    """
    surprises: dict[str, list[dict[str, Any]]] = {}

    for ev in calendar_events:
        currency = ev.get("currency")
        actual   = ev.get("actual")
        forecast = ev.get("forecast")
        name     = ev.get("name", "Unknown Event")
        ts_utc   = ev.get("ts_utc", "")

        # Skip events not yet released
        if actual is None:
            continue

        historical_std = ev.get("historical_std")
        polarity = float(ev.get("surprise_polarity", 1.0))  # +1 normal, -1 terbalik (unemployment)

        # Compute z
        if forecast is not None:
            z = _compute_surprise_z(
                float(actual), float(forecast), historical_std
            )
            # Fallback: directional sign if no std
            if z is None and forecast is not None:
                raw_delta = float(actual) - float(forecast)
                # Annotate as directional-only (no proper normalization)
                z = round(raw_delta, 4)
                logger.debug(
                    "%s %s: no historical_std — z is raw delta (not normalised)",
                    currency, name,
                )
            # Polarity: untuk indikator terbalik (mis. Unemployment Rate), beat = bearish.
            if z is not None:
                z = round(z * polarity, 4)
        else:
            z = None

        entry = {
            "event":    name,
            "actual":   float(actual),
            "forecast": float(forecast) if forecast is not None else None,
            "z":        z,
            "ts_utc":   ts_utc,
        }

        surprises.setdefault(currency, []).append(entry)

    return surprises


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_macro(
    calendar_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Fetch central bank policy rates from FRED and compute rate differentials.

    Args:
        calendar_events: Optional list of released calendar events (from
            collectors/calendar_evt.py) used to populate surprises.
            If None or empty, surprises dict contains empty lists.

    Returns schema-§4-compliant dict. Never raises.
    """
    ts = now_utc()
    result: dict[str, Any] = {
        "as_of_utc": fmt_iso_utc(ts),
        "rates": {},
        "rate_diff": {},
        "surprises": {},
        "_meta": {"sources_ok": [], "sources_failed": []},
    }

    api_key = _get_fred_key()
    if not api_key:
        logger.error("FRED_API_KEY not found — all rates will be null")
        result["_meta"]["sources_failed"].append("FRED (no API key)")
        # Still compute diffs (all None) and surprises structure
        result["rates"] = {ccy: None for ccy in _FRED_SERIES}
        result["rate_diff"] = _compute_rate_diffs(result["rates"])
        result["surprises"] = build_surprises(calendar_events or [])
        return result

    # --- Fetch rates from FRED ------------------------------------------------
    session = requests.Session()
    session.headers.update({"User-Agent": "qf_bias/1.0 (research tool)"})

    fred_ok: list[str] = []
    fred_fail: list[str] = []

    import time as _t
    for currency, candidates in _FRED_SERIES_CANDIDATES.items():
        val = None
        used_id = None
        for series_id, _desc in candidates:
            _t.sleep(0.6)   # jeda anti-429 (FRED throttle); total ~8 currency × 0.6s ≈ 5s
            val = _fetch_fred_latest(series_id, api_key, session)
            if val is not None:
                used_id = series_id
                break  # kandidat berhasil → stop
            logger.warning("FRED %s untuk %s gagal/404 — coba kandidat berikutnya", series_id, currency)
        result["rates"][currency] = val
        if val is not None:
            fred_ok.append(f"{currency}({used_id})")
        else:
            fred_fail.append(f"{currency}(semua kandidat gagal)")
            logger.warning("Rate %s: SEMUA kandidat FRED gagal", currency)

    if fred_ok:
        result["_meta"]["sources_ok"].append(f"FRED:{','.join(fred_ok)}")
    if fred_fail:
        result["_meta"]["sources_failed"].append(f"FRED:{','.join(fred_fail)}")

    # --- Rate differentials ---------------------------------------------------
    result["rate_diff"] = _compute_rate_diffs(result["rates"])

    # --- Surprises (from caller-provided calendar data) -----------------------
    result["surprises"] = build_surprises(calendar_events or [])

    logger.info(
        "get_macro() done — rates OK: %d, failed: %d",
        len(fred_ok), len(fred_fail),
    )
    return result


# ---------------------------------------------------------------------------
# FRED series reference (for future maintenance)
# ---------------------------------------------------------------------------
#
# How to verify / update series IDs:
#   https://fred.stlouisfed.org/series/{series_id}
#   e.g. https://fred.stlouisfed.org/series/DFEDTARU
#
# CHF / SARON caveat:
#   If FRED drops SARON coverage, fallback candidates:
#     IR3TIB01CHM156N  (3-month CHF IBOR, monthly)
#     IRSTCI01CHM156N  (overnight call money rate, monthly)
#   These are monthly → less precise but directionally correct for rate regime.
#
# JPY caveat:
#   IRSTCI01JPM156N is monthly frequency. BoJ policy rate is near-zero /
#   occasionally negative. For daily precision, BoJ publishes call rates at:
#   https://www.boj.or.jp/en/statistics/  — but no direct FRED daily series.
#   Monthly proxy is sufficient for rate differential (regime context).
#
# Surprise z-score design note (§0 Prinsip 9):
#   v1 ships build_surprises() as a STRUCTURAL placeholder.
#   Proper historical_std should come from ~3yr rolling std of actual-minus-
#   consensus for each economic release (e.g. 36-month BLS CPI surprise history).
#   Until that dataset exists, caller can pass historical_std=None to get
#   directional-only z (raw delta, not normalised). Label clearly in UI.
