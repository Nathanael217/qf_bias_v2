# QF_BIAS_BUILD: news-multisource (FinancialJuice+Fed+ForexLive) (2026-06-03)
"""
collectors/news.py — FinancialJuice RSS headline fetcher.

Fetch-only collector: ambil headline terbaru, normalisasi timestamp, simpan raw_category.
TIDAK melakukan dedup, klasifikasi arah, atau scoring — itu tugas engine/news_overlay.py.

Output schema (arsitektur §4):
{
  "as_of_utc": "2026-06-01T07:00:00Z",
  "headlines": [
    {
      "ts_utc": "2026-06-01T06:45:00Z",
      "ts_wib": "2026-06-01 13:45",
      "title": "Fed Chair Powell: 'No rush to cut rates'",
      "source": "FinancialJuice",
      "raw_category": "CENTRAL BANKS"
    },
    ...
  ]
}

SUMBER:
  FinancialJuice RSS — https://www.financialjuice.com/feed.ashx?format=rss
  Feed publik, tidak perlu auth. Update frekuensi tinggi (beberapa kali per menit saat pasar aktif).

  RSS item structure (feedparser fields):
    entry.title        → judul headline
    entry.published    → string tanggal (RFC 2822 atau ISO)
    entry.published_parsed → struct_time UTC (dari feedparser)
    entry.tags[0].term → kategori (CENTRAL BANKS, GEOPOLITICAL, ECONOMIC DATA, dll.)
    entry.link         → URL artikel (tidak dipakai tapi ada)

  Contoh raw_category yang umum muncul:
    "CENTRAL BANKS", "GEOPOLITICAL", "ECONOMIC DATA", "EQUITY MARKETS",
    "FOREX", "COMMODITIES", "CRYPTO", "BONDS", "ENERGY"

  TTL cache: 300 detik (5 menit) — sesuai config.TTL["news"].
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import feedparser
import requests

from utils.timeutils import fmt_iso_utc, fmt_wib_display, now_utc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanta
# ---------------------------------------------------------------------------

_FINANCIALJUICE_RSS_URL: str = "https://www.financialjuice.com/feed.ashx?format=rss"
"""
URL feed RSS publik FinancialJuice.
Alternatif bila format=rss tidak bekerja: https://www.financialjuice.com/feed.ashx?format=atom
"""

# ---------------------------------------------------------------------------
# FEED REGISTRY — multi-source (semua RSS, di-merge + di-dedup oleh clustering)
# ---------------------------------------------------------------------------
# (nama_sumber, url). Tiap feed di-fetch terpisah dengan try/except sendiri:
# satu feed mati TIDAK mematikan yang lain. raw_category dari tag feed (kalau ada);
# kalau feed tak punya tag (mis. Fed), raw_category jatuh ke fallback default per feed.
#
# Risiko blokir IP datacenter (pelajaran retail layer):
#   - federalreserve.gov  : situs .gov, praktis tidak diblok. Sinyal primer hawkish/dovish.
#   - financialjuice      : sudah terbukti jalan di deploy.
#   - forexlive           : rebrand → InvestingLive; requests ikut redirect otomatis.
# Untuk menambah/menghapus sumber: edit list ini saja. Tidak ada perubahan lain.
_FEEDS: list[tuple[str, str]] = [
    ("FinancialJuice", _FINANCIALJUICE_RSS_URL),
    ("Fed",            "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("ForexLive",      "https://www.forexlive.com/feed"),
]
# Fallback raw_category bila feed tidak menyertakan tag kategori (mis. Fed press release).
_FEED_DEFAULT_CATEGORY: dict[str, str] = {
    "Fed": "CENTRAL_BANK",
}

_MAX_HEADLINES: int = 80
"""Jumlah headline maksimal TOTAL (gabungan semua feed) yang dikembalikan."""

_MAX_PER_FEED: int = 40
"""Batas headline per feed sebelum merge (cegah satu feed mendominasi)."""

_TIMEOUT: int = 15  # detik
_MAX_RETRY: int = 2
_RETRY_BACKOFF: float = 2.0

_SOURCE_NAME: str = "FinancialJuice"


# ---------------------------------------------------------------------------
# HTTP helper khusus feedparser
# ---------------------------------------------------------------------------

def _fetch_feed_with_retry(url: str) -> feedparser.FeedParserDict:
    """Fetch RSS feed dengan retry sederhana.

    feedparser.parse() bisa gagal dengan connection error — tangani via requests
    dulu, lalu parse dari string (menghindari feedparser internal network issues).

    Raises RuntimeError setelah semua retry habis.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; qf_bias/1.0; "
            "+https://github.com/your-org/qf_bias)"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(_MAX_RETRY):
        try:
            resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
            resp.raise_for_status()
            # Parse dari content bytes (lebih reliable daripada URL langsung)
            feed = feedparser.parse(resp.content)
            if feed.get("bozo") and not feed.get("entries"):
                # bozo = True berarti ada parse error; tapi entries mungkin tetap ada
                exc = feed.get("bozo_exception")
                raise RuntimeError(f"feedparser bozo error: {exc}")
            return feed
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRY - 1:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
            logger.debug("RSS fetch retry %d/%d: %s", attempt + 1, _MAX_RETRY, exc)
    raise RuntimeError(f"RSS fetch gagal setelah {_MAX_RETRY} retry: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_entry_timestamp(entry: Any) -> datetime | None:
    """Ekstrak timestamp UTC dari entry feedparser.

    Urutan prioritas:
    1. entry.published_parsed (struct_time UTC, paling reliable)
    2. entry.updated_parsed
    3. entry.created_parsed
    4. Fallback: now_utc() dengan warning

    Returns datetime aware UTC, atau None kalau semua gagal.
    """
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        struct_t = getattr(entry, attr, None)
        if struct_t is not None:
            try:
                # struct_time dari feedparser selalu UTC
                return datetime(*struct_t[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError, OverflowError) as exc:
                logger.debug("Gagal konversi %s: %s", attr, exc)

    # Coba parse string langsung kalau ada
    for attr in ("published", "updated"):
        raw_str: str | None = getattr(entry, attr, None)
        if raw_str:
            try:
                # feedparser.parse biasanya sudah normalize, tapi coba fallback
                import email.utils
                parsed = email.utils.parsedate_to_datetime(raw_str)
                return parsed.astimezone(timezone.utc)
            except Exception:
                pass

    logger.debug("Tidak bisa parse timestamp entry: %s", getattr(entry, "title", "?"))
    return None


def _extract_raw_category(entry: Any) -> str | None:
    """Ekstrak kategori dari entry feedparser.

    FinancialJuice biasanya menyimpan kategori di:
    - entry.tags[0].term  → nilai seperti "CENTRAL BANKS"
    - entry.category      → string langsung

    Return None kalau tidak ada.
    """
    # Coba tags dulu
    tags = getattr(entry, "tags", None)
    if tags and isinstance(tags, list) and len(tags) > 0:
        term = getattr(tags[0], "term", None)
        if term:
            return str(term).strip().upper()

    # Fallback ke attribute category
    category = getattr(entry, "category", None)
    if category:
        return str(category).strip().upper()

    return None


def _clean_title(raw_title: str) -> str:
    """Bersihkan judul headline dari HTML entities dan whitespace berlebih.

    feedparser biasanya sudah decode HTML entities, tapi ada edge cases.
    """
    import html
    title = html.unescape(raw_title)
    # Hapus tag HTML bila ada (defensive)
    title = re.sub(r"<[^>]+>", "", title)
    # Normalize whitespace
    title = " ".join(title.split())
    return title.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _parse_feed_entries(
    feed: feedparser.FeedParserDict,
    source_name: str,
    ts_now: datetime,
    max_n: int,
) -> list[dict]:
    """Ubah entries feedparser → list headline dict (schema §4). Tag source."""
    entries = feed.get("entries", [])
    default_cat = _FEED_DEFAULT_CATEGORY.get(source_name)
    out: list[dict] = []
    for entry in entries[:max_n]:
        raw_title = getattr(entry, "title", "")
        if not raw_title:
            continue  # skip entry tanpa judul

        ts_utc = _parse_entry_timestamp(entry)
        if ts_utc is None:
            # Lebih baik ada entry dengan timestamp kurang akurat daripada hilang.
            ts_utc = ts_now
            logger.debug("Timestamp fallback (%s): %s", source_name, raw_title)

        title = _clean_title(raw_title)
        raw_category = _extract_raw_category(entry) or default_cat
        link = getattr(entry, "link", "") or ""

        out.append({
            "ts_utc": fmt_iso_utc(ts_utc),
            "ts_wib": fmt_wib_display(ts_utc),
            "title": title,
            "source": source_name,
            "raw_category": raw_category,  # None kalau tidak ada
            "link": link,
        })
    return out


def get_news(max_headlines: int = _MAX_HEADLINES) -> dict:
    """Fetch headline dari SEMUA feed di _FEEDS, merge + tag source.

    Tiap feed di-fetch terpisah dengan try/except sendiri: satu feed mati
    TIDAK mematikan yang lain (defensif terhadap blokir IP datacenter).

    Returns dict sesuai schema §4 news + meta sumber:
        {
          "as_of_utc": "...",
          "headlines": [ {ts_utc, ts_wib, title, source, raw_category, link}, ... ],
          "sources_ok":     ["FinancialJuice", "Fed"],   # feed yang hidup
          "sources_failed": ["ForexLive"],               # feed yang gagal
          "error": "..."   # HANYA kalau SEMUA feed gagal
        }

    Headlines diurutkan descending by timestamp (terbaru duluan).
    Dedup lintas-sumber TIDAK dilakukan di sini (itu tugas clustering di engine).
    """
    ts_now = now_utc()

    headlines: list[dict] = []
    sources_ok: list[str] = []
    sources_failed: list[str] = []
    errors: list[str] = []

    for source_name, url in _FEEDS:
        try:
            feed = _fetch_feed_with_retry(url)
            parsed = _parse_feed_entries(feed, source_name, ts_now, _MAX_PER_FEED)
            if parsed:
                headlines.extend(parsed)
                sources_ok.append(source_name)
                logger.info("get_news: %s → %d headline", source_name, len(parsed))
            else:
                # Fetch sukses tapi 0 entry → anggap gagal (sumber tidak berguna kali ini)
                sources_failed.append(source_name)
                errors.append(f"{source_name}: 0 entry")
                logger.warning("get_news: %s fetch OK tapi 0 entry", source_name)
        except Exception as exc:
            sources_failed.append(source_name)
            errors.append(f"{source_name}: {exc}")
            logger.warning("get_news: %s gagal: %s (lanjut feed lain)", source_name, exc)

    # Urutkan terbaru dulu, lalu cap total
    headlines.sort(key=lambda h: h["ts_utc"], reverse=True)
    headlines = headlines[:max_headlines]

    result = {
        "as_of_utc": fmt_iso_utc(ts_now),
        "headlines": headlines,
        "sources_ok": sources_ok,
        "sources_failed": sources_failed,
    }
    # error HANYA kalau semua feed gagal (tidak ada headline sama sekali)
    if not headlines:
        result["error"] = "; ".join(errors) or "semua feed kosong"
    return result


# ---------------------------------------------------------------------------
# Untuk menghindari error pada import re di _clean_title
# ---------------------------------------------------------------------------
import re  # noqa: E402  (dipakai oleh _clean_title di atas)
