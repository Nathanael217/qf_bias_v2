# QF_BIAS_BUILD: Modul A #2 — manual actual input + vision-row cross-check (2026-06-04e)
"""
engine/manual_actuals.py — Input ACTUAL manual (+ pencocokan hasil vision).
=============================================================================

KONTEKS (poin #1 user — beban terberat sistem bias):
  faireconomy tak kirim actual; API resmi per-negara tak bisa dites & sparse.
  Jalur paling andal = MANUSIA input actual sambil lihat screenshot ForexFactory.
  App sudah punya event (nama/currency/forecast/previous dari feed) — tinggal
  diisi `actual`-nya, lalu masuk pipeline σ→surprise→R_hard yang SUDAH ADA.

DUA LAPIS:
  1. Manual ketik (andal, sumber kebenaran). Manusia = verifikator → tak perlu guard.
  2. Vision pre-fill (Groq Scout baca screenshot) → DICOCOKKAN ke event engine dengan
     CROSS-CHECK forecast/previous (prinsip alignment guard) → isi sebagai SARAN ke
     form Lapis 1. Manusia tetap konfirmasi sebelum score. Vision TIDAK pernah
     langsung men-drive bias.

DISIPLIN: modul ini hanya MENGISI actual. σ + polarity tetap dari sigma_table;
engine tetap menghitung surprise→R_hard. Bobot tetap placeholder sampai backtest.

PURE: tak impor streamlit/groq. UI + panggilan vision ditangani app.py.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger("qf_bias.manual_actuals")

_MATCH_REL_TOL = 0.05
_MATCH_ABS_TOL = 0.10
_NAME_SIM_MIN = 0.62   # ambang kemiripan nama event (SequenceMatcher)


def make_event_id(ev: dict[str, Any]) -> str:
    """ID stabil per event utk key session_state. currency|name|ts_utc."""
    return f"{ev.get('currency','?')}|{(ev.get('name') or '').strip()}|{ev.get('ts_utc','')}"


def _parse_num(raw: Any) -> float | None:
    """Parse '122K'→122000, '0.3%'→0.3, '-8.0M'→-8000000. None kalau gagal.
    Disamakan dgn collectors/calendar_evt._parse_number agar unit konsisten."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s or s in ("-", "–", "—"):
        return None
    s_clean = re.sub(r"[%kKMBbps\s]+$", "", s).strip()
    mult = 1.0
    if s.upper().endswith("K"):
        mult = 1_000.0
    elif s.upper().endswith("M"):
        mult = 1_000_000.0
    try:
        return float(s_clean) * mult
    except ValueError:
        return None


def apply_manual_actuals(events: list[dict[str, Any]],
                         manual_map: dict[str, float]) -> dict[str, int]:
    """
    Isi ev['actual'] dari manual_map (key = make_event_id). MUTASI in-place.
    Manual = prioritas tertinggi → menimpa actual dari sumber lain (mis. Eurostat).
    Return diag {applied}.
    """
    diag = {"applied": 0}
    if not manual_map:
        return diag
    for ev in events:
        eid = make_event_id(ev)
        if eid in manual_map and manual_map[eid] is not None:
            try:
                ev["actual"] = float(manual_map[eid])
            except (TypeError, ValueError):
                continue
            ev["actual_source"] = "Manual"
            ev.pop("actual_misaligned", None)
            diag["applied"] += 1
    return diag


def _aligned(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    tol = max(_MATCH_ABS_TOL, abs(b) * _MATCH_REL_TOL)
    return abs(a - b) <= tol


def match_vision_rows(events: list[dict[str, Any]],
                      vision_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Cocokkan baris hasil vision (Groq Scout) ke event engine.
    vision_rows: [{currency, event, actual, forecast, previous}] (nilai bisa string).

    Untuk tiap baris: cari event currency-sama dgn nama paling mirip (≥ _NAME_SIM_MIN),
    lalu CROSS-CHECK forecast/previous vision ≈ feed. Hasil = saran pre-fill:
      [{event_id, name, currency, actual, confidence: high|low, reason}]
    confidence 'high' = nama mirip & (forecast/previous align) → aman dipercaya.
    'low' = nama mirip tapi forecast/previous tak align → tampilkan tapi minta cek.
    Baris tanpa actual / tanpa match → di-skip.
    """
    suggestions: list[dict[str, Any]] = []
    for row in vision_rows:
        v_actual = _parse_num(row.get("actual"))
        if v_actual is None:
            continue  # belum rilis / tak terbaca
        v_ccy = str(row.get("currency", "")).strip().upper()
        v_name = re.sub(r"\s+", " ", str(row.get("event", "")).lower()).strip()
        if not v_ccy or not v_name:
            continue

        best, best_sim = None, 0.0
        for ev in events:
            if (ev.get("currency") or "").upper() != v_ccy:
                continue
            ev_name = re.sub(r"\s+", " ", (ev.get("name") or "").lower()).strip()
            sim = SequenceMatcher(None, v_name, ev_name).ratio()
            if sim > best_sim:
                best, best_sim = ev, sim

        if best is None or best_sim < _NAME_SIM_MIN:
            continue

        v_fc = _parse_num(row.get("forecast"))
        v_pv = _parse_num(row.get("previous"))
        fc_ok = _aligned(v_fc, _parse_num(best.get("forecast")))
        pv_ok = _aligned(v_pv, _parse_num(best.get("previous")))
        # Cross-check: minimal salah satu (forecast/previous) align → confidence high.
        if fc_ok or pv_ok:
            conf, reason = "high", f"nama~{best_sim:.2f}, fc/prev cocok"
        else:
            conf, reason = "low", f"nama~{best_sim:.2f}, fc/prev TAK cocok — cek manual"

        suggestions.append({
            "event_id": make_event_id(best),
            "name": best.get("name"),
            "currency": best.get("currency"),
            "actual": v_actual,
            "confidence": conf,
            "reason": reason,
        })
    return suggestions
