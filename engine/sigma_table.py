# QF_BIAS_BUILD: sigma_table — lean surprise enrichment via faireconomy actual (2026-06-04)
"""
engine/sigma_table.py — σ (historical_std) seed table + surprise enrichment.
=============================================================================

KONTEKS (lihat qf_bias_HANDOVER_PLAN.md, dikoreksi):
  Premis lama handover keliru. `actual` BUKAN yang hilang — calendar_evt.py
  sudah mem-parse `actual` dari feed faireconomy untuk event yang sudah rilis.
  Yang benar-benar mengunci pipeline surprise adalah `historical_std` (σ):
  gate di app.py butuh actual DAN historical_std, sementara σ tidak pernah
  di-set di mana pun → released_events selalu kosong → surprise tak pernah
  masuk R_hard.

  Modul ini mengisi σ. Karena z = (actual − forecast) / σ, dan actual+forecast
  dua-duanya datang dari feed yang SAMA (faireconomy), unit otomatis konsisten
  → "unit-matching landmine" yang dikhawatirkan handover HILANG: kita tidak lagi
  mencampur forecast-faireconomy dengan actual-FRED.

  σ adalah besaran HISTORIS (dispersi surprise historis per indikator). Telat
  sebulan tidak relevan untuk menghitungnya — jadi keberatan "agregator basi"
  yang membunuh DBnomics tidak berlaku di sini. v1 ini PAKAI SEED hardcoded;
  ganti dengan σ terukur dari histori seri saat backtest.

DISIPLIN PROYEK (DIKUNCI):
  - Tabel ini hanya MENGUKUR dispersi. Engine (scoring.py) yang MENGHITUNG poin.
  - SEMUA nilai σ = PLACEHOLDER sampai dihitung dari histori nyata + backtest.
  - Keanggotaan tabel = GATE scoring: indikator yang TIDAK ada di sini tidak
    pernah men-drive skor (tetap display-only). Ini disengaja & graceful.

UNIT (WAJIB cocok dengan output collectors/calendar_evt.py:_parse_number):
  - Indikator "%"  (CPI m/m, Unemployment Rate, dst) → σ dalam POIN PERSEN.
      "0.3%" → 0.3 ; "4.0%" → 4.0
  - Indikator "K"  (NFP, Claims, Employment Change)  → σ dalam ORANG (raw).
      "175K" → 175000 ; "32.2K" → 32200
  - Indikator indeks (ISM/PMI) → σ dalam POIN INDEKS.  "48.5" → 48.5

POLARITY (asumsi rezim — PLACEHOLDER):
  +1 = beat ⇒ currency bullish (inflasi/growth/employment naik ⇒ CB hawkish).
  −1 = beat ⇒ currency bearish (Unemployment Rate / Jobless Claims: angka naik = buruk).
  Konvensi ini sah di rezim respons-hawkish; tinjau ulang saat backtest.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Negative-match: event yang TIDAK boleh masuk jalur surprise.
# Rate decision sudah ditangani terpisah lewat rate_diff di R_hard → jangan
# double-count. Speech/testimoni tidak punya actual/forecast numerik.
# ---------------------------------------------------------------------------

_SKIP_PATTERNS: tuple[str, ...] = (
    "rate decision", "rate statement", "bank rate", "cash rate",
    "interest rate", "federal funds", "refinancing rate", "deposit facility",
    "monetary policy", "press conference", "speaks", "speech", "testifies",
    "testimony", "member ", "governor ", "minutes",
)

# ---------------------------------------------------------------------------
# SIGMA RULES — urutan PENTING: spesifik-mata-uang dulu, lalu generik (None).
# Tiap rule: patterns (substring lower-case), currency (None=semua), sigma, polarity, note.
# ---------------------------------------------------------------------------

SIGMA_RULES: list[dict[str, Any]] = [
    # ===================== USD (high-impact, σ khas AS) =====================
    {"patterns": ["adp"], "currency": "USD", "sigma": 60000.0, "polarity": +1,
     "note": "ADP employment (orang) — seed"},
    {"patterns": ["non-farm employment change", "nonfarm", "non farm employment"],
     "currency": "USD", "sigma": 70000.0, "polarity": +1,
     "note": "NFP (orang) — seed; surprise std historis ~60–100k"},
    {"patterns": ["unemployment claims", "jobless claims", "initial claims"],
     "currency": "USD", "sigma": 20000.0, "polarity": -1,
     "note": "Weekly jobless claims (orang) — seed; polarity −1"},
    {"patterns": ["core cpi m/m", "core cpi"], "currency": "USD", "sigma": 0.10, "polarity": +1,
     "note": "Core CPI m/m (pp) — seed"},
    {"patterns": ["cpi m/m"], "currency": "USD", "sigma": 0.10, "polarity": +1,
     "note": "Headline CPI m/m (pp) — seed"},
    {"patterns": ["cpi y/y"], "currency": "USD", "sigma": 0.13, "polarity": +1,
     "note": "CPI y/y (pp) — seed"},
    {"patterns": ["core pce", "pce price"], "currency": "USD", "sigma": 0.08, "polarity": +1,
     "note": "Core PCE m/m (pp) — seed; gauge inflasi favorit Fed"},
    {"patterns": ["average hourly earnings"], "currency": "USD", "sigma": 0.10, "polarity": +1,
     "note": "AHE m/m (pp) — seed"},
    {"patterns": ["core retail sales"], "currency": "USD", "sigma": 0.40, "polarity": +1,
     "note": "Core retail sales m/m (pp) — seed"},
    {"patterns": ["retail sales m/m", "retail sales"], "currency": "USD", "sigma": 0.45, "polarity": +1,
     "note": "Retail sales m/m (pp) — seed"},
    {"patterns": ["core ppi", "ppi m/m"], "currency": "USD", "sigma": 0.20, "polarity": +1,
     "note": "PPI m/m (pp) — seed"},
    {"patterns": ["ism manufacturing"], "currency": "USD", "sigma": 1.5, "polarity": +1,
     "note": "ISM Manufacturing PMI (indeks) — seed"},
    {"patterns": ["ism services", "ism non-manufacturing"], "currency": "USD", "sigma": 1.5, "polarity": +1,
     "note": "ISM Services PMI (indeks) — seed"},
    {"patterns": ["unemployment rate"], "currency": "USD", "sigma": 0.13, "polarity": -1,
     "note": "Unemployment rate (pp) — seed; polarity −1"},
    {"patterns": ["advance gdp", "gdp q/q", "prelim gdp", "gdp"], "currency": "USD", "sigma": 0.50, "polarity": +1,
     "note": "GDP q/q annualised (pp) — seed; AS pakai annualised, σ lebih besar"},

    # ===================== Employment change non-US (orang, σ lebih kecil) =====================
    {"patterns": ["employment change"], "currency": "AUD", "sigma": 22000.0, "polarity": +1,
     "note": "AU employment change (orang) — seed"},
    {"patterns": ["employment change"], "currency": "CAD", "sigma": 25000.0, "polarity": +1,
     "note": "CA employment change (orang) — seed"},
    {"patterns": ["employment change", "claimant count"], "currency": "GBP", "sigma": 30000.0, "polarity": +1,
     "note": "UK employment/claimant (orang) — seed"},
    {"patterns": ["employment change", "non-farm payrolls"], "currency": "NZD", "sigma": 8000.0, "polarity": +1,
     "note": "NZ employment (orang) — seed; ekonomi kecil"},

    # ===================== Generik (None = semua mata uang lain) =====================
    # CPI flash EUR (EA20), CPI y/y GBP/EUR, dll. σ generik untuk indikator umum.
    {"patterns": ["core cpi flash", "cpi flash", "core cpi y/y", "cpi y/y"], "currency": None,
     "sigma": 0.15, "polarity": +1, "note": "CPI y/y flash (pp) — seed generik"},
    {"patterns": ["core cpi m/m", "cpi m/m"], "currency": None, "sigma": 0.15, "polarity": +1,
     "note": "CPI m/m (pp) — seed generik"},
    {"patterns": ["cpi q/q", "trimmed mean cpi", "cpi"], "currency": None, "sigma": 0.20, "polarity": +1,
     "note": "CPI kuartalan (AUD/NZD) (pp) — seed generik"},
    {"patterns": ["unemployment rate"], "currency": None, "sigma": 0.15, "polarity": -1,
     "note": "Unemployment rate (pp) — seed generik; polarity −1"},
    {"patterns": ["retail sales"], "currency": None, "sigma": 0.55, "polarity": +1,
     "note": "Retail sales m/m (pp) — seed generik"},
    {"patterns": ["manufacturing pmi", "services pmi", "composite pmi", "flash manufacturing", "flash services"],
     "currency": None, "sigma": 1.2, "polarity": +1,
     "note": "S&P Global flash PMI (indeks) — seed generik"},
    {"patterns": ["ifo business climate", "zew economic sentiment", "gdp q/q", "gdp"],
     "currency": None, "sigma": 0.30, "polarity": +1,
     "note": "GDP q/q / sentimen (pp/indeks) — seed generik"},
]

# (actual_source di-set oleh provider actual, mis. Eurostat — bukan di sini)


def _norm(s: str | None) -> str:
    """Lower-case + rapikan whitespace untuk pencocokan nama event."""
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _match_rule(name_norm: str, currency: str | None) -> dict[str, Any] | None:
    """Cari rule σ pertama yang cocok. Spesifik-mata-uang menang (urutan list)."""
    for rule in SIGMA_RULES:
        rc = rule.get("currency")
        if rc is not None and rc != currency:
            continue
        if any(pat in name_norm for pat in rule["patterns"]):
            return rule
    return None


def sigma_polarity(name: str, currency: str | None = None) -> tuple[float | None, int | None]:
    """Lookup σ + polarity untuk nama event. (None, None) kalau tak ada rule."""
    rule = _match_rule(_norm(name), currency)
    if rule is None:
        return None, None
    return rule.get("sigma"), rule.get("polarity")


def enrich_surprise_fields(events: list[dict[str, Any]]) -> dict[str, int]:
    """
    Set historical_std + surprise_polarity (+ sigma_basis) pada event yang
    cocok dengan SIGMA_RULES. Idempotent (skip kalau historical_std sudah ada).
    MUTASI in-place. Hanya event ber-σ yang nantinya lolos gate & men-drive skor.

    Return diagnostik:
      {
        "released":        jumlah event status=released,
        "released_actual": released yang punya actual (dari faireconomy),
        "scored":          released_actual yang dapat σ (akan masuk R_hard),
        "no_sigma":        released_actual TANPA σ (display-only, tak dikenal tabel),
        "skipped":         event yang sengaja di-skip (rate decision/speech),
      }
    """
    diag = {"released": 0, "released_actual": 0, "scored": 0, "no_sigma": 0, "skipped": 0}

    for ev in events:
        is_released = ev.get("status") == "released"
        has_actual = ev.get("actual") is not None
        if is_released:
            diag["released"] += 1
            if has_actual:
                diag["released_actual"] += 1

        # Sudah ter-enrich sebelumnya (cache) → jangan ulang, tapi tetap hitung diag.
        if ev.get("historical_std") is not None:
            if is_released and has_actual:
                diag["scored"] += 1
            continue

        name_norm = _norm(ev.get("name"))

        # Negative-match: rate decision (sudah di rate_diff) / speech (tak ada angka).
        if any(skip in name_norm for skip in _SKIP_PATTERNS):
            diag["skipped"] += 1
            continue

        rule = _match_rule(name_norm, ev.get("currency"))
        if rule is None:
            if is_released and has_actual:
                diag["no_sigma"] += 1
            continue

        ev["historical_std"] = float(rule["sigma"])
        ev["surprise_polarity"] = float(rule["polarity"])
        ev["sigma_basis"] = rule.get("note", "seed placeholder")
        # CATATAN: actual_source TIDAK di-set di sini. faireconomy tak memberi actual
        # (terbukti di deploy). Provider actual (mis. Eurostat) yang men-set actual_source.

        if is_released and has_actual:
            diag["scored"] += 1

    return diag
