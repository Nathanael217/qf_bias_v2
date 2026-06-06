# QF_BIAS_BUILD: Modul B — Groq news direction classifier (measurement-only) (2026-06-04d)
"""
engine/groq_client.py — Groq sebagai PENGUKUR arah news (bukan penentu bias).
=============================================================================

GARIS TEGAS (handover §5 — TIDAK BOLEH DILANGGAR):
  - Groq MENGUKUR → arah (+1/0/−1) per aset + impact (low/med/high) + reasoning.
    Output JSON terstruktur, terukur, terverifikasi.
  - Engine MENGHITUNG → ambil skor arah Groq sebagai INPUT ke
    news_overlay.compute_news_delta (menggantikan classify_direction keyword),
    lalu engine yang kalikan magnitude×decay×SCALE_FACTOR dan cap ±30. TIDAK BERUBAH.
  - Groq TIDAK PERNAH: menentukan poin bias langsung, meramal harga, jadi hakim akhir.
    Melanggar = membangun ulang "metavulus" (R_narrative korelasi ~nol per audit v3).

Kenapa Groq di sini berguna: keyword gagal pada nuansa arah —
  "BoJ should slow bond buying" = hawkish JPY (keyword tak tangkap),
  judul bank sentral kering ("Monetary Policy Decision") → keyword nol.
Groq baca makna → ekstrak ARAH. Itu saja. Magnitude tetap dari struktur cluster.

MODUL INI PURE (tak impor streamlit). Caching + st.secrets + fallback ditangani app.py.
HTTP call ke api.groq.com tak bisa dites dari sandbox → parser dipisah agar
testable offline; semua kegagalan → return None (caller fallback ke keyword).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

logger = logging.getLogger("qf_bias.groq_client")

# Sinkron dengan engine/news_overlay._ALL_ASSETS
ASSETS: list[str] = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "XAU", "BTC", "ETH"]

_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
_MODEL = "llama-3.3-70b-versatile"
_TIMEOUT = 20
_RETRIES = 1

_SYSTEM_PROMPT = (
    "You are a financial-news DIRECTION CLASSIFIER for an FX/Gold/Crypto bias engine. "
    "You MEASURE direction; you do NOT forecast prices or give trading advice. "
    "Given ONE news headline, identify which assets are DIRECTLY affected and the immediate "
    "directional implication for each.\n"
    "Assets: USD EUR GBP JPY AUD NZD CAD CHF XAU(gold) BTC ETH.\n"
    "For each AFFECTED asset output +1 (bullish for that asset), -1 (bearish), or 0. "
    "Only include assets with a clear implication; omit the rest.\n"
    "Guidance: hawkish central-bank tone => that currency +1; dovish => -1. "
    "Risk-off => XAU,USD,JPY,CHF +1 and BTC,ETH,AUD,NZD,CAD -1; risk-on => inverse. "
    "A rate HIKE/strong-data surprise is bullish that currency; CUT/weak-data bearish.\n"
    "Rate impact: 'high' (rate decision, CPI, NFP, war/shock), 'med' (secondary data, official speech), "
    "'low' (minor/aggregate).\n"
    "Do NOT predict the future. Output ONLY a JSON object, no markdown, no prose:\n"
    '{"affected":{"JPY":1},"impact":"high","reasoning":"<=20 words"}'
)


def _coerce_score(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f > 0:
        return 1.0
    if f < 0:
        return -1.0
    return 0.0


def parse_groq_json(content: str) -> dict[str, Any] | None:
    """
    Parse konten balasan model → {scores:{asset:float}, impact:str, reasoning:str}.
    Defensif: strip fence ```json, validasi, coerce ke {-1,0,1}. None kalau gagal.
    DIPISAH dari HTTP supaya bisa diuji offline.
    """
    if not content or not isinstance(content, str):
        return None
    s = content.strip()
    # Buang fence markdown bila model bandel
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
        s = s.strip()
    # Ambil objek JSON pertama bila ada teks pengiring
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j == -1 or j < i:
            return None
        s = s[i:j + 1]
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    affected = obj.get("affected", {})
    if not isinstance(affected, dict):
        affected = {}
    scores: dict[str, float] = {a: 0.0 for a in ASSETS}
    for a, v in affected.items():
        au = str(a).strip().upper()
        if au in scores:
            scores[au] = _coerce_score(v)

    impact = str(obj.get("impact", "")).strip().lower()
    if impact not in ("low", "med", "high"):
        impact = "med"
    reasoning = str(obj.get("reasoning", "")).strip()[:200]

    return {"scores": scores, "impact": impact, "reasoning": reasoning}


def classify_headline(headline: str, api_key: str,
                      model: str = _MODEL, timeout: int = _TIMEOUT) -> dict[str, Any] | None:
    """
    Panggil Groq untuk SATU headline → {scores, impact, reasoning} atau None.
    None = caller WAJIB fallback ke keyword classify_direction. Tidak pernah raise.
    """
    if not headline or not api_key:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": headline.strip()[:500]},
        ],
        "temperature": 0,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_err: Exception | None = None
    for attempt in range(_RETRIES + 1):
        try:
            resp = requests.post(_ENDPOINT, json=payload, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                logger.warning("Groq rate-limited (429) — fallback keyword")
                return None
            resp.raise_for_status()
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            parsed = parse_groq_json(content)
            if parsed is None:
                logger.warning("Groq output tak terparse: %r", content[:120])
            return parsed
        except Exception as exc:  # noqa: BLE001 — graceful
            last_err = exc
            logger.debug("Groq attempt %d gagal: %s", attempt + 1, exc)
    logger.warning("Groq classify gagal final: %s", last_err)
    return None


_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
_VISION_SYSTEM = (
    "You read an economic-calendar screenshot (ForexFactory). Extract EVERY visible row "
    "as structured data. For each row output: currency (3-letter, e.g. USD/EUR/JPY), "
    "event (the event name text), actual, forecast, previous (copy the displayed strings "
    "verbatim, e.g. '122K','0.3%','-8.0M'; use empty string if a cell is blank/'–'). "
    "Do NOT interpret, invent, or compute — only transcribe what is shown. "
    'Output ONLY JSON: {"rows":[{"currency":"USD","event":"ISM Services PMI","actual":"54.5","forecast":"53.7","previous":"53.6"}]}'
)


def extract_calendar_image(image_b64: str, api_key: str,
                           media_type: str = "image/png", timeout: int = 40) -> list[dict] | None:
    """
    Groq Scout (vision) baca screenshot kalender → list baris mentah
    [{currency,event,actual,forecast,previous}] (string apa adanya) atau None.
    Hanya TRANSKRIPSI; pencocokan + cross-check dilakukan engine/manual_actuals.
    None = gagal → user input manual. Tidak pernah raise.
    """
    if not image_b64 or not api_key:
        return None
    payload = {
        "model": _VISION_MODEL,
        "messages": [
            {"role": "system", "content": _VISION_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": "Transcribe all calendar rows to JSON."},
                {"type": "image_url",
                 "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
            ]},
        ],
        "temperature": 0,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(_ENDPOINT, json=payload, headers=headers, timeout=timeout)
        if resp.status_code == 429:
            logger.warning("Groq vision rate-limited (429)")
            return None
        resp.raise_for_status()
        data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        obj = parse_groq_json(content) if False else _loads_rows(content)
        return obj
    except Exception as exc:  # noqa: BLE001
        logger.warning("Groq vision gagal: %s", exc)
        return None


def _loads_rows(content: str) -> list[dict] | None:
    """Parse {'rows':[...]} dari balasan vision (defensif)."""
    if not content:
        return None
    s = content.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
        s = s.strip()
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j == -1:
            return None
        s = s[i:j + 1]
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    rows = obj.get("rows") if isinstance(obj, dict) else None
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict)]
