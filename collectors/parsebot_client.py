"""parse.bot client — on-demand (click-to-run) fetch dengan kontrol kredit.

Filosofi kredit (free tier = 200 kredit/bulan, no rollover):
- TIDAK pernah auto-call saat load/rerun. Hanya saat tombol diklik.
- st.cache_data TTL → klik ulang dalam TTL = 0 kredit (serve dari cache).
- Penghitung kredit per-sesi supaya user lihat burn-nya.

CATATAN kredit (dari riset, BUKAN dokumen resmi parse.bot — verifikasi di dashboard):
- Free tier 200 kredit. Buat scraper baru ~75 kredit, edit ~50 kredit.
- API call = VARIABEL: situs anti-bot berat (ForexFactory/myfxbook/investing)
  BISA > 1 kredit/call. Marketplace bilang "1 kredit = 1 call" untuk kasus simpel.
- Reset kemungkinan bulanan (framing "credits/month"), tapi konfirmasi di akunmu.
"""
from __future__ import annotations

import json
import time
from typing import Any

import requests

_BASE = "https://api.parse.bot/scraper"
_TIMEOUT = 30
_TTL_CEILING = 604_800  # 7 hari — plafon; expiry sebenarnya dikontrol bucket

# Scraper IDs (dari marketplace + URL scraper custom-mu)
SCRAPERS: dict[str, str] = {
    "forexfactory": "0d3aa2e2-80b6-42dc-986a-d7f0845f4deb",
    "myfxbook": "09664a39-3a46-41b8-852d-1286994ec995",
    "tradingeconomics": "25322cd0-d8e5-4665-bbf2-a358ae8a7601",
    "a1edge": "57dca8fa-f379-42fd-9230-109de0cac79b",
    "sentiment_custom": "57dca8fa-f379-42fd-9230-109de0cac79b",  # alias lama
}

# Nama mata-uang tunggal di A1 retail → kode ISO (entri per-currency, ideal utk faktor D)
A1_CCY_NAMES = {
    "US-DOLLAR": "USD", "EURO": "EUR", "JP-YEN": "JPY", "GB-POUND": "GBP",
    "CH-FRANC": "CHF", "AU-DOLLAR": "AUD", "CA-DOLLAR": "CAD", "NZ-DOLLAR": "NZD",
    "GOLD": "XAU", "BITCOIN": "BTC", "Ethereum": "ETH",
}


def _api_key() -> str | None:
    try:
        import streamlit as st  # type: ignore
        return st.secrets.get("PARSE_API_KEY") or st.secrets.get("parse_api_key")
    except Exception:
        return None


def _bump_credit() -> None:
    """Hitung call nyata (cache miss) di sesi ini."""
    try:
        import streamlit as st  # type: ignore
        st.session_state["parsebot_calls"] = st.session_state.get("parsebot_calls", 0) + 1
    except Exception:
        pass


def calls_this_session() -> int:
    try:
        import streamlit as st  # type: ignore
        return int(st.session_state.get("parsebot_calls", 0))
    except Exception:
        return 0


def _raw(scraper_id: str, endpoint: str, params: dict[str, Any] | None, key: str) -> dict:
    url = f"{_BASE}/{scraper_id}/{endpoint}"
    resp = requests.get(
        url,
        headers={"X-API-Key": key, "Accept": "application/json"},
        params=params or {},
        timeout=_TIMEOUT,
    )
    if resp.status_code == 401:
        raise RuntimeError(
            "401 Unauthorized — API key ditolak. Cek: (1) bukan placeholder '$PARSE_API_KEY', "
            "(2) tak ada spasi/kutip nyangkut, (3) key benar dari parse.bot/settings & milik akunmu."
        )
    if resp.status_code == 403:
        raise RuntimeError("403 Forbidden — key valid tapi tak punya akses scraper ini "
                           "(perlu Subscribe scraper-nya di marketplace?).")
    if resp.status_code == 429:
        raise RuntimeError("429 — rate limit (5 req/menit di free tier). Tunggu sebentar.")
    resp.raise_for_status()
    return resp.json()


# --- cached layer (didefinisikan di module-level agar cache persisten) ---
try:
    import streamlit as st  # type: ignore

    @st.cache_data(ttl=_TTL_CEILING, show_spinner=False)
    def _cached_fetch(scraper_id: str, endpoint: str, params_json: str,
                      _key: str, _bucket: int) -> dict:
        # _bucket berubah tiap kelipatan TTL → memaksa cache miss saat expired.
        # Hanya jalan saat cache MISS = 1 call nyata = hitung kredit.
        _bump_credit()
        return _raw(scraper_id, endpoint, json.loads(params_json), _key)

    _HAS_ST = True
except Exception:
    _HAS_ST = False


def fetch(scraper_id: str, endpoint: str,
          params: dict[str, Any] | None = None,
          ttl: int = 21_600) -> dict:
    """Ambil data parse.bot. Cache-hit dalam `ttl` detik = 0 kredit.

    ttl default 6 jam. Raise RuntimeError kalau PARSE_API_KEY belum di-set.
    """
    key = _api_key()
    if not key:
        raise RuntimeError("PARSE_API_KEY belum di-set di Streamlit Secrets.")
    if not _HAS_ST:
        _bump_credit()
        return _raw(scraper_id, endpoint, params, key)
    bucket = int(time.time() // max(int(ttl), 1))
    return _cached_fetch(
        scraper_id, endpoint,
        json.dumps(params or {}, sort_keys=True),
        key, bucket,
    )


# ---------------------------------------------------------------------------
# Parser sumber yang skemanya SUDAH diketahui (dari marketplace)
# ---------------------------------------------------------------------------
def parse_ff_calendar(resp: dict) -> list[dict]:
    """ForexFactory get_calendar → list event {date,time,currency,impact,actual,forecast,previous,name}."""
    data = (resp or {}).get("data", {})
    events = data.get("events", []) if isinstance(data, dict) else []
    out = []
    for e in events:
        if not isinstance(e, dict):
            continue
        out.append({
            "name": e.get("name", ""),
            "currency": (e.get("currency") or "").upper(),
            "impact": (e.get("impact") or "").lower(),
            "actual": e.get("actual", ""),
            "forecast": e.get("forecast", ""),
            "previous": e.get("previous", ""),
            "date": e.get("date", ""),
            "time": e.get("time", ""),
            "id": e.get("id"),
        })
    return out


def parse_myfxbook_rates(resp: dict) -> list[dict]:
    """myfxbook get_interest_rates → list {bank,country,current_rate,previous_rate,change,last_meeting}."""
    data = (resp or {}).get("data", [])
    if isinstance(data, dict):
        data = data.get("rates") or data.get("data") or []
    out = []
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict):
            continue
        out.append({
            "bank": r.get("central_bank_abbr") or r.get("central_bank_name", ""),
            "country": r.get("country", ""),
            "current_rate": r.get("current_rate"),
            "previous_rate": r.get("previous_rate"),
            "change": r.get("change"),
            "last_meeting": r.get("last_meeting", ""),
        })
    return out


# ---------------------------------------------------------------------------
# Parser A1 EdgeFinder (skema dari JSON live yang divalidasi user 2026-06-04)
# ---------------------------------------------------------------------------
def parse_a1_retail(resp: dict) -> dict:
    """get_retail_sentiment → {per_currency:{CCY:{long_pct,short_pct,signal}}, per_pair:{...}}.

    per_currency dipakai untuk faktor D (long% MENTAH; engine yang hitung kontrarian).
    'signal' disimpan utk display saja — JANGAN dipakai sbg skor (itu verdict A1).
    """
    pairs = (resp or {}).get("data", {}).get("pairs", [])
    per_ccy, per_pair = {}, {}
    for p in pairs if isinstance(pairs, list) else []:
        if not isinstance(p, dict):
            continue
        name = p.get("pair", "")
        slot = {"long_pct": p.get("long_percentage"),
                "short_pct": p.get("short_percentage"),
                "signal": p.get("signal", "")}
        if name in A1_CCY_NAMES:
            per_ccy[A1_CCY_NAMES[name]] = slot
        else:
            per_pair[name] = slot
    return {"per_currency": per_ccy, "per_pair": per_pair}


def parse_a1_cot(resp: dict) -> dict:
    """get_cot_report → {ASSET:{long_pct,short_pct,net_pct}} (non-commercial = smart money)."""
    assets = (resp or {}).get("data", {}).get("assets", [])
    out = {}
    for a in assets if isinstance(assets, list) else []:
        if isinstance(a, dict) and a.get("asset"):
            out[a["asset"]] = {
                "long_pct": a.get("non_commercial_long_pct"),
                "short_pct": a.get("non_commercial_short_pct"),
                "net_pct": a.get("net_position_pct"),
            }
    return out


def parse_a1_rates(resp: dict) -> list[dict]:
    """get_interest_rates → list {currency,current_rate,previous_rate,bank,last_change}."""
    rates = (resp or {}).get("data", {}).get("rates", [])
    out = []
    for r in rates if isinstance(rates, list) else []:
        if isinstance(r, dict):
            out.append({
                "currency": r.get("currency"),
                "current_rate": r.get("current_rate"),
                "previous_rate": r.get("previous_rate"),
                "bank": r.get("central_bank", ""),
                "last_change": r.get("last_change_date", ""),
            })
    return out


def parse_a1_simple(resp: dict, value_key: str) -> dict:
    """Endpoint per-negara (inflation/growth/labor) → {CCY: current_value}."""
    rows = (resp or {}).get("data", {}).get("countries", [])
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("currency"):
            out[r["currency"]] = r.get(value_key)
    return out


def parse_a1_strength(resp: dict) -> dict:
    """get_currency_heatmap → {CCY: avg_change_pct} (kekuatan harga = lensa TERPISAH, bukan bias)."""
    pairs = (resp or {}).get("data", {}).get("pairs", [])
    agg: dict[str, float] = {}
    cnt: dict[str, int] = {}
    for p in pairs if isinstance(pairs, list) else []:
        if not isinstance(p, dict):
            continue
        b = p.get("base_currency")
        ch = p.get("change_pct")
        if b is None or ch is None:
            continue
        agg[b] = agg.get(b, 0.0) + float(ch)
        cnt[b] = cnt.get(b, 0) + 1
    return {c: round(agg[c] / cnt[c], 3) for c in agg if cnt[c]}


def rates_look_like_cpi(rates_resp: dict, infl_resp: dict) -> bool:
    """GUARD: True kalau get_interest_rates ternyata duplikat CPI (bug scraper).

    Kalau True → JANGAN feed ke rate_diff (akan korupsi R_hard 0.60).
    """
    try:
        r = {x["currency"]: float(x["current_rate"])
             for x in rates_resp["data"]["rates"] if x.get("currency")}
        c = {x["currency"]: float(x["current_cpi"])
             for x in infl_resp["data"]["countries"] if x.get("currency")}
        common = set(r) & set(c)
        if len(common) < 4:
            return False
        matches = sum(1 for k in common if abs(r[k] - c[k]) < 1e-9)
        return matches == len(common)
    except Exception:
        return False
