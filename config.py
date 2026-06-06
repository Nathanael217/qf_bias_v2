"""
config.py — QF_BIAS master configuration.

KONTRAK: semua konstanta bertanda # PLACEHOLDER wajib divalidasi via
backtest/forward-test sebelum dipakai sizing nyata. Lihat arsitektur §0 Prinsip 9.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# ASSET UNIVERSE
# ---------------------------------------------------------------------------

ASSETS_FX: list[str] = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"]
"""Delapan major FX currencies. Bias dihitung per-currency, bukan per-pair."""

ASSET_GOLD: str = "XAU"
"""Gold. Diperlakukan seperti FX major tapi dengan kategori COT managed_money."""

ASSETS_CRYPTO: list[str] = ["BTC", "ETH"]
"""
Crypto: bias = slice makro/sentimen saja.
Intelijen utama tetap di stack on-chain QUANTFLOW — JANGAN rebuild di sini.
"""

ASSETS_ALL: list[str] = ASSETS_FX + [ASSET_GOLD] + ASSETS_CRYPTO
"""Daftar lengkap semua aset yang di-score. Urutan tidak signifikan."""

# DXY bukan aset yang di-score, tapi dipakai sebagai regime context.
REGIME_TICKER: str = "DXY"

# ---------------------------------------------------------------------------
# PAIRS — definisi umum beserta helper base/quote
# ---------------------------------------------------------------------------

# Setiap entry: (base, quote)  →  string XXXYYY otomatis dari helper di bawah.
_PAIR_DEFS: list[tuple[str, str]] = [
    ("EUR", "USD"),
    ("GBP", "USD"),
    ("USD", "JPY"),
    ("AUD", "USD"),
    ("NZD", "USD"),
    ("USD", "CAD"),
    ("USD", "CHF"),
    ("XAU", "USD"),
    ("BTC", "USD"),
    ("ETH", "USD"),
]

PAIRS: list[str] = [f"{b}{q}" for b, q in _PAIR_DEFS]
"""10 pair default yang ditampilkan di Pair Scanner."""

PAIR_META: dict[str, dict[str, str]] = {
    f"{b}{q}": {"base": b, "quote": q} for b, q in _PAIR_DEFS
}
"""
Lookup cepat base/quote dari simbol pair.

Contoh:
    PAIR_META["EURUSD"]  # → {"base": "EUR", "quote": "USD"}
"""


def pair_components(symbol: str) -> tuple[str, str]:
    """Return (base, quote) dari simbol pair yang terdaftar.

    Raises KeyError kalau pair tidak ada di PAIR_META.
    """
    meta = PAIR_META[symbol]
    return meta["base"], meta["quote"]


# ---------------------------------------------------------------------------
# SCORING WEIGHTS  — PLACEHOLDER, BELUM TERVALIDASI
# ---------------------------------------------------------------------------

WEIGHTS: dict[str, float] = {
    "R_hard": 0.45,       # PLACEHOLDER — rate differential + rate surprise
    "C": 0.20,            # PLACEHOLDER — COT (efektif lebih kecil krn gating ekstrem-only)
    "D": 0.15,            # PLACEHOLDER — Retail sentiment contrarian (gating ekstrem)
    "F": 0.20,            # PLACEHOLDER — ForexFactory surprise (actual vs forecast × impact × freshness)
    "R_narrative": 0.00,  # Advisory; tidak masuk skor v1
}
"""
Bobot per faktor scoring. SUM = 1.0.

⚠ SEMUA NILAI = PLACEHOLDER sampai backtest dilakukan.
  Renormalisasi otomatis di engine/scoring.py atas faktor yang score-nya ≠ 0.
  Lihat arsitektur §0 Prinsip 9 & §3 Scoring Spec.
"""

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "WEIGHTS harus sum = 1.0"

# ---------------------------------------------------------------------------
# GATING THRESHOLDS — PLACEHOLDER, BELUM TERVALIDASI
# ---------------------------------------------------------------------------

COT_EXTREME: tuple[int, int] = (20, 80)
"""
(lower, upper) percentile COT Index.
Score C = 0 kalau COT Index berada DI ANTARA lower dan upper.
Hanya berkontribusi bila index < 20 atau > 80.
⚠ PLACEHOLDER — tuning via analisis distribusi COT historis.
"""

RETAIL_EXTREME: int = 70
"""
Threshold long% untuk gating retail sentiment.
Score D aktif hanya bila long_pct_agg > RETAIL_EXTREME  (crowd sangat long → contrarian short)
                    atau long_pct_agg < (100 - RETAIL_EXTREME)  (crowd sangat short → contrarian long).
⚠ PLACEHOLDER — range sebenarnya bisa 70–75% tergantung distribusi sumber.
"""

# ---------------------------------------------------------------------------
# COT FRESHNESS DECAY — PLACEHOLDER, BELUM TERVALIDASI
# ---------------------------------------------------------------------------

FRESHNESS_TAU: float = 6.0
"""
Time-constant (hari) untuk decay eksponensial COT.
freshness = clamp(exp(-(days_since_snapshot - 3) / τ), FRESHNESS_FLOOR, 1.0)
⚠ PLACEHOLDER — dikalibrasi dari pola rilis CFTC vs pergerakan pasar.
"""

FRESHNESS_FLOOR: float = 0.25
"""
Batas bawah freshness multiplier.
COT tidak pernah di-zero-kan sepenuhnya meski sudah sangat basi.
⚠ PLACEHOLDER.
"""

ATR_DIVERGENCE_K: float = 1.5
"""
Multiplier ATR14 untuk penalty divergence harga vs snapshot COT.
Bila |harga_change_sejak_snapshot| > K × ATR14 → freshness ×0.5.
⚠ PLACEHOLDER — dikalibrasi dari kasus di mana COT memberikan sinyal menyesatkan.
"""

# ---------------------------------------------------------------------------
# NEWS OVERLAY — PLACEHOLDER, BELUM TERVALIDASI
# ---------------------------------------------------------------------------

NEWS_DECAY_MIN: float = 120.0
"""
Half-life (menit) time-decay untuk magnitude tiap event berita.
magnitude_adjusted = magnitude × exp(-age_minutes / NEWS_DECAY_MIN)
⚠ PLACEHOLDER — bisa disesuaikan per kategori event (geopolitik vs data makro).
"""

NEWS_CAP: float = 30.0
"""
Cap absolut news_delta per aset: clamp(news_delta, -NEWS_CAP, +NEWS_CAP).
Overlay tidak boleh membalik bias baseline secara dominan.
⚠ PLACEHOLDER — audit dari distribusi magnitude event historis.
"""

# ---------------------------------------------------------------------------
# COT CATEGORIES per aset
# ---------------------------------------------------------------------------

COT_CATEGORY: dict[str, str] = {
    # FX majors — ikut konvensi CFTC Disaggregated / Legacy report
    "USD": "leveraged_funds",
    "EUR": "leveraged_funds",
    "GBP": "leveraged_funds",
    "JPY": "leveraged_funds",
    "AUD": "leveraged_funds",
    "NZD": "leveraged_funds",
    "CAD": "leveraged_funds",
    "CHF": "leveraged_funds",
    # Commodities / metals
    "XAU": "managed_money",
    # Crypto futures (CME)
    "BTC": "leveraged_funds",
    # ETH tidak punya laporan COT CME yang mapan per v1; tambahkan kalau sudah ada.
}
"""
Kategori reporter COT yang dipakai per currency/aset.
Harus cocok dengan field yang diparsing di collectors/cot.py.
"""

# ---------------------------------------------------------------------------
# CACHE TTL (detik)
# ---------------------------------------------------------------------------

TTL: dict[str, int] = {
    "prices":   60,      # Yahoo / ccxt — live tapi request ringan
    "cot":      21600,   # 6 jam — data mingguan, rilis Jumat
    "macro":    21600,   # 6 jam — FRED: yields & surprise harian
    "retail":   1800,    # 30 mnt — Myfxbook delay ~60 mnt anyway
    "news":     300,     # 5 mnt — satu-satunya yang benar-benar fresh
    "calendar": 1800,    # 30 mnt — jadwal event stabil
    "news_overlay": 300, # 5 mnt — proses mahal; ikut TTL news
}
"""
TTL cache per collector/engine (dalam detik).
Lihat arsitektur §2 Data Flow — Mixed-cadence.
"""

# ---------------------------------------------------------------------------
# TIMEZONE
# ---------------------------------------------------------------------------

TIMEZONE_WIB: str = "Asia/Jakarta"
"""Zona waktu WIB (UTC+7). Dipakai di timeutils & semua display timestamp."""

# ---------------------------------------------------------------------------
# SCORING OUTPUT LABELS
# ---------------------------------------------------------------------------

def bias_label(score: float) -> str:
    """Konversi bias_score (−100..+100) ke label human-readable.

    Thresholds bersifat display-only; tidak dipakai dalam logika scoring.
    """
    if score >= 60:
        return "Strong Bullish"
    elif score >= 25:
        return "Bullish"
    elif score >= -24:
        return "Neutral"
    elif score >= -59:
        return "Bearish"
    else:
        return "Strong Bearish"
