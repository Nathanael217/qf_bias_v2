"""
engine/scoring.py — Per-Asset Bias Scoring

Mengimplementasikan §3 Scoring Spec dari arsitektur QF_BIAS:
  - score_R_hard  : Makro (surprise z-score + rate differential), kontinu [-1,1].
  - score_C       : COT (cot_index percentile), EKSTREM-ONLY, freshness-weighted.
  - score_D       : Retail sentiment, KONTRARIAN, EKSTREM-ONLY.
  - compute_asset_bias : Driver dict + bias_baseline via weighted-sum renormalisasi.
  - compute_all_assets : Loop semua aset (FX + XAU + Crypto).

KONVENSI TANDA (dikunci — arsitektur §3.1):
  C  : FOLLOWING per v1. cot_index > 80 → C positif (bullish lean).
       ⚠ OPEN: apakah harusnya contrarian di ekstrem? Lihat arsitektur §3.1.
  D  : KONTRARIAN (dikunci). long_pct_agg tinggi → crowd long → D negatif (fade short).

BOBOT: semua PLACEHOLDER. Renormalisasi otomatis atas faktor yang score-nya ≠ 0.
"""

from __future__ import annotations

import logging
from typing import Any

from config import (
    ASSETS_ALL,
    ASSETS_CRYPTO,
    ASSETS_FX,
    ASSET_GOLD,
    COT_CATEGORY,
    COT_EXTREME,
    RETAIL_EXTREME,
    WEIGHTS,
    bias_label,
)
from engine.freshness import cot_freshness

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# R_hard = CARRY MURNI (rate differential). PLACEHOLDER.
# ---------------------------------------------------------------------------
# FIX DOUBLE-COUNT (2026-06-05): komponen surprise z-score DIHAPUS dari R_hard.
# Surprise rilis ekonomi sekarang HANYA hidup di faktor F (engine/ff_surprise),
# di-feed otomatis dari kalender faireconomy. Sebelumnya rilis yang SAMA men-drive
# R_hard (z, internal 0.60) DAN F (0.20) → double-count lintas-faktor. R_hard kini
# murni mengukur carry (selisih suku bunga kebijakan).
_RHARD_CARRY_SCALE: float = 0.40
"""
Skala kontribusi carry ke R_hard. Sengaja = bobot diff lama (0.40) agar magnitude
R_hard standalone TIDAK berubah dari versi pra-fix (mis. EUR carry-only tetap ≈ -18,
bukan melonjak ke -46). Carry dijaga moderat: sinyal lambat, tak boleh teriak
sendirian. ⚠ PLACEHOLDER — backtest bisa membenarkan range penuh.
"""

_RHARD_DIFF_MAX: float = 5.0
"""
Rate differential maksimum (pp) untuk normalisasi ke [-1,1].
Diff > 5pp diperlakukan sebagai sinyal penuh.
⚠ PLACEHOLDER.
"""

# Bobot crypto-khusus — PLACEHOLDER (arsitektur §3 Crypto)
_CRYPTO_WEIGHTS: dict[str, float] = {
    "R_hard": 0.15,    # PLACEHOLDER — rate tidak langsung drive crypto
    "C": 0.35,         # PLACEHOLDER — COT CME BTC lebih relevan untuk crypto
    "D": 0.40,         # PLACEHOLDER — retail L/S kripto sangat sentimen-driven
    "F": 0.10,         # PLACEHOLDER — surprise makro AS (lewat USD) imbas tak langsung
    "R_narrative": 0.00,
}
"""
Bobot per faktor untuk aset crypto (BTC, ETH).
Berbeda dari FX/XAU: R_hard turun, C & D naik karena crypto lebih sentimen/regime-driven.
Lihat arsitektur §3 Crypto. ⚠ SEMUA = PLACEHOLDER.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Ambil nilai nested dict secara aman; return default kalau key tidak ada."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, None)
        if cur is None:
            return default
    return cur


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp value ke [lo, hi]."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# score_R_hard
# ---------------------------------------------------------------------------

def score_R_hard(macro: dict[str, Any], asset: str) -> tuple[float, str]:
    """Hitung sub-skor R_hard (CARRY) untuk satu asset/currency.

    R_hard = rate differential murni (selisih suku bunga kebijakan).
      Dari macro["rate_diff"]. Lookup pair di mana asset = base → positif berarti
      yield asset ini lebih tinggi → bullish carry. Untuk asset = quote (mis. USD
      di EURUSD) diff dinegate ke perspektif asset. Multi-pair (USD/EUR) dirata.
      Di-clamp ke [-DIFF_MAX, DIFF_MAX], dinormalisasi, lalu ×_RHARD_CARRY_SCALE.

    FIX DOUBLE-COUNT: komponen surprise z-score DIHAPUS dari R_hard. Surprise kini
    hidup HANYA di faktor F (engine/ff_surprise). Dulu rilis yang sama men-drive
    R_hard (z) DAN F sekaligus → satu kejutan ekonomi dihitung dua kali.

    Kalau rate_diff tak tersedia → score = 0 (kontribusi berkurang, bukan crash).

    Parameters
    ----------
    macro : dict
        Output dari collectors/macro.py sesuai schema §4.
    asset : str
        Currency/aset (mis. "USD", "EUR", "XAU", "BTC").

    Returns
    -------
    (score, detail_str)
        score ∈ [-1, 1], detail_str untuk display di dashboard.
    """
    # --- Rate differential (carry) — satu-satunya komponen R_hard ---
    diff_norm = 0.0
    diff_detail = "no rate diff data"

    rate_diff_map: dict[str, float] = _safe_get(macro, "rate_diff", default={})
    # Cari pair di mana asset = BASE (mis. EUR dalam "EURUSD")
    # Sekaligus handle kasus asset = QUOTE (mis. USD dalam "EURUSD") → negate diff
    found_diff = False
    acc_diff = 0.0
    count_diff = 0

    for pair_sym, diff_val in rate_diff_map.items():
        # pair_sym = 6 karakter, misal "EURUSD"
        if len(pair_sym) != 6:
            continue
        if diff_val is None:          # FRED rate hilang → skip (jangan crash += None)
            continue
        base_sym = pair_sym[:3]
        quote_sym = pair_sym[3:]
        if base_sym == asset:
            acc_diff += diff_val
            count_diff += 1
        elif quote_sym == asset:
            # Asset adalah quote → diff berlawanan untuk asset ini
            acc_diff += -diff_val
            count_diff += 1

    if count_diff > 0:
        avg_diff = acc_diff / count_diff
        diff_clamped = _clamp(avg_diff, -_RHARD_DIFF_MAX, _RHARD_DIFF_MAX)
        diff_norm = diff_clamped / _RHARD_DIFF_MAX
        diff_detail = f"rate diff avg={avg_diff:.2f}pp"
        found_diff = True
    else:
        logger.debug("score_R_hard[%s]: tidak ada rate_diff data", asset)

    # --- Skor carry ---
    score = _clamp(_RHARD_CARRY_SCALE * diff_norm, -1.0, 1.0)

    detail = f"{diff_detail} → R_hard(carry)={score:.3f}"
    logger.debug("score_R_hard[%s]: diff_norm=%.3f → %.3f", asset, diff_norm, score)
    return score, detail


# ---------------------------------------------------------------------------
# score_C (COT)
# ---------------------------------------------------------------------------

def score_C(
    cot: dict[str, Any],
    asset: str,
    freshness: float,
) -> tuple[float, str]:
    """Hitung sub-skor C (COT) untuk satu asset/currency.

    Gating: COT Index (percentile 0–100) harus berada di LUAR COT_EXTREME = (20, 80).
    Di antara 20–80 → skor = 0 (tidak berkontribusi).

    Konvensi tanda (FOLLOWING per v1 — arsitektur §3.1):
      cot_index > 80 (net spec LONG ekstrem) → C positif (bullish lean).
      cot_index < 20 (net spec SHORT ekstrem) → C negatif (bearish lean).
      ⚠ OPEN: apakah harusnya CONTRARIAN di ekstrem (exhaustion)?
        Biarkan following + tandai placeholder. Jangan flip tanpa backtest.

    Magnitude di ekstrem:
      Skala linear dari threshold ke batas (0 atau 100):
        upper: cot_index dari 80 → 100 di-map ke 0 → +1
        lower: cot_index dari 20 → 0  di-map ke 0 → -1

    Freshness: skor efektif = skor_raw × freshness.
    (Bobot efektif w_C = w_C_nominal × freshness, diterapkan di compute_asset_bias
    agar renormalisasi tetap konsisten — TAPI di sini kita langsung scale skor
    agar driver breakdown mencerminkan kontribusi aktual.)

    Parameters
    ----------
    cot : dict
        Output dari collectors/cot.py sesuai schema §4.
    asset : str
        Currency/aset.
    freshness : float
        Multiplier dari cot_freshness(), ∈ [FRESHNESS_FLOOR, 1.0].

    Returns
    -------
    (score, detail_str)
        score ∈ [-1, 1] (sudah di-scale freshness), detail untuk display.
    """
    cot_lower, cot_upper = COT_EXTREME  # (20, 80)

    cot_data: dict | None = _safe_get(cot, "cot", asset, default=None)
    if cot_data is None:
        logger.debug("score_C[%s]: tidak ada data COT", asset)
        return 0.0, f"no COT data → 0"

    idx_raw = cot_data.get("cot_index")
    if idx_raw is None:
        logger.debug("score_C[%s]: cot_index=None", asset)
        return 0.0, "cot_index=None → 0"

    cot_idx = float(idx_raw)

    # Gating
    if cot_lower <= cot_idx <= cot_upper:
        detail = f"COT index {cot_idx:.1f} (tdk ekstrem, {cot_lower}–{cot_upper}) → 0"
        return 0.0, detail

    # Magnitude scale di ekstrem
    if cot_idx > cot_upper:
        # Bullish ekstrem: 80→100 di-map ke 0→+1
        raw_score = (cot_idx - cot_upper) / (100.0 - cot_upper)
        sign_str = "bullish (following)"
    else:
        # Bearish ekstrem: 20→0 di-map ke 0→-1
        raw_score = -((cot_lower - cot_idx) / (cot_lower - 0.0))
        sign_str = "bearish (following)"

    raw_score = _clamp(raw_score, -1.0, 1.0)

    # CATATAN (fix double-count): freshness TIDAK diterapkan ke skor di sini.
    # Sesuai arsitektur §3 (w_C_effective = w_C × freshness), freshness memodifikasi
    # BOBOT C di compute_asset_bias, bukan skor. score_C mengembalikan skor MENTAH.
    # freshness tetap diterima sebagai argumen agar detail breakdown informatif.
    detail = (
        f"COT index {cot_idx:.1f} → ekstrem {sign_str}; "
        f"raw={raw_score:.3f} (freshness {freshness:.3f} diterapkan ke BOBOT)"
    )
    logger.debug("score_C[%s]: idx=%.1f, raw=%.3f (fresh→weight=%.3f)", asset, cot_idx, raw_score, freshness)
    return raw_score, detail


# ---------------------------------------------------------------------------
# score_D (Retail Sentiment — KONTRARIAN)
# ---------------------------------------------------------------------------

def score_D(retail: dict[str, Any], asset: str) -> tuple[float, str]:
    """Hitung sub-skor D (Retail Sentiment) untuk satu asset/currency.

    KONTRARIAN (dikunci — arsitektur §3.1 & §3.2):
      long_pct_agg tinggi → crowd long → D NEGATIF (fade: bearish bias).
      long_pct_agg rendah → crowd short → D POSITIF (fade: bullish bias).

    Gating RETAIL_EXTREME (default 70):
      Aktif hanya kalau:
        long_pct_agg > RETAIL_EXTREME           (crowd sangat long)
        long_pct_agg < (100 - RETAIL_EXTREME)   (crowd sangat short)
      Di luar rentang → D = 0.

    Granularitas (arsitektur §3.2):
      Retail NATIVE per-pair. Untuk currency multi-pair (USD, EUR, dst)
      diperlukan agregasi lintas pair — BELUM diimplementasikan v1.
      Kalau asset tersedia langsung (XAU, BTC, ETH) → pakai XAUUSD/BTCUSD/ETHUSD.
      Untuk FX currency → lookup pair paling liquid (EURUSD untuk EUR, dst).
      Kalau tidak ada data → D=0 + catat.

    Magnitude scale di ekstrem (linear):
      upper side: long_pct dari RETAIL_EXTREME → 100 di-map ke 0 → -1 (kontrarian short)
      lower side: long_pct dari (100-RETAIL_EXTREME) → 0 di-map ke 0 → +1 (kontrarian long)

    Parameters
    ----------
    retail : dict
        Output dari collectors/retail.py sesuai schema §4.
    asset : str
        Currency/aset.

    Returns
    -------
    (score, detail_str)
        score ∈ [-1, 1], detail untuk display.
    """
    # Mapping asset ke pair retail yang dipakai (PLACEHOLDER — lihat §3.2)
    # Multi-pair currency: pakai pair paling representative. Ini simplifikasi v1.
    _ASSET_TO_RETAIL_PAIR: dict[str, str] = {
        "EUR": "EURUSD",
        "GBP": "GBPUSD",
        "JPY": "USDJPY",  # Note: USDJPY → USD=base, JPY=quote; long% = long USD/short JPY
        "AUD": "AUDUSD",
        "NZD": "NZDUSD",
        "CAD": "USDCAD",  # Note: USDCAD → long% = long USD/short CAD
        "CHF": "USDCHF",  # Note: USDCHF → long% = long USD/short CHF
        "USD": "EURUSD",  # Proxy: ambil kebalikan EURUSD (short EUR = long USD)
        "XAU": "XAUUSD",
        "BTC": "BTCUSD",
        "ETH": "ETHUSD",
    }

    # Pair di mana asset bukan BASE (USD = quote di EURUSD, tapi base di USDJPY)
    # → harus negate long_pct ke perspektif asset
    _PAIR_QUOTE_ASSETS: dict[str, str] = {
        # pair: currency yang bukan base tapi kita sedang hitung score-nya
        "USDJPY": "JPY",    # long% USDJPY = long USD → untuk JPY, negate
        "USDCAD": "CAD",
        "USDCHF": "CHF",
        "EURUSD": "USD",    # proxy USD: long% EURUSD = long EUR; USD = short → negate
    }

    retail_map: dict = _safe_get(retail, "retail", default={})

    pair_key = _ASSET_TO_RETAIL_PAIR.get(asset)
    if pair_key is None:
        logger.debug("score_D[%s]: tidak ada mapping retail pair", asset)
        return 0.0, f"no retail pair mapping → 0"

    pair_data: dict | None = retail_map.get(pair_key)
    if pair_data is None:
        logger.debug("score_D[%s]: pair %s tidak ada di retail data", asset, pair_key)
        return 0.0, f"pair {pair_key} tidak tersedia → D=0"

    long_pct_raw = pair_data.get("long_pct_agg")
    if long_pct_raw is None:
        return 0.0, f"long_pct_agg=None untuk {pair_key} → D=0"

    long_pct = float(long_pct_raw)

    # Kalau pair adalah quote-perspective, negate long_pct ke sudut pandang asset
    # Contoh: EURUSD long%=62 berarti 62% long EUR / 38% long USD
    #         Untuk USD: long_pct_from_usd_view = 100 - 62 = 38%
    if _PAIR_QUOTE_ASSETS.get(pair_key) == asset:
        long_pct = 100.0 - long_pct

    extreme_hi = float(RETAIL_EXTREME)           # default 70
    extreme_lo = 100.0 - extreme_hi              # default 30

    # Gating
    if extreme_lo <= long_pct <= extreme_hi:
        detail = (
            f"retail {pair_key} long_pct={long_pct:.1f}% "
            f"(tdk ekstrem, {extreme_lo:.0f}–{extreme_hi:.0f}%) → D=0"
        )
        return 0.0, detail

    # Magnitude scale + kontrarian sign
    if long_pct > extreme_hi:
        # Crowd sangat long → contrarian short → D negatif
        magnitude = (long_pct - extreme_hi) / (100.0 - extreme_hi)
        raw_score = -magnitude
        crowd_str = f"long {long_pct:.1f}% → contrarian short"
    else:
        # Crowd sangat short → contrarian long → D positif
        magnitude = (extreme_lo - long_pct) / extreme_lo
        raw_score = +magnitude
        crowd_str = f"long {long_pct:.1f}% (crowd short) → contrarian long"

    score = _clamp(raw_score, -1.0, 1.0)
    agreement = pair_data.get("agreement", None)
    agreement_str = f", agreement={agreement:.2f}" if agreement is not None else ""

    detail = (
        f"retail {pair_key} {crowd_str}{agreement_str}; "
        f"score={score:.3f}"
    )
    logger.debug("score_D[%s]: pair=%s, long_pct=%.1f → %.3f", asset, pair_key, long_pct, score)
    return score, detail


# ---------------------------------------------------------------------------
# compute_asset_bias
# ---------------------------------------------------------------------------

def compute_asset_bias(
    asset: str,
    macro: dict[str, Any],
    cot: dict[str, Any],
    retail: dict[str, Any],
    prices: dict[str, Any],
    weights_override: dict[str, float] | None = None,
    ff_scores: dict[str, dict] | None = None,
    retail_override: dict[str, dict] | None = None,
    enabled: set[str] | None = None,
) -> dict[str, Any]:
    """Hitung bias per satu aset: driver dict + bias_baseline.

    Mengimplementasikan formula arsitektur §3:
      asset_bias = Σ(w_i × score_i) / Σ(w_i untuk score_i ≠ 0)
      → skala [-1,1] → ×100 untuk bias_baseline (display).

    Renormalisasi otomatis: kalau C atau D = 0 (gated/tidak ekstrem),
    bobot mereka didistribusikan proporsional ke faktor yang aktif.
    Ini memastikan bias_baseline selalu ∈ [-100, 100] meski banyak faktor nol.

    Parameters
    ----------
    asset : str
        Currency/aset yang dihitung.
    macro, cot, retail, prices : dict
        Output masing-masing collector sesuai schema §4.
    weights_override : dict, optional
        Override bobot default (untuk crypto, pakai _CRYPTO_WEIGHTS).

    Returns
    -------
    dict dengan struktur:
        {
          "drivers": {
              "R_hard": {"score": float, "weight": float, "detail": str},
              "C":      {"score": float, "weight": float, "detail": str},
              "D":      {"score": float, "weight": float, "detail": str},
          },
          "bias_baseline": float,  # ×100, ∈ [-100, 100]
          "active_factors": list[str],
          "weights_used": dict,
        }
    """
    w = weights_override if weights_override is not None else WEIGHTS

    # --- Freshness COT ---
    days_since = float(_safe_get(cot, "days_since_snapshot", default=7))

    # Price change sejak snapshot: pakai chg_pct × last sebagai proxy nilai absolut
    # Alternatif: simpan harga snapshot di cot — v1 pakai proxy ini.
    # Pair harga yang relevan per asset
    _ASSET_TO_PRICE_KEY: dict[str, str] = {
        "EUR": "EURUSD", "GBP": "GBPUSD", "JPY": "USDJPY",
        "AUD": "AUDUSD", "NZD": "NZDUSD", "CAD": "USDCAD",
        "CHF": "USDCHF", "USD": "DXY",
        "XAU": "XAUUSD", "BTC": "BTCUSD", "ETH": "ETHUSD",
    }
    price_key = _ASSET_TO_PRICE_KEY.get(asset, "")
    px_data: dict = _safe_get(prices, "prices", price_key, default={})
    # .get(k, default) mengembalikan None kalau value EKSPLISIT None (bukan default).
    # Guard: None / non-numeric → 0.0 (jangan crash float(None)).
    def _num(v: Any, d: float = 0.0) -> float:
        try:
            return float(v) if v is not None else d
        except (TypeError, ValueError):
            return d
    last_price = _num(px_data.get("last"))
    chg_pct = _num(px_data.get("chg_pct"))
    atr14 = _num(px_data.get("atr14"))

    # Price change absolut (proxy: chg_pct/100 × last)
    price_change_abs = abs(chg_pct / 100.0 * last_price) if last_price > 0 else 0.0

    freshness = cot_freshness(days_since, price_change_abs, atr14)

    # --- Hitung sub-skor ---
    r_hard_score, r_hard_detail = score_R_hard(macro, asset)
    c_score,      c_detail      = score_C(cot, asset, freshness)
    # D: pakai override retail A1 kalau tersedia, else score_D (sumber lama)
    if retail_override and asset in retail_override:
        d_score = float(retail_override[asset].get("score", 0.0))
        d_detail = retail_override[asset].get("detail", "retail A1 (kontrarian)")
    else:
        d_score, d_detail = score_D(retail, asset)
    # F: ForexFactory surprise (0 kalau kalender FF belum ditarik)
    if ff_scores and asset in ff_scores:
        f_score = float(ff_scores[asset].get("score", 0.0))
        f_detail = ff_scores[asset].get("detail", "FF surprise")
    else:
        f_score, f_detail = 0.0, "tak ada FF surprise (tarik kalender FF utk aktifkan)"

    scores = {
        "R_hard": r_hard_score,
        "C":      c_score,
        "D":      d_score,
        "F":      f_score,
    }
    details = {
        "R_hard": r_hard_detail,
        "C":      c_detail,
        "D":      d_detail,
        "F":      f_detail,
    }

    # --- Renormalisasi atas faktor non-zero ---
    # Freshness COT memodifikasi BOBOT C (w_C_effective = w_C × freshness),
    # BUKAN skor C. Lihat arsitektur §3 + fix double-count. Faktor lain pakai bobot nominal.
    active: list[str] = []
    numerator = 0.0
    denom_w = 0.0

    for factor, score in scores.items():
        weight = w.get(factor, 0.0)
        if enabled is not None and factor not in enabled:
            weight = 0.0   # faktor di-OFF-kan user → keluar dari renormalisasi (netral)
        if factor == "C":
            weight = weight * freshness   # bobot efektif COT
        if score != 0.0 and weight > 0.0:
            active.append(factor)
            numerator += weight * score
            denom_w += weight

    if denom_w > 0.0:
        raw_bias = numerator / denom_w
    else:
        # Semua faktor nol (tidak ada data) → bias = 0
        raw_bias = 0.0
        logger.warning("compute_asset_bias[%s]: semua faktor nol atau bobot 0", asset)

    bias_baseline = round(_clamp(raw_bias * 100.0, -100.0, 100.0), 2)

    # --- Bangun driver dict ---
    # Bobot yang "efektif" ditampilkan (nominal; renorm tidak ubah bobot display)
    drivers: dict[str, dict] = {}
    for factor in ["R_hard", "C", "D", "F"]:
        nominal_w = w.get(factor, 0.0)
        eff_w = nominal_w * freshness if factor == "C" else nominal_w
        if enabled is not None and factor not in enabled:
            eff_w = 0.0
        drivers[factor] = {
            "score":  round(scores[factor], 4),
            "weight": round(eff_w, 4),          # bobot EFEKTIF (C ×freshness; OFF→0)
            "weight_nominal": nominal_w,
            "detail": details[factor],
        }

    return {
        "drivers":        drivers,
        "bias_baseline":  bias_baseline,
        "active_factors": active,
        "weights_used":   {k: w.get(k, 0.0) for k in ["R_hard", "C", "D", "F"]},
        "freshness_cot":  round(freshness, 4),
    }


# ---------------------------------------------------------------------------
# compute_all_assets
# ---------------------------------------------------------------------------

def compute_all_assets(
    macro: dict[str, Any],
    cot: dict[str, Any],
    retail: dict[str, Any],
    prices: dict[str, Any],
    ff_scores: dict[str, dict] | None = None,
    retail_override: dict[str, dict] | None = None,
    enabled: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Hitung bias untuk semua aset di ASSETS_ALL.

    FX majors + XAU → pakai WEIGHTS default.
    Crypto (BTC, ETH) → pakai _CRYPTO_WEIGHTS (R_hard turun, C/D naik).
    ETH tidak punya COT CME → C akan 0 (graceful).

    Returns
    -------
    dict[asset_str, result_dict]
        Tiap value = output compute_asset_bias() + "asset" key.
    """
    results: dict[str, dict] = {}

    for asset in ASSETS_ALL:
        is_crypto = asset in ASSETS_CRYPTO
        w_override = _CRYPTO_WEIGHTS if is_crypto else None

        try:
            result = compute_asset_bias(
                asset=asset,
                macro=macro,
                cot=cot,
                retail=retail,
                prices=prices,
                weights_override=w_override,
                ff_scores=ff_scores,
                retail_override=retail_override,
                enabled=enabled,
            )
        except Exception as exc:
            logger.error("compute_asset_bias[%s] gagal: %s", asset, exc, exc_info=True)
            result = {
                "drivers": {
                    "R_hard": {"score": 0.0, "weight": 0.0, "detail": f"ERROR: {exc}"},
                    "C":      {"score": 0.0, "weight": 0.0, "detail": "ERROR"},
                    "D":      {"score": 0.0, "weight": 0.0, "detail": "ERROR"},
                    "F":      {"score": 0.0, "weight": 0.0, "detail": "ERROR"},
                },
                "bias_baseline":  0.0,
                "active_factors": [],
                "weights_used":   {},
                "freshness_cot":  0.0,
                "error":          str(exc),
            }

        result["asset"] = asset
        result["is_crypto"] = is_crypto
        result["bias_label"] = bias_label(result["bias_baseline"])
        results[asset] = result

    return results


# ---------------------------------------------------------------------------
# Contoh pemanggilan
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")

    # -------------------------------------------------------------------------
    # Dict dummy sesuai schema §4
    # -------------------------------------------------------------------------
    _macro = {
        "as_of_utc": "2026-06-01T07:00:00Z",
        "rates": {
            "USD": 4.55, "EUR": 2.25, "GBP": 4.10, "JPY": 0.50,
            "AUD": 3.85, "NZD": 3.00, "CAD": 3.25, "CHF": 1.00,
        },
        "rate_diff": {
            "EURUSD": -2.30,  # EUR rate - USD rate
            "GBPUSD": -0.45,
            "USDJPY": 4.05,   # USD rate - JPY rate (USD base)
            "AUDUSD": -0.70,
            "NZDUSD": -1.55,
            "USDCAD": 1.30,
            "USDCHF": 3.55,
        },
        "surprises": {
            "USD": [
                {"event": "Core PCE m/m", "actual": 0.30, "forecast": 0.26, "z": 1.2, "ts_utc": "2026-05-30T13:30:00Z"},
            ],
            "EUR": [
                {"event": "CPI Flash y/y", "actual": 2.1, "forecast": 2.3, "z": -0.8, "ts_utc": "2026-05-29T10:00:00Z"},
            ],
            "GBP": [],
            "JPY": [
                {"event": "CPI Tokyo y/y", "actual": 2.8, "forecast": 2.5, "z": 0.9, "ts_utc": "2026-05-30T08:00:00Z"},
            ],
            "XAU": [],
            "BTC": [],
            "ETH": [],
        },
    }

    _cot = {
        "as_of_tuesday": "2026-05-26",
        "released": "2026-05-29",
        "days_since_snapshot": 6,  # Senin berikutnya
        "cot": {
            "USD": {"category": "leveraged_funds", "net": -12345, "cot_index": 72.5},  # tidak ekstrem → C=0
            "EUR": {"category": "leveraged_funds", "net": 45000,  "cot_index": 85.0},  # ekstrem bullish
            "GBP": {"category": "leveraged_funds", "net": -8000,  "cot_index": 18.0},  # ekstrem bearish
            "JPY": {"category": "leveraged_funds", "net": 20000,  "cot_index": 55.0},  # tidak ekstrem
            "AUD": {"category": "leveraged_funds", "net": -5000,  "cot_index": 30.0},  # tidak ekstrem
            "NZD": {"category": "leveraged_funds", "net": -2000,  "cot_index": 25.0},  # batas bawah
            "CAD": {"category": "leveraged_funds", "net": 3000,   "cot_index": 15.0},  # ekstrem bearish
            "CHF": {"category": "leveraged_funds", "net": -1000,  "cot_index": 45.0},  # tidak ekstrem
            "XAU": {"category": "managed_money",   "net": 61400,  "cot_index": 82.0},  # ekstrem bullish
            "BTC": {"category": "leveraged_funds", "net": 5000,   "cot_index": 78.0},  # batas atas, belum ekstrem
        },
    }

    _retail = {
        "as_of_utc": "2026-06-01T06:00:00Z",
        "sources_ok": ["myfxbook", "fxssi"],
        "sources_failed": ["dukascopy"],
        "retail": {
            "EURUSD": {"by_source": {"myfxbook": 62, "fxssi": 58}, "long_pct_agg": 60.0, "agreement": 0.92},
            "GBPUSD": {"by_source": {"myfxbook": 35, "fxssi": 32}, "long_pct_agg": 33.5, "agreement": 0.88},  # ekstrem short → D positif
            "USDJPY": {"by_source": {"myfxbook": 45, "fxssi": 48}, "long_pct_agg": 46.5, "agreement": 0.94},
            "AUDUSD": {"by_source": {"myfxbook": 72, "fxssi": 75}, "long_pct_agg": 73.5, "agreement": 0.96},  # ekstrem long → D negatif
            "NZDUSD": {"by_source": {"myfxbook": 55, "fxssi": 58}, "long_pct_agg": 56.5, "agreement": 0.90},
            "USDCAD": {"by_source": {"myfxbook": 40, "fxssi": 38}, "long_pct_agg": 39.0, "agreement": 0.95},
            "USDCHF": {"by_source": {"myfxbook": 50, "fxssi": 52}, "long_pct_agg": 51.0, "agreement": 0.96},
            "XAUUSD": {"by_source": {"myfxbook": 78}, "long_pct_agg": 78.0, "agreement": 1.0},  # ekstrem long → D negatif
            "BTCUSD": {"by_source": {"myfxbook": 65, "fxssi": 68}, "long_pct_agg": 66.5, "agreement": 0.95},
            "ETHUSD": {"by_source": {"myfxbook": 60, "fxssi": 62}, "long_pct_agg": 61.0, "agreement": 0.97},
        },
    }

    _prices = {
        "as_of_utc": "2026-06-01T07:00:00Z",
        "prices": {
            "EURUSD": {"last": 1.0850, "chg_pct": 0.33,  "atr14": 0.0060},
            "GBPUSD": {"last": 1.2710, "chg_pct": -0.15, "atr14": 0.0075},
            "USDJPY": {"last": 152.50, "chg_pct": 0.12,  "atr14": 0.80},
            "AUDUSD": {"last": 0.6530, "chg_pct": -0.40, "atr14": 0.0055},
            "NZDUSD": {"last": 0.5980, "chg_pct": 0.05,  "atr14": 0.0048},
            "USDCAD": {"last": 1.3620, "chg_pct": 0.10,  "atr14": 0.0065},
            "USDCHF": {"last": 0.9010, "chg_pct": -0.08, "atr14": 0.0050},
            "XAUUSD": {"last": 4528.2, "chg_pct": 1.81,  "atr14": 35.0},
            "BTCUSD": {"last": 73608,  "chg_pct": -1.0,  "atr14": 2100},
            "ETHUSD": {"last": 3850,   "chg_pct": 0.50,  "atr14": 120},
            "DXY":    {"last": 98.99,  "chg_pct": -0.22, "atr14": 0.45},
        },
    }

    # -------------------------------------------------------------------------
    # Jalankan
    # -------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("  QF_BIAS — compute_all_assets() — CONTOH OUTPUT")
    print("=" * 70)

    results = compute_all_assets(_macro, _cot, _retail, _prices)

    for asset, r in results.items():
        print(f"\n{'─'*60}")
        print(f"  {asset:>4}  │  bias_baseline={r['bias_baseline']:>7.2f}  │  label={r['bias_label']}")
        print(f"         │  active={r['active_factors']}  │  freshness_cot={r['freshness_cot']}")
        for factor, d in r["drivers"].items():
            print(f"         │  {factor:<10} score={d['score']:>7.4f}  w={d['weight']}  → {d['detail']}")

    print("\n" + "=" * 70)
    print("Selesai. Semua bobot = PLACEHOLDER. Backtest sebelum pakai sizing nyata.")
