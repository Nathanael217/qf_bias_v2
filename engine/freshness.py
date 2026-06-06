"""
engine/freshness.py — COT Freshness Decay Weighting

Menghitung multiplier freshness untuk skor C (COT), berdasarkan:
  1. Hari sejak snapshot Selasa (decay eksponensial).
  2. Penalty divergence harga vs snapshot (kalau harga bergerak jauh sejak COT diambil,
     posisi COT mungkin sudah tidak relevan).

Formula (dari arsitektur §3 Freshness COT):
  freshness = clamp(exp(-(t - 3) / τ), FLOOR, 1.0)
  kalau |price_change| > K × atr14 → freshness ×0.5

Semua konstanta = PLACEHOLDER (lihat config.py & arsitektur §0 Prinsip 9).
"""

from __future__ import annotations

import math
import logging

from config import FRESHNESS_TAU, FRESHNESS_FLOOR, ATR_DIVERGENCE_K

logger = logging.getLogger(__name__)


def cot_freshness(
    days_since_snapshot: float,
    price_change: float,
    atr14: float,
    *,
    tau: float = FRESHNESS_TAU,
    floor: float = FRESHNESS_FLOOR,
) -> float:
    """Hitung freshness multiplier COT untuk digunakan di score_C.

    Parameters
    ----------
    days_since_snapshot:
        Jumlah hari antara tanggal snapshot Selasa COT dan hari ini.
        Misal: rilis Jumat = t=3, Senin berikutnya = t=6, dst.
        Nilai 0 atau negatif diperlakukan sebagai 0 (freshness maksimum).
    price_change:
        Perubahan harga aset sejak tanggal snapshot (nilai absolut maupun signed;
        fungsi mengambil |price_change| secara internal).
        Satuan sama dengan atr14 (pips/USD/dsb — harus konsisten).
    atr14:
        Average True Range 14 periode dari prices collector.
        Dipakai sebagai baseline volatilitas untuk mengukur signifikansi price_change.
        Kalau 0 atau negatif (data hilang), penalty divergence diabaikan.
    tau : float, keyword-only
        Time-constant (hari) untuk decay eksponensial. Default = FRESHNESS_TAU (6.0).
        PLACEHOLDER — bisa di-override per profil tipe trade.
    floor : float, keyword-only
        Batas bawah freshness multiplier. Default = FRESHNESS_FLOOR (0.25).
        PLACEHOLDER — bisa di-override per profil tipe trade.

    Returns
    -------
    float
        Freshness multiplier ∈ [floor, 1.0].
        Nilai 1.0 = data sangat segar. floor = minimum (tidak pernah nol).

    Notes
    -----
    - τ (tau) default = FRESHNESS_TAU (6 hari) → PLACEHOLDER.
    - floor default = FRESHNESS_FLOOR (0.25) → PLACEHOLDER.
    - ATR_DIVERGENCE_K = 1.5 → PLACEHOLDER.
    - Penalty divergence bersifat binary (×0.5 kalau lewat threshold),
      bukan kontinyu — sesuai spesifikasi arsitektur v1.
    - Default tau/floor identik dengan versi sebelumnya → backward-compatible.
    """
    # 1. Decay eksponensial sejak hari ke-3 (hari rilis normal COT = Jumat = t≈3)
    t = max(0.0, float(days_since_snapshot))
    raw_freshness = math.exp(-(t - 3.0) / tau)

    # Clamp ke [floor, 1.0]
    freshness = max(floor, min(1.0, raw_freshness))

    # 2. Penalty divergence harga
    if atr14 > 0.0:
        abs_change = abs(price_change)
        threshold = ATR_DIVERGENCE_K * atr14
        if abs_change > threshold:
            logger.debug(
                "COT divergence penalty aktif: |price_change|=%.5f > %.1f×ATR14=%.5f",
                abs_change,
                ATR_DIVERGENCE_K,
                atr14,
            )
            freshness = max(floor, freshness * 0.5)
    else:
        logger.debug(
            "atr14=%.5f ≤ 0 — divergence penalty dilewati (data hilang).",
            atr14,
        )

    logger.debug(
        "cot_freshness: t=%.1f days, tau=%.1f, floor=%.2f → raw=%.4f → final=%.4f",
        t,
        tau,
        floor,
        raw_freshness,
        freshness,
    )
    return freshness


# ---------------------------------------------------------------------------
# Contoh pemanggilan
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    cases = [
        # (days, price_change, atr14,  keterangan)
        (3,   0.00050, 0.0060,  "Rilis Jumat, harga tenang → freshness tinggi"),
        (6,   0.00050, 0.0060,  "Senin berikutnya, harga tenang → decay sedang"),
        (10,  0.00050, 0.0060,  "10 hari stale, harga tenang → decay lanjut"),
        (3,   0.0110,  0.0060,  "Rilis Jumat, harga gap 1.1×ATR (di bawah K×ATR=0.009? cek)"),
        (3,   0.0100,  0.0060,  "Rilis Jumat, harga bergerak 1×ATR14 (masih < K×ATR=0.009)"),
        (3,   0.0095,  0.0060,  "Rilis Jumat, |change|=0.0095 > K×ATR=0.009 → penalty"),
        (6,   0.0100,  0.0060,  "6 hari + divergence tinggi → ganda penalty"),
        (0,   0.0,     0.0060,  "t=0 (snapshot hari ini) → freshness = 1.0"),
        (3,   0.0050,  0.000,   "atr14=0 → penalty dilewati"),
    ]

    print(f"\n{'days':>5} {'chg':>9} {'atr14':>8} {'freshness':>10}  keterangan")
    print("-" * 70)
    for days, chg, atr14, note in cases:
        f = cot_freshness(days, chg, atr14)
        print(f"{days:>5} {chg:>9.5f} {atr14:>8.5f} {f:>10.4f}  {note}")

    print("\n--- Test tau/floor override ---")
    f1 = cot_freshness(6, 0.00050, 0.0060, tau=3.0, floor=0.0)
    f2 = cot_freshness(6, 0.00050, 0.0060, tau=6.0, floor=0.25)
    print(f"tau=3.0 floor=0.0  → {f1:.4f}")
    print(f"tau=6.0 floor=0.25 → {f2:.4f} (default)")
