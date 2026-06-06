"""
collectors/retail.py — Retail sentiment aggregator (3 sumber).

Output schema (sesuai arsitektur §4):
{
  "as_of_utc": "2026-06-01T07:00:00Z",
  "sources_ok": ["myfxbook", "fxssi"],
  "sources_failed": ["dukascopy"],
  "retail": {
    "EURUSD": {
      "by_source": {"myfxbook": 62.0, "fxssi": 58.0},
      "long_pct_agg": 60.0,
      "agreement": 0.92
    },
    "XAUUSD": {
      "by_source": {"myfxbook": 70.0},
      "long_pct_agg": 70.0,
      "agreement": 1.0
    }
  }
}

SUMBER:
  A. Myfxbook — https://www.myfxbook.com/api/get-community-outlook.json
     Docs: https://www.myfxbook.com/help/api
     Login: POST https://www.myfxbook.com/api/login.json?email=...&password=...
     Response login: {"error":false,"session":"..."}
     Outlook: GET  https://www.myfxbook.com/api/get-community-outlook.json?session=...
     Response: {"error":false,"symbols":[{"name":"EURUSD","shortPercentage":38,"longPercentage":62,...},...]}

  B. FXSSI — https://fxssi.com/current-ratio
     Halaman publik. Data diserve sebagai HTML + inline JS / JSON.
     Selector target: script tag atau data-* attribute yang berisi array pasangan + long%.
     Alternatif: undocumented JSON endpoint https://fxssi.com/wp-json/fxssi-crc/v1/chart-data
     (ditemukan dari network tab browser — tidak dijamin stabil, bungkus try/except).

  C. Dukascopy SWFX — https://www.dukascopy.com/trading-tools/widgets/sentiment/sentiment.php
     Widget publik, update ~30 menit. Query param: ?a=json
     Response: JSON array [{"s":"EUR/USD","long":62,"short":38}, ...]
     Alternatif widget endpoint:
       https://www.dukascopy.com/trading-tools/widgets/quotes/sentiment.php?pairs=EUR/USD,...
     Beberapa versi pakai iframe; fallback ke scrape HTML bila JSON gagal.

CATATAN GRANULARITAS (arsitektur §3.2):
  - Data native PER-PAIR. Agregasi ke currency dilakukan di engine/scoring.py, BUKAN di sini.
  - Collector ini hanya return raw long_pct per pair yang tersedia.
  - Pasangan yang tidak tersedia dari sumber manapun = tidak masuk dict retail.
"""

from __future__ import annotations

import logging
import re
import json
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

from utils.timeutils import fmt_iso_utc, now_utc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanta
# ---------------------------------------------------------------------------

_TIMEOUT: int = 10  # detik per request
_MAX_RETRY: int = 2
_RETRY_BACKOFF: float = 1.5  # detik antar retry

# Pasangan yang di-track (sesuai config.PAIRS — tanpa BTC/ETH karena retail data pair-nya berbeda)
_TRACKED_PAIRS: list[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    "NZDUSD", "USDCAD", "USDCHF", "XAUUSD",
    "BTCUSD", "ETHUSD",
]

# Normalisasi nama pair dari berbagai sumber ke format XXXYYY
_PAIR_ALIAS: dict[str, str] = {
    # Myfxbook kadang pakai nama dengan spasi atau XAU/USD
    "XAU/USD": "XAUUSD",
    "BTC/USD": "BTCUSD",
    "ETH/USD": "ETHUSD",
    # FXSSI & Dukascopy pakai EUR/USD
    "EUR/USD": "EURUSD",
    "GBP/USD": "GBPUSD",
    "USD/JPY": "USDJPY",
    "AUD/USD": "AUDUSD",
    "NZD/USD": "NZDUSD",
    "USD/CAD": "USDCAD",
    "USD/CHF": "USDCHF",
    # Dukascopy kadang pakai spasi
    "EUR USD": "EURUSD",
    "GBP USD": "GBPUSD",
    "USD JPY": "USDJPY",
    "AUD USD": "AUDUSD",
    "NZD USD": "NZDUSD",
    "USD CAD": "USDCAD",
    "USD CHF": "USDCHF",
    "XAU USD": "XAUUSD",
}


def _normalize_pair(raw: str) -> str | None:
    """Normalisasi nama pair ke format XXXYYY (6–7 karakter, uppercase, tanpa separator).

    Return None kalau pair tidak ada di tracking list.
    """
    clean = raw.upper().strip()
    # Cek alias dulu
    if clean in _PAIR_ALIAS:
        clean = _PAIR_ALIAS[clean]
    # Coba hapus separator bila masih ada
    clean_nosep = clean.replace("/", "").replace(" ", "").replace("-", "")
    if clean_nosep in _TRACKED_PAIRS:
        return clean_nosep
    if clean in _TRACKED_PAIRS:
        return clean
    return None


# ---------------------------------------------------------------------------
# HTTP helper dengan retry
# ---------------------------------------------------------------------------

def _get_with_retry(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _TIMEOUT,
    max_retry: int = _MAX_RETRY,
    http: "requests.Session | None" = None,
) -> requests.Response:
    """GET request dengan retry + exponential backoff sederhana.

    http: kalau diberi requests.Session, dipakai untuk reuse koneksi (keep-alive)
          → login & outlook myfxbook keluar via 1 koneksi/IP yang sama (session IP-bound).
    Raises requests.RequestException setelah semua retry habis.
    """
    client = http or requests
    default_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/html, */*",
        "Connection": "keep-alive",
    }
    if headers:
        default_headers.update(headers)

    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(max_retry):
        try:
            resp = client.get(
                url,
                params=params,
                headers=default_headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < max_retry - 1:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
            logger.debug("Retry %d/%d for %s: %s", attempt + 1, max_retry, url, exc)
    raise last_exc


# ---------------------------------------------------------------------------
# SOURCE A — Myfxbook
# ---------------------------------------------------------------------------

_MYFXBOOK_LOGIN_URL: str = "https://www.myfxbook.com/api/login.json"
_MYFXBOOK_OUTLOOK_URL: str = "https://www.myfxbook.com/api/get-community-outlook.json"


def _myfxbook_login(email: str, password: str, http: "requests.Session | None" = None) -> str:
    """Login ke Myfxbook API dan return session token.

    Raises RuntimeError bila login gagal atau respons tidak valid.
    API: GET https://www.myfxbook.com/api/login.json?email=X&password=Y
    Response: {"error":false,"session":"abc123..."}
    """
    resp = _get_with_retry(
        _MYFXBOOK_LOGIN_URL,
        params={"email": email, "password": password},
        http=http,
    )
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Myfxbook login failed: {data.get('message', 'unknown error')}")
    session: str | None = data.get("session")
    if not session:
        raise RuntimeError("Myfxbook login: session token tidak ada dalam respons")
    return session


def _fetch_myfxbook(session: str, http: "requests.Session | None" = None) -> dict[str, float]:
    """Fetch long% per pair dari Myfxbook Community Outlook.

    Args:
        session: Session token dari _myfxbook_login().

    Returns:
        dict pair → long_pct, contoh: {"EURUSD": 62.0, "GBPUSD": 45.5}

    API: GET https://www.myfxbook.com/api/get-community-outlook.json?session=...
    Response shape:
        {
          "error": false,
          "symbols": [
            {
              "name": "EURUSD",
              "longPercentage": 62,
              "shortPercentage": 38,
              "longVolume": 12345,
              "shortVolume": 7654,
              "longPositions": 100,
              "shortPositions": 62
            },
            ...
          ]
        }
    Catatan: name biasanya UPPERCASE tanpa separator. XAU dipresentasikan sbg "XAUUSD" atau "Gold".
    """
    resp = _get_with_retry(_MYFXBOOK_OUTLOOK_URL, params={"session": session}, http=http)
    data = resp.json()

    if data.get("error"):
        raise RuntimeError(f"Myfxbook outlook error: {data.get('message', 'unknown')}")

    symbols: list[dict] = data.get("symbols", [])
    result: dict[str, float] = {}
    for sym in symbols:
        raw_name: str = str(sym.get("name", ""))
        pair = _normalize_pair(raw_name)
        if pair is None:
            continue
        long_pct = sym.get("longPercentage")
        if long_pct is None:
            continue
        try:
            result[pair] = float(long_pct)
        except (TypeError, ValueError):
            logger.debug("Myfxbook: longPercentage tidak bisa dikonversi untuk %s", raw_name)
    return result


# ---------------------------------------------------------------------------
# SOURCE B — FXSSI
# ---------------------------------------------------------------------------

# Endpoint JSON tidak-resmi yang ditemukan via network tab browser.
# URL ini bisa berubah sewaktu-waktu — bungkus try/except, fallback ke HTML scrape.
_FXSSI_JSON_URL: str = "https://fxssi.com/wp-json/fxssi-crc/v1/chart-data"
_FXSSI_PAGE_URL: str = "https://fxssi.com/current-ratio"


def _fetch_fxssi_json() -> dict[str, float]:
    """Coba ambil data FXSSI dari undocumented JSON endpoint.

    Response shape (berdasarkan inspeksi network):
        {
          "EURUSD": {"long": 62.5, "short": 37.5},
          "GBPUSD": {"long": 45.0, "short": 55.0},
          ...
        }
    ATAU array:
        [{"pair": "EURUSD", "longPercent": 62.5}, ...]

    Karena endpoint tidak terdokumentasi, kita coba dua shape.
    Raises RuntimeError bila tidak berhasil parse.
    """
    resp = _get_with_retry(_FXSSI_JSON_URL)
    raw = resp.json()

    result: dict[str, float] = {}

    # Shape 1: dict keyed by pair symbol
    if isinstance(raw, dict):
        for key, val in raw.items():
            pair = _normalize_pair(key)
            if pair is None:
                continue
            # val bisa {"long": 62.5, "short": 37.5} atau {"longPercent": 62.5}
            if isinstance(val, dict):
                long_pct = (
                    val.get("long")
                    or val.get("longPercent")
                    or val.get("long_percent")
                    or val.get("longPercentage")
                )
                if long_pct is not None:
                    try:
                        result[pair] = float(long_pct)
                    except (TypeError, ValueError):
                        pass
            elif isinstance(val, (int, float)):
                # langsung angka long%
                result[pair] = float(val)

    # Shape 2: list of dicts
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            raw_name = str(item.get("pair") or item.get("symbol") or item.get("name") or "")
            pair = _normalize_pair(raw_name)
            if pair is None:
                continue
            long_pct = (
                item.get("long")
                or item.get("longPercent")
                or item.get("long_percent")
                or item.get("longPercentage")
            )
            if long_pct is not None:
                try:
                    result[pair] = float(long_pct)
                except (TypeError, ValueError):
                    pass

    if not result:
        raise RuntimeError("FXSSI JSON: tidak ada data yang berhasil diparsing")
    return result


def _fetch_fxssi_html() -> dict[str, float]:
    """Fallback: scrape HTML halaman https://fxssi.com/current-ratio.

    Strategi scraping (robust, multi-pattern):
    1. Cari <script> tag yang mengandung JSON array data.
    2. Cari elemen dengan class/data-* yang berisi long%.
    3. Cari tabel <tr> dengan kolom pair + long%.

    HTML structure yang diketahui (diperkirakan — BISA BERUBAH):
        <div class="cr-item" data-pair="EURUSD" data-long="62.5" data-short="37.5">
        ATAU
        <script>var chartData = [{"pair":"EURUSD","long":62.5},...];</script>

    Raises RuntimeError bila semua strategi gagal.
    """
    resp = _get_with_retry(_FXSSI_PAGE_URL, headers={"Accept": "text/html"})
    soup = BeautifulSoup(resp.text, "lxml")

    result: dict[str, float] = {}

    # Strategi 1: cari inline JSON di <script>
    for script_tag in soup.find_all("script"):
        text: str = script_tag.get_text()
        # Cari pola: var ... = [{"pair":...,"long":...}]
        # atau window.__DATA__ = {...}
        candidates = re.findall(
            r'(?:var\s+\w+|window\.\w+)\s*=\s*(\[.+?\]|\{.+?\})\s*;',
            text,
            re.DOTALL,
        )
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            # Coba parse sebagai list
            if isinstance(parsed, list):
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    raw_name = str(
                        item.get("pair") or item.get("symbol") or item.get("name") or ""
                    )
                    pair = _normalize_pair(raw_name)
                    if pair is None:
                        continue
                    long_pct = item.get("long") or item.get("longPercent") or item.get("long_percent")
                    if long_pct is not None:
                        try:
                            result[pair] = float(long_pct)
                        except (TypeError, ValueError):
                            pass
            # Coba parse sebagai dict
            elif isinstance(parsed, dict):
                for key, val in parsed.items():
                    pair = _normalize_pair(key)
                    if pair is None:
                        continue
                    if isinstance(val, dict):
                        long_pct = val.get("long") or val.get("longPercent")
                        if long_pct is not None:
                            try:
                                result[pair] = float(long_pct)
                            except (TypeError, ValueError):
                                pass

    if result:
        return result

    # Strategi 2: data-* attribute
    for el in soup.find_all(attrs={"data-pair": True}):
        raw_name = el.get("data-pair", "")
        pair = _normalize_pair(raw_name)
        if pair is None:
            continue
        long_pct_str = el.get("data-long") or el.get("data-long-percent")
        if long_pct_str:
            try:
                result[pair] = float(long_pct_str)
            except (TypeError, ValueError):
                pass

    if result:
        return result

    # Strategi 3: tabel dengan kolom yang relevan
    for table in soup.find_all("table"):
        headers_row = table.find("tr")
        if not headers_row:
            continue
        headers_text = [th.get_text(strip=True).lower() for th in headers_row.find_all(["th", "td"])]
        # Cari indeks kolom pair dan long%
        pair_idx: int | None = None
        long_idx: int | None = None
        for i, h in enumerate(headers_text):
            if "pair" in h or "symbol" in h or "instrument" in h:
                pair_idx = i
            if "long" in h and "%" in h:
                long_idx = i
            elif h == "long":
                long_idx = i
        if pair_idx is None or long_idx is None:
            continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if max(pair_idx, long_idx) >= len(cells):
                continue
            raw_name = cells[pair_idx].get_text(strip=True)
            pair = _normalize_pair(raw_name)
            if pair is None:
                continue
            long_pct_str = cells[long_idx].get_text(strip=True).replace("%", "")
            try:
                result[pair] = float(long_pct_str)
            except (TypeError, ValueError):
                pass

    if not result:
        raise RuntimeError("FXSSI HTML: semua strategi scraping gagal mengekstrak data")
    return result


def _fetch_fxssi() -> dict[str, float]:
    """Fetch FXSSI data, coba JSON endpoint dulu, fallback ke HTML scrape."""
    try:
        return _fetch_fxssi_json()
    except Exception as exc_json:
        logger.debug("FXSSI JSON endpoint gagal (%s), fallback ke HTML scrape.", exc_json)
        return _fetch_fxssi_html()


# ---------------------------------------------------------------------------
# SOURCE C — Dukascopy SWFX
# ---------------------------------------------------------------------------

# Endpoint JSON widget publik Dukascopy SWFX Trader Sentiment
# Ref: https://www.dukascopy.com/trading-tools/widgets/sentiment/
# Query param: ?a=json → return JSON array
# Alternatif: iframe widget + scrape HTML
_DUKASCOPY_JSON_URL: str = (
    "https://www.dukascopy.com/trading-tools/widgets/sentiment/sentiment.php?a=json"
)
_DUKASCOPY_WIDGET_URL: str = (
    "https://www.dukascopy.com/trading-tools/widgets/quotes/sentiment.php"
)


def _fetch_dukascopy_json() -> dict[str, float]:
    """Fetch Dukascopy SWFX dari JSON endpoint.

    Response shape (berdasarkan dokumentasi widget):
        [
          {"s": "EUR/USD", "long": 62, "short": 38},
          {"s": "GBP/USD", "long": 45, "short": 55},
          ...
        ]
    Field 's' = pair name (dengan separator '/').
    Field 'long'/'short' = persentase (integer atau float, 0–100).

    Raises RuntimeError bila gagal parse atau response kosong.
    """
    resp = _get_with_retry(_DUKASCOPY_JSON_URL)

    # Response bisa berupa JSON langsung atau JSON di dalam HTML
    content_type = resp.headers.get("Content-Type", "")
    if "html" in content_type.lower():
        # Fallback: ekstrak JSON dari HTML
        raise RuntimeError("Dukascopy: response adalah HTML, bukan JSON")

    raw = resp.json()
    if not isinstance(raw, list):
        raise RuntimeError(f"Dukascopy JSON: format tidak dikenali (type={type(raw).__name__})")

    result: dict[str, float] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        # Field nama pair: 's', 'symbol', 'pair', 'name'
        raw_name = str(
            item.get("s") or item.get("symbol") or item.get("pair") or item.get("name") or ""
        )
        pair = _normalize_pair(raw_name)
        if pair is None:
            continue
        long_val = item.get("long") or item.get("longPercent") or item.get("long_percent")
        if long_val is None:
            continue
        try:
            result[pair] = float(long_val)
        except (TypeError, ValueError):
            pass

    if not result:
        raise RuntimeError("Dukascopy JSON: tidak ada data pair yang dikenali")
    return result


def _fetch_dukascopy_html() -> dict[str, float]:
    """Fallback: scrape HTML dari widget Dukascopy.

    Strategi:
    1. Cari <script> inline dengan JSON data.
    2. Cari elemen HTML dengan data-* atau class yang berisi long%.

    HTML structure widget (diperkirakan):
        <li data-pair="EUR/USD" data-long="62" data-short="38">
        ATAU
        <script>DUKAS_SENTIMENT_DATA = [{"s":"EUR/USD","long":62}];</script>
    """
    resp = _get_with_retry(
        _DUKASCOPY_WIDGET_URL,
        params={"pairs": "EUR/USD,GBP/USD,USD/JPY,AUD/USD,NZD/USD,USD/CAD,USD/CHF,XAU/USD"},
        headers={"Accept": "text/html"},
    )
    soup = BeautifulSoup(resp.text, "lxml")
    result: dict[str, float] = {}

    # Strategi 1: inline script JSON
    for script_tag in soup.find_all("script"):
        text = script_tag.get_text()
        candidates = re.findall(r'(?:\w+)\s*=\s*(\[.+?\])\s*[;,]', text, re.DOTALL)
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, list):
                continue
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                raw_name = str(
                    item.get("s") or item.get("symbol") or item.get("pair") or ""
                )
                pair = _normalize_pair(raw_name)
                if pair is None:
                    continue
                long_val = item.get("long") or item.get("longPercent")
                if long_val is not None:
                    try:
                        result[pair] = float(long_val)
                    except (TypeError, ValueError):
                        pass

    if result:
        return result

    # Strategi 2: data-* attribute
    for el in soup.find_all(attrs={"data-long": True}):
        # Cari pair dari sibling atau parent attribute
        raw_name = (
            el.get("data-pair") or el.get("data-symbol")
            or el.get("data-s") or ""
        )
        pair = _normalize_pair(raw_name)
        if pair is None:
            continue
        try:
            result[pair] = float(el["data-long"])
        except (TypeError, ValueError, KeyError):
            pass

    if not result:
        raise RuntimeError("Dukascopy HTML: semua strategi scraping gagal")
    return result


def _fetch_dukascopy() -> dict[str, float]:
    """Fetch Dukascopy SWFX, JSON endpoint dulu, fallback ke HTML."""
    try:
        return _fetch_dukascopy_json()
    except Exception as exc_json:
        logger.debug("Dukascopy JSON gagal (%s), fallback ke HTML.", exc_json)
        return _fetch_dukascopy_html()


# ---------------------------------------------------------------------------
# Aggregation logic
# ---------------------------------------------------------------------------

def _compute_agreement(values: list[float]) -> float:
    """Hitung agreement score dari list long_pct antar sumber.

    agreement = 1 − (spread / 100), di mana spread = max − min.
    - 1 sumber → agreement = 1.0 (tidak ada konflik, tapi sources_failed mungkin ada).
    - Semua sumber sama → 1.0.
    - Sumber berselisih 40pp → 0.60.

    Clamp ke [0.0, 1.0].
    """
    if len(values) <= 1:
        return 1.0
    spread = max(values) - min(values)
    return max(0.0, min(1.0, 1.0 - spread / 100.0))


def _aggregate_retail(
    myfxbook_data: dict[str, float] | None,
    fxssi_data: dict[str, float] | None,
    dukascopy_data: dict[str, float] | None,
) -> dict[str, dict]:
    """Gabungkan data dari sumber yang berhasil per pair.

    Untuk setiap pair yang ada di minimal 1 sumber:
      - by_source: dict nama_sumber → long_pct (hanya sumber yang punya data pair ini)
      - long_pct_agg: rata-rata dari sumber yang ada
      - agreement: lihat _compute_agreement()
    """
    sources_map: dict[str, dict[str, float] | None] = {
        "myfxbook": myfxbook_data,
        "fxssi": fxssi_data,
        "dukascopy": dukascopy_data,
    }

    # Kumpulkan semua pair yang muncul di minimal 1 sumber
    all_pairs: set[str] = set()
    for data in sources_map.values():
        if data:
            all_pairs.update(data.keys())

    retail: dict[str, dict] = {}
    for pair in sorted(all_pairs):
        by_source: dict[str, float] = {}
        for src_name, src_data in sources_map.items():
            if src_data and pair in src_data:
                by_source[src_name] = round(src_data[pair], 2)

        if not by_source:
            continue  # seharusnya tidak terjadi

        values = list(by_source.values())
        long_pct_agg = round(sum(values) / len(values), 2)
        agreement = round(_compute_agreement(values), 4)

        retail[pair] = {
            "by_source": by_source,
            "long_pct_agg": long_pct_agg,
            "agreement": agreement,
        }

    return retail


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_retail(
    *,
    myfxbook_session: str | None = None,
    myfxbook_email: str | None = None,
    myfxbook_password: str | None = None,
) -> dict:
    """Fetch dan aggregasi retail sentiment dari Myfxbook, FXSSI, dan Dukascopy.

    Kredensial Myfxbook diambil dari parameter atau st.secrets (urutan prioritas:
    1. myfxbook_session langsung (kalau sudah punya token),
    2. myfxbook_email + myfxbook_password untuk login,
    3. Coba import st.secrets kalau tidak ada parameter.

    Returns dict sesuai schema §4 retail:
        {
          "as_of_utc": str,
          "sources_ok": list[str],
          "sources_failed": list[str],
          "retail": {
            "EURUSD": {
              "by_source": {"myfxbook": 62.0, "fxssi": 58.0},
              "long_pct_agg": 60.0,
              "agreement": 0.96
            }, ...
          }
        }

    Graceful degradation:
    - Sumber gagal → masuk sources_failed, tidak crash.
    - Kalau SEMUA sumber gagal → retail={} + semua masuk sources_failed.
    - Angka tidak pernah dikarang: kalau data tidak ada, pair tidak masuk dict.
    """
    ts = fmt_iso_utc(now_utc())
    sources_ok: list[str] = []
    sources_failed: list[str] = []

    # ---- Myfxbook ----
    myfxbook_data: dict[str, float] | None = None
    myfxbook_status = "not_attempted"
    _stage = "init"
    try:
        session = myfxbook_session
        email = myfxbook_email
        password = myfxbook_password
        if not session and not (email and password):
            try:
                import streamlit as st  # type: ignore
                email = email or st.secrets.get("MYFXBOOK_EMAIL") or st.secrets.get("myfxbook_email")
                password = password or st.secrets.get("MYFXBOOK_PASSWORD") or st.secrets.get("myfxbook_password")
                session = session or st.secrets.get("MYFXBOOK_SESSION") or st.secrets.get("myfxbook_session")
            except Exception:
                pass
        if not session and not (email and password):
            myfxbook_status = ("no_credentials: MYFXBOOK_EMAIL/PASSWORD tak terbaca di Secrets "
                               "(cek nama persis & tanpa [section])")
            raise RuntimeError(myfxbook_status)
        # Satu Session keep-alive utk login + outlook → coba paksa egress IP konsisten
        # (myfxbook session IP-bound; Streamlit bisa rotasi IP antar-request).
        http = requests.Session()
        # Opsional: proxy IP-tetap (solusi "VPN" sisi-server). Kalau keep-alive tak cukup,
        # set MYFXBOOK_PROXY di Secrets (mis. "http://user:pass@host:port") → login+outlook
        # keluar via 1 IP stabil → session valid. Tanpa secret ini = tak ada efek.
        try:
            import streamlit as _st  # type: ignore
            _proxy = _st.secrets.get("MYFXBOOK_PROXY") or _st.secrets.get("myfxbook_proxy")
            if _proxy:
                http.proxies = {"http": _proxy, "https": _proxy}
                logger.info("Myfxbook: pakai proxy IP-tetap")
        except Exception:
            pass
        if not session:
            _stage = "login"
            session = _myfxbook_login(email, password, http=http)
        _stage = "outlook"
        myfxbook_data = _fetch_myfxbook(session, http=http)
        sources_ok.append("myfxbook")
        myfxbook_status = f"ok: {len(myfxbook_data)} pairs"
        logger.info("Myfxbook: %d pairs fetched", len(myfxbook_data))
    except Exception as exc:
        if "myfxbook" not in sources_failed:
            sources_failed.append("myfxbook")
        if not myfxbook_status.startswith("no_credentials"):
            myfxbook_status = f"{_stage}_failed: {type(exc).__name__}: {str(exc)[:160]}"
        logger.warning("Myfxbook gagal [%s]: %s", _stage, exc)

    # ---- FXSSI ----
    # DINONAKTIFKAN: scrape FXSSI/Dukascopy gagal konsisten dari IP datacenter
    # (handover). Myfxbook API = sumber retail tunggal yang andal. Set True untuk re-enable.
    _USE_DEAD_SCRAPES = False
    fxssi_data: dict[str, float] | None = None
    if _USE_DEAD_SCRAPES:
        try:
            fxssi_data = _fetch_fxssi()
            sources_ok.append("fxssi")
            logger.info("FXSSI: %d pairs fetched", len(fxssi_data))
        except Exception as exc:
            sources_failed.append("fxssi")
            logger.warning("FXSSI gagal: %s", exc)

    # ---- Dukascopy ----
    dukascopy_data: dict[str, float] | None = None
    if _USE_DEAD_SCRAPES:
        try:
            dukascopy_data = _fetch_dukascopy()
            sources_ok.append("dukascopy")
            logger.info("Dukascopy: %d pairs fetched", len(dukascopy_data))
        except Exception as exc:
            sources_failed.append("dukascopy")
            logger.warning("Dukascopy gagal: %s", exc)

    retail = _aggregate_retail(myfxbook_data, fxssi_data, dukascopy_data)

    return {
        "as_of_utc": ts,
        "sources_ok": sources_ok,
        "sources_failed": sources_failed,
        "retail": retail,
        "_meta": {"myfxbook_status": myfxbook_status},
    }
