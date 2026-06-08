"""
collectors/telegram_news.py — Sumber news TAMBAHAN dari channel Telegram publik.

Mekanisme: scrape halaman preview publik `https://t.me/s/<channel>` (HTML, TANPA
auth/bot/MTProto). Hanya channel PUBLIK yang preview-nya aktif. Output schema
sama dengan collectors/news.py → ikut di-filter ketat oleh engine/news_filter.

PENTING (kejujuran teknis):
  - Default OFF: TELEGRAM_CHANNELS kosong → tidak ada perubahan perilaku.
    Aktifkan dengan menambah handle ke TELEGRAM_CHANNELS.
  - t.me/s scraping bisa diblok/di-rate-limit Telegram & struktur HTML bisa
    berubah → parser ini WAJIB diverifikasi live setelah deploy.
  - Sumber cepat (wire mirror) BISA menyiarkan headline yang kemudian terbukti
    PALSU (mis. fake "Trump 90-day tariff pause", Apr-2025) — sesuai keputusan,
    itu tetap relevan kalau menggoyang market; filter menilai DAMPAK, bukan
    kebenaran.
  - VERIFIKASI HANDLE PERSIS. Banyak copycat (mis. "firstsquaw" tanpa 'k').

Kandidat terverifikasi dari riset (handle resmi; tetap cek ulang sebelum aktifkan):
  - WalterBloomberg : mirror headline Bloomberg Terminal (cepat). Pernah sebar
                      headline palsu → jangan percaya 1 headline mentah.
  - FirstSquawk     : squawk makro/geopolitik (resmi). AWAS copycat "firstsquaw".
  - bloomberg       : channel resmi Bloomberg (lebih lambat, lebih terkurasi).
"""
from __future__ import annotations

import html as _html
import logging
import re
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# Aktifkan dengan menambah handle (tanpa @). KOSONG = fitur OFF.
# Default: 2 channel terverifikasi. Kalau app terasa lambat / TG kosong di
# deploy-mu (t.me/s bisa diblok/lambat dari Streamlit Cloud), kosongkan list ini.
TELEGRAM_CHANNELS: list[str] = ["WalterBloomberg", "FirstSquawk"]

# Kandidat (referensi; pindahkan ke TELEGRAM_CHANNELS untuk mengaktifkan).
CANDIDATE_CHANNELS: dict[str, str] = {
    "WalterBloomberg": "mirror Bloomberg Terminal (cepat; pernah sebar headline palsu)",
    "FirstSquawk":     "squawk makro/geopolitik resmi (AWAS copycat 'firstsquaw')",
    "bloomberg":       "Bloomberg resmi (lebih lambat, terkurasi)",
}

_BASE = "https://t.me/s/"
_TIMEOUT = 6
_MAX_PER_CHANNEL = 20

_MSG_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(raw: str) -> str:
    """HTML message → teks bersih (buang tag, unescape, <br> → spasi)."""
    txt = raw.replace("<br/>", " ").replace("<br>", " ").replace("</p>", " ")
    txt = _TAG_RE.sub("", txt)
    txt = _html.unescape(txt)
    return " ".join(txt.split()).strip()


def _norm_ts(raw_dt: str) -> str | None:
    """'2026-06-08T12:30:00+00:00' → '2026-06-08T12:30:00Z' (UTC ISO)."""
    try:
        dt = datetime.fromisoformat(raw_dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def parse_tme_html(html_str: str, channel: str, limit: int = _MAX_PER_CHANNEL) -> list[dict]:
    """Parse HTML preview t.me/s → list headline dict (schema collectors/news).

    Pairing text↔time per blok pesan (split di 'tgme_widget_message_wrap').
    Pure function → bisa di-test tanpa jaringan.
    """
    out: list[dict] = []
    if not html_str:
        return out
    chunks = re.split(r"tgme_widget_message_wrap", html_str)
    for ch in chunks[1:]:
        tmatch = _MSG_TEXT_RE.search(ch)
        dmatch = _TIME_RE.search(ch)
        if not tmatch or not dmatch:
            continue
        title = _strip_html(tmatch.group(1))
        ts = _norm_ts(dmatch.group(1))
        if not title or not ts:
            continue
        out.append({
            "ts_utc": ts,
            "title": title[:300],
            "source": f"TG:{channel}",
            "raw_category": "TELEGRAM",
            "link": f"https://t.me/{channel}",
        })
    # Terbaru dulu, batasi.
    out.sort(key=lambda h: h["ts_utc"], reverse=True)
    return out[:limit]


def fetch_telegram_channel(channel: str, limit: int = _MAX_PER_CHANNEL) -> list[dict]:
    """Fetch + parse satu channel publik. Tahan-banting → [] kalau gagal."""
    url = f"{_BASE}{channel}"
    try:
        resp = requests.get(url, timeout=_TIMEOUT,
                            headers={"User-Agent": "Mozilla/5.0 (qf_bias news)"})
        resp.raise_for_status()
        return parse_tme_html(resp.text, channel, limit)
    except Exception as exc:
        logger.warning("Telegram %s gagal: %s", channel, exc)
        return []


def get_telegram_news(channels: list[str] | None = None) -> list[dict]:
    """Gabung headline dari beberapa channel publik. Per-channel guarded."""
    chans = channels if channels is not None else TELEGRAM_CHANNELS
    out: list[dict] = []
    for c in chans or []:
        out.extend(fetch_telegram_channel(c))
    return out
