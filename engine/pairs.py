"""
engine/pairs.py — Pair Bias Computation

Mengimplementasikan §3 Pair spec dari arsitektur QF_BIAS:
  pair_bias(BASE/QUOTE) = asset_bias(BASE) − asset_bias(QUOTE)
  → clamp ke [-100, 100].
  → label WAJIB dari config.bias_label() — JANGAN hardcode threshold sendiri.

Input: dict {asset: {"bias_baseline": float, ...}} dari engine/scoring.compute_all_assets().
Output: dict {pair_symbol: {"bias_score", "label", "confidence", "base", "quote"}}.

Kalau salah satu currency tidak ada di asset_bias_map → pair_bias = 0 + catat.
Confidence pair = rata-rata confidence base dan quote kalau tersedia, else None.
"""

from __future__ import annotations

import logging
from typing import Any

from config import PAIR_META, bias_label

logger = logging.getLogger(__name__)


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp value ke [lo, hi]."""
    return max(lo, min(hi, value))


def compute_pairs(
    asset_bias_map: dict[str, dict[str, Any]],
    pairs_override: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Hitung bias untuk semua pair yang terdaftar di config.PAIR_META.

    Formula:
        pair_bias = asset_bias(BASE) − asset_bias(QUOTE)   → clamp [-100, 100]
        label     = config.bias_label(pair_bias)   ← SINGLE SOURCE OF TRUTH

    Parameters
    ----------
    asset_bias_map : dict
        Output dari compute_all_assets(). Key = asset string ("USD", "EUR", dst),
        value = dict dengan minimal field:
          - "bias_baseline" : float ∈ [-100, 100]
          - "confidence"    : float ∈ [0, 1]  (opsional; dipakai kalau ada)
    pairs_override : list[str], optional
        Kalau diisi, hitung hanya pair dalam list ini.
        Default: semua pair di PAIR_META.

    Returns
    -------
    dict[str, dict]
        Key = simbol pair (mis. "EURUSD").
        Value = {
            "bias_score"  : float,    # ∈ [-100, 100], clamped
            "label"       : str,      # dari config.bias_label()
            "base"        : str,
            "quote"       : str,
            "base_bias"   : float,    # bias_baseline base asset
            "quote_bias"  : float,    # bias_baseline quote asset
            "confidence"  : float | None,   # rata-rata confidence base+quote
            "ok"          : bool,     # False kalau salah satu asset tidak ada
            "note"        : str,      # detail/warning
        }

    Notes
    -----
    - JANGAN menambahkan threshold label sendiri di sini.
      config.bias_label() adalah satu-satunya tempat definisi threshold.
      Lihat arsitektur §3.3 Label.
    - Pair bisa bernilai 0 kalau kedua asset bias sama persis (bukan error).
    - Confidence pair = None kalau tidak ada data confidence di asset_bias_map.
    """
    target_pairs = pairs_override if pairs_override is not None else list(PAIR_META.keys())
    results: dict[str, dict] = {}

    for pair_sym in target_pairs:
        meta = PAIR_META.get(pair_sym)
        if meta is None:
            logger.warning("compute_pairs: pair %s tidak ada di PAIR_META — dilewati", pair_sym)
            continue

        base = meta["base"]
        quote = meta["quote"]

        base_data  = asset_bias_map.get(base)
        quote_data = asset_bias_map.get(quote)

        # Graceful: kalau asset tidak ada
        missing: list[str] = []
        if base_data is None:
            missing.append(base)
        if quote_data is None:
            missing.append(quote)

        if missing:
            logger.warning(
                "compute_pairs[%s]: asset tidak ada di map: %s → pair_bias=0",
                pair_sym, missing,
            )
            results[pair_sym] = {
                "bias_score": 0.0,
                "label":      bias_label(0.0),
                "base":       base,
                "quote":      quote,
                "base_bias":  0.0,
                "quote_bias": 0.0,
                "confidence": None,
                "ok":         False,
                "note":       f"asset tidak tersedia: {missing}",
            }
            continue

        base_bias  = float(base_data.get("bias_baseline", 0.0))
        quote_bias = float(quote_data.get("bias_baseline", 0.0))

        pair_raw = base_bias - quote_bias
        pair_score = round(_clamp(pair_raw, -100.0, 100.0), 2)

        # Confidence: rata-rata confidence base + quote (kalau ada)
        conf_base  = base_data.get("confidence")
        conf_quote = quote_data.get("confidence")
        if conf_base is not None and conf_quote is not None:
            confidence: float | None = round((float(conf_base) + float(conf_quote)) / 2.0, 4)
        elif conf_base is not None:
            confidence = round(float(conf_base), 4)
        elif conf_quote is not None:
            confidence = round(float(conf_quote), 4)
        else:
            confidence = None

        # Label: WAJIB dari config.bias_label() — tidak ada threshold lain
        label = bias_label(pair_score)

        note = f"{base}={base_bias:.2f} − {quote}={quote_bias:.2f} = {pair_raw:.2f}"
        if pair_raw != pair_score:
            note += f" (clamped dari {pair_raw:.2f})"

        results[pair_sym] = {
            "bias_score": pair_score,
            "label":      label,
            "base":       base,
            "quote":      quote,
            "base_bias":  round(base_bias, 2),
            "quote_bias": round(quote_bias, 2),
            "confidence": confidence,
            "ok":         True,
            "note":       note,
        }

        logger.debug(
            "compute_pairs[%s]: %s=%.2f − %s=%.2f → %.2f [%s]",
            pair_sym, base, base_bias, quote, quote_bias, pair_score, label,
        )

    return results


def rank_pairs(
    pair_result: dict[str, dict[str, Any]],
    top_n: int = 10,
    abs_sort: bool = True,
) -> list[dict[str, Any]]:
    """Ranking pair berdasarkan kekuatan bias.

    Parameters
    ----------
    pair_result : dict
        Output compute_pairs().
    top_n : int
        Berapa pair teratas yang dikembalikan.
    abs_sort : bool
        True → sort berdasarkan |bias_score| (kekuatan, apapun arahnya).
        False → sort berdasarkan bias_score mentah (positif terkuat dulu).

    Returns
    -------
    list[dict]
        List dict hasil, tiap entry = {"pair": str, **pair_data},
        diurutkan dari kekuatan bias tertinggi.
    """
    valid = [
        {"pair": sym, **data}
        for sym, data in pair_result.items()
        if data.get("ok", False)
    ]

    key_fn = (lambda x: abs(x["bias_score"])) if abs_sort else (lambda x: x["bias_score"])
    ranked = sorted(valid, key=key_fn, reverse=True)
    return ranked[:top_n]


# ---------------------------------------------------------------------------
# Contoh pemanggilan
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import os

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")

    # Mock asset_bias_map (biasanya dari compute_all_assets)
    _mock_asset_bias: dict[str, dict] = {
        "USD": {"bias_baseline": 28.0,  "confidence": 0.62},
        "EUR": {"bias_baseline": -15.0, "confidence": 0.45},
        "GBP": {"bias_baseline": -42.0, "confidence": 0.70},
        "JPY": {"bias_baseline": 10.0,  "confidence": 0.38},
        "AUD": {"bias_baseline": -22.0, "confidence": 0.55},
        "NZD": {"bias_baseline": 5.0,   "confidence": 0.30},
        "CAD": {"bias_baseline": -18.0, "confidence": 0.50},
        "CHF": {"bias_baseline": 8.0,   "confidence": 0.40},
        "XAU": {"bias_baseline": 35.0,  "confidence": 0.65},
        "BTC": {"bias_baseline": -10.0, "confidence": 0.35},
        "ETH": {"bias_baseline": -5.0,  "confidence": 0.28},
    }

    pair_results = compute_pairs(_mock_asset_bias)

    print("\n" + "=" * 70)
    print("  QF_BIAS — compute_pairs() — CONTOH OUTPUT")
    print("=" * 70)
    print(f"\n{'Pair':<10} {'Score':>8} {'Label':<15} {'Conf':>6}  Kalkulasi")
    print("-" * 70)

    for sym, data in pair_results.items():
        conf_str = f"{data['confidence']:.2f}" if data["confidence"] is not None else " N/A"
        ok_str   = "" if data["ok"] else " ⚠ MISSING"
        print(
            f"{sym:<10} {data['bias_score']:>8.2f} {data['label']:<15} {conf_str:>6}"
            f"  {data['note']}{ok_str}"
        )

    print("\n--- Top 5 pair (sorted by |bias|) ---")
    for r in rank_pairs(pair_results, top_n=5):
        print(f"  {r['pair']:<10} {r['bias_score']:>8.2f}  [{r['label']}]")

    print("\n" + "=" * 70)
    print("Label threshold: didelegasikan ke config.bias_label() — TIDAK hardcode di sini.")
