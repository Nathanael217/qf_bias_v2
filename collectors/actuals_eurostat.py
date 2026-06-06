# QF_BIAS_BUILD: Modul A #1 — Eurostat direct-API actual (EUR/HICP) (2026-06-04c)
"""
collectors/actuals_eurostat.py — Actual real-time via API resmi Eurostat (EUR).
================================================================================

KONTEKS (handover §4 + verifikasi 2026-06-04):
  Feed faireconomy TERBUKTI tidak mengirim `actual` (forecast/previous saja —
  dicek di deploy: semua event released menampilkan actual "–"). Maka actual
  diambil dari penerbit resmi: Eurostat. Datacenter-IP aman (.europa.eu tak
  diblok), gratis, se-fresh penerbit (flash masuk cepat).

  Connector PERTAMA = EUR/HICP (impact tertinggi, punya nilai uji terverifikasi).
  Buktikan dulu, baru tambah negara/indikator lain (handover §4: "bangun SATU dulu").

PEMISAHAN TUGAS (penting):
  - Connector ini hanya men-set `actual` + `actual_source="Eurostat"`.
  - σ (historical_std) + polarity tetap dari engine/sigma_table.py.
  - Gate scoring di app.py butuh actual (dari sini) DAN σ (dari sigma_table).

ALIGNMENT GUARD (WAJIB — handover §3/§5, terbukti menyelamatkan dari DBnomics-basi):
  Sebelum menerima actual, observasi KEDUA-terbaru dari seri Eurostat (= bulan lalu)
  harus ≈ `previous` di kalender faireconomy (toleransi relatif ~5%). Kalau tidak
  align → TOLAK (jangan isi), tandai "misaligned". Guard ini juga melindungi dari
  timing-skew (mis. Eurostat belum muat flash bulan ini → series[0] masih bulan lalu
  → series[1] tak match previous → ditolak; lebih baik "–" jujur daripada actual basi).
  Karena itu, mapping coicop yang BELUM 100% pasti tetap AMAN dikirim: kalau kode salah,
  seri-nya beda → previous tak match → guard menolak. Tidak akan ada actual palsu.

UNIT:
  HICP unit=RCH_A ("annual rate of change") sudah dalam % YoY (mis. 3.2), cocok
  dengan forecast faireconomy "CPI Flash Estimate y/y" ("3.2%" → 3.2). transform=level.

NILAI UJI TERVERIFIKASI (rilis Eurostat, dari handover):
  HICP YoY EA → Des25 2.0 · Jan26 1.7 · Feb26 1.9 · Mar26 2.6 · Apr26 3.0 · Mei26 3.2 (flash)
  Parser HARUS mengembalikan 3.2 untuk observasi terbaru (Mei26), bukan 1.9.

CATATAN VERIFY-ON-DEPLOY:
  - prc_hicp_manr memuat flash bulan terkini? (handover bilang ya). Cek caption diagnostik.
  - geo=EA20 (komposisi tetap 20 negara) — BUKAN "EA" chain (kandidat sumber selisih lama).
  - coicop core = TOT_X_NRG_FOOD_TOB (excl. energy, food, alkohol, tembakau). Kalau guard
    menolak core terus padahal headline lolos → kemungkinan kode coicop core beda; cek &
    sesuaikan (guard mencegah data salah masuk selama itu).
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger("qf_bias.actuals_eurostat")

_BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
_ACTUAL_SOURCE = "Eurostat"
_ALIGN_REL_TOL = 0.05   # toleransi relatif previous-guard (5%)
_ALIGN_ABS_TOL = 0.10   # toleransi absolut (poin persen) — untuk nilai kecil
_TIMEOUT = 12
_RETRIES = 2

# ---------------------------------------------------------------------------
# INDIKATOR EUR — v1: HICP headline + core. Tambah une_rt_m/GDP belakangan.
# Tiap entry: dataset + dims (semua difilter single → response cuma vary di time).
#   match  = substring lower-case nama event faireconomy (currency=EUR diasumsikan).
#   transform = "level" (RCH_A sudah YoY %).
# ---------------------------------------------------------------------------

EU_INDICATORS: dict[str, dict[str, Any]] = {
    "hicp_core_yoy": {
        "match": ["core cpi flash", "core cpi estimate", "core cpi y/y", "core hicp"],
        "dataset": "prc_hicp_manr",
        "dims": {"freq": "M", "unit": "RCH_A", "coicop": "TOT_X_NRG_FOOD_TOB", "geo": "EA20"},
        "transform": "level",
        "label": "Core HICP YoY (EA20, excl energy/food/alc/tob)",
    },
    "hicp_headline_yoy": {
        "match": ["cpi flash", "cpi estimate", "cpi y/y", "hicp flash", "inflation rate"],
        "dataset": "prc_hicp_manr",
        "dims": {"freq": "M", "unit": "RCH_A", "coicop": "CP00", "geo": "EA20"},
        "transform": "level",
        "label": "HICP YoY all-items (EA20)",
    },
}
# Urutan: core SEBELUM headline (substring "core cpi flash" lebih spesifik).


def _build_url(dataset: str, dims: dict[str, str]) -> str:
    params = "&".join(f"{k}={v}" for k, v in dims.items())
    return f"{_BASE}/{dataset}?format=JSON&lang=EN&{params}"


def _get_json(url: str) -> dict[str, Any] | None:
    last_err: Exception | None = None
    for attempt in range(_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=_TIMEOUT,
                                headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — graceful
            last_err = exc
            logger.debug("Eurostat fetch attempt %d gagal (%s): %s", attempt + 1, url, exc)
    logger.warning("Eurostat fetch gagal final: %s (%s)", url, last_err)
    return None


def _parse_jsonstat_obs(js: dict[str, Any]) -> list[tuple[str, float]]:
    """
    Parse JSON-stat v2.0 → list (period, value) urut DESC (terbaru dulu).
    Mengasumsikan semua dimensi non-time difilter ke 1 kategori; hanya time vary.
    Robust: tetap pakai formula flat-index row-major umum.
    """
    try:
        ids: list[str] = js["id"]
        sizes: list[int] = js["size"]
        dims: dict[str, Any] = js["dimension"]
        values: dict[str, Any] = js["value"]
    except (KeyError, TypeError):
        logger.warning("JSON-stat: struktur tak terduga (id/size/dimension/value hilang)")
        return []

    if not ids or not sizes or not values:
        return []

    # Strides row-major: stride[i] = product(sizes[i+1:])
    n = len(sizes)
    strides = [1] * n
    for i in range(n - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]

    time_id = "time" if "time" in ids else ids[-1]
    tpos = ids.index(time_id)

    # Offset dari dimensi non-time (single-filtered → ambil posisi kategori tunggalnya)
    base = 0
    for i, dim_id in enumerate(ids):
        if i == tpos:
            continue
        idx_map = dims.get(dim_id, {}).get("category", {}).get("index", {})
        pos = min(idx_map.values()) if idx_map else 0
        if sizes[i] != 1:
            logger.debug("Eurostat: dim %s size=%d (>1) — filter tak lengkap, pakai pos terkecil",
                         dim_id, sizes[i])
        base += pos * strides[i]

    tindex: dict[str, int] = dims.get(time_id, {}).get("category", {}).get("index", {})
    obs: list[tuple[str, float]] = []
    for period, pos in tindex.items():
        flat = base + pos * strides[tpos]
        v = values.get(str(flat), values.get(flat))  # JSON-stat keys = string
        if v is not None:
            try:
                obs.append((str(period), float(v)))
            except (TypeError, ValueError):
                continue

    # Period code "2026-05"/"2026M05" → sort leksikografis desc = kronologis desc
    obs.sort(key=lambda x: x[0], reverse=True)
    return obs


def fetch_series(dataset: str, dims: dict[str, str]) -> list[tuple[str, float]]:
    """Fetch + parse satu seri Eurostat. Return [(period,value)] DESC, [] kalau gagal."""
    js = _get_json(_build_url(dataset, dims))
    if js is None:
        return []
    return _parse_jsonstat_obs(js)


def get_eu_actuals() -> dict[str, dict[str, Any]]:
    """
    Fetch semua indikator EUR sekali (untuk di-cache di app.py).
    Return: {key: {"latest_period","latest_value","prev_value","label","ok"}}.
    Tidak menyentuh events — itu tugas apply_eu_actuals (pure).
    """
    out: dict[str, dict[str, Any]] = {}
    for key, spec in EU_INDICATORS.items():
        obs = fetch_series(spec["dataset"], spec["dims"])
        if len(obs) >= 1:
            latest_p, latest_v = obs[0]
            prev_v = obs[1][1] if len(obs) >= 2 else None
            out[key] = {
                "latest_period": latest_p, "latest_value": latest_v,
                "prev_value": prev_v, "label": spec.get("label", key), "ok": True,
            }
        else:
            out[key] = {"latest_period": None, "latest_value": None,
                        "prev_value": None, "label": spec.get("label", key), "ok": False}
    return out


def _aligned(series_prev: float | None, calendar_prev: float | None) -> bool:
    """Guard: series_prev (bulan lalu dari Eurostat) ≈ previous kalender."""
    if calendar_prev is None:
        return True  # tak ada pembanding → terima tapi caller boleh tandai unverified
    if series_prev is None:
        return False  # tak bisa verifikasi recency → tolak (konservatif)
    diff = abs(series_prev - calendar_prev)
    tol = max(_ALIGN_ABS_TOL, abs(calendar_prev) * _ALIGN_REL_TOL)
    return diff <= tol


def _match_key(name_norm: str) -> str | None:
    for key, spec in EU_INDICATORS.items():
        if any(pat in name_norm for pat in spec["match"]):
            return key
    return None


def apply_eu_actuals(
    events: list[dict[str, Any]],
    actuals: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """
    Set ev["actual"] + ev["actual_source"]="Eurostat" untuk event EUR released yang
    cocok indikator & LOLOS alignment guard. MUTASI in-place. Pure (tak ada network).

    Return diag: {matched, filled, misaligned, no_data, unverified}.
    """
    import re
    diag = {"matched": 0, "filled": 0, "misaligned": 0, "no_data": 0, "unverified": 0}

    for ev in events:
        if ev.get("currency") != "EUR" or ev.get("status") != "released":
            continue
        if ev.get("actual") is not None:
            continue  # sudah terisi (sumber lain) — jangan timpa
        name_norm = re.sub(r"\s+", " ", (ev.get("name") or "").lower()).strip()
        key = _match_key(name_norm)
        if key is None:
            continue
        diag["matched"] += 1

        rec = actuals.get(key)
        if not rec or not rec.get("ok") or rec.get("latest_value") is None:
            diag["no_data"] += 1
            ev["actual_misaligned"] = "eurostat no-data"
            continue

        cal_prev = ev.get("previous")
        if not _aligned(rec.get("prev_value"), cal_prev):
            diag["misaligned"] += 1
            ev["actual_misaligned"] = (
                f"prev seri {rec.get('prev_value')} ≠ kalender {cal_prev}"
            )
            logger.info("Eurostat %s misaligned: series_prev=%s vs cal_prev=%s — TOLAK",
                        key, rec.get("prev_value"), cal_prev)
            continue

        ev["actual"] = float(rec["latest_value"])
        ev["actual_source"] = _ACTUAL_SOURCE
        ev["actual_period"] = rec.get("latest_period")
        diag["filled"] += 1
        if cal_prev is None:
            diag["unverified"] += 1
            ev["actual_unverified"] = True

    return diag
