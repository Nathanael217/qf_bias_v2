"""
engine/confidence.py — Confidence Engine (QF_BIAS)

Confidence = kesepakatan arah antar faktor scoring aktif.
Bukan angka hardcoded (anti-overclaim §0 Prinsip 7).

Formula:
    conf = BASELINE
         + AGREEMENT_BONUS  × frac_agree     (faktor searah)
         - CONFLICT_PENALTY × frac_conflict   (faktor berlawanan)
         + RETAIL_BONUS × retail_agreement    (jika data aggregat tersedia)
    conf = clamp(conf, 0.0, 1.0)

Definisi "aktif": faktor dengan score ≠ 0 (tidak di-gate).
Definisi "searah": sign(score_i) == sign(score_j) untuk semua pasangan.
Definisi "konflik": ada setidaknya satu pasangan dengan tanda berlawanan.

§3 Arsitektur: confidence ∈ [0,1]. BUKAN hardcoded.
"""

from __future__ import annotations

from typing import TypedDict

# ---------------------------------------------------------------------------
# TUNING PARAMETERS  (PLACEHOLDER — belum dikalibrasi dari backtest)
# ---------------------------------------------------------------------------

_BASELINE: float = 0.40
"""
Kepercayaan dasar ketika hanya 1 faktor aktif
(tidak ada kesepakatan / konflik karena hanya 1 faktor).
PLACEHOLDER.
"""

_AGREEMENT_BONUS: float = 0.40
"""
Bonus maksimum ketika semua faktor aktif searah.
Diterapkan secara proporsional terhadap frac_agree.
PLACEHOLDER.
"""

_CONFLICT_PENALTY: float = 0.20
"""
Penalti maksimum ketika faktor-faktor aktif saling berkonflik.
Diterapkan secara proporsional terhadap frac_conflict.
PLACEHOLDER.
"""

_RETAIL_BONUS_MAX: float = 0.10
"""
Bonus maksimum dari retail_agreement (antar sumber data retail).
Hanya berlaku jika D aktif dan retail_agreement tersedia.
PLACEHOLDER.
"""


# ---------------------------------------------------------------------------
# TYPE ALIASES
# ---------------------------------------------------------------------------

class DriverDict(TypedDict, total=False):
    """
    Subset driver dict dari engine/scoring.py yang dibutuhkan confidence.
    Hanya field yang relevan; field lain diabaikan.
    """
    score: float   # sub-skor faktor ∈ [-1, 1]
    weight: float  # bobot nominal (bukan efektif)


# ---------------------------------------------------------------------------
# HELPER
# ---------------------------------------------------------------------------

def _sign(x: float) -> int:
    """Return +1, -1, atau 0 (untuk nilai mendekati nol)."""
    if x > 1e-9:
        return 1
    elif x < -1e-9:
        return -1
    return 0


def _count_pairs(signs: list[int]) -> tuple[int, int]:
    """
    Hitung jumlah pasangan (agree, conflict) dari list tanda.

    Args:
        signs: List nilai +1 atau -1 (tanda non-zero).

    Returns:
        (n_agree, n_conflict) — total pasangan yang searah vs berlawanan.
    """
    n = len(signs)
    if n < 2:
        return 0, 0

    n_agree = 0
    n_conflict = 0
    for i in range(n):
        for j in range(i + 1, n):
            if signs[i] == signs[j]:
                n_agree += 1
            else:
                n_conflict += 1
    return n_agree, n_conflict


# ---------------------------------------------------------------------------
# MAIN FUNCTION
# ---------------------------------------------------------------------------

def compute_confidence(
    driver_dict: dict[str, DriverDict],
    retail_agreement: float | None = None,
) -> float:
    """
    Hitung confidence score ∈ [0, 1] dari kesepakatan faktor scoring.

    Args:
        driver_dict: Dict faktor → metadata, bentuk:
            {
              "R_hard": {"score": 0.40, "weight": 0.60, ...},
              "C":      {"score": 0.00, "weight": 0.25, ...},
              "D":      {"score": 0.10, "weight": 0.15, ...},
            }
            Faktor dengan score == 0 (di-gate atau tidak aktif) DIABAIKAN.

        retail_agreement: Nilai agreement antar sumber retail ∈ [0, 1].
            Dari collectors/retail.py → field `agreement` per pair/currency.
            Hanya dipakai kalau faktor "D" aktif (score ≠ 0).
            None → tidak ada data retail atau D tidak aktif.

    Returns:
        Confidence ∈ [0.0, 1.0].

    Formula (detail):
        1. Filter faktor aktif (|score| > 1e-9).
        2. Kumpulkan tanda (sign) tiap faktor aktif.
        3. Hitung frac_agree = n_agree / n_total_pairs.
           Hitung frac_conflict = n_conflict / n_total_pairs.
        4. conf = BASELINE + AGREEMENT_BONUS×frac_agree - CONFLICT_PENALTY×frac_conflict
        5. Kalau D aktif dan retail_agreement tersedia:
           conf += RETAIL_BONUS_MAX × retail_agreement
        6. clamp(conf, 0.0, 1.0)

    Edge cases:
        - Tidak ada faktor aktif → return 0.0 (tidak ada dasar kepercayaan).
        - Hanya 1 faktor aktif   → return BASELINE (tidak ada pasangan untuk compare).
        - Semua faktor zero      → return 0.0.

    Dokumentasi rentang output tipis:
        ≈ 0.00–0.25 : sangat rendah — konflik kuat antar faktor
        ≈ 0.25–0.50 : rendah — campuran / kebanyakan gate
        ≈ 0.50–0.70 : moderat — sebagian besar searah
        ≈ 0.70–0.85 : tinggi — semua faktor aktif searah + retail agree
        ≈ 0.85–1.00 : sangat tinggi — jarang, butuh semua faktor sepakat kuat
    """
    if not driver_dict:
        return 0.0

    # --- 1. Filter faktor aktif ---
    active_factors: list[tuple[str, float]] = []
    for name, info in driver_dict.items():
        score = info.get("score", 0.0)
        if _sign(score) != 0:
            active_factors.append((name, score))

    if not active_factors:
        return 0.0

    if len(active_factors) == 1:
        # Hanya 1 faktor — kembalikan baseline (belum bisa bicara agreement)
        conf = _BASELINE
        # Tambahkan retail bonus kalau D adalah satu-satunya faktor aktif
        factor_name = active_factors[0][0]
        if factor_name == "D" and retail_agreement is not None:
            conf += _RETAIL_BONUS_MAX * retail_agreement
        return round(max(0.0, min(1.0, conf)), 4)

    # --- 2. Tanda per faktor aktif ---
    signs = [_sign(score) for _, score in active_factors]

    # --- 3. Hitung pasangan agree / conflict ---
    n_agree, n_conflict = _count_pairs(signs)
    n_total_pairs = n_agree + n_conflict  # = n*(n-1)/2

    if n_total_pairs == 0:
        return round(_BASELINE, 4)

    frac_agree = n_agree / n_total_pairs
    frac_conflict = n_conflict / n_total_pairs

    # --- 4. Formula utama ---
    conf = (
        _BASELINE
        + _AGREEMENT_BONUS * frac_agree
        - _CONFLICT_PENALTY * frac_conflict
    )

    # --- 5. Retail bonus (hanya kalau D aktif) ---
    d_active = any(name == "D" for name, _ in active_factors)
    if d_active and retail_agreement is not None:
        conf += _RETAIL_BONUS_MAX * retail_agreement

    # --- 6. Clamp ---
    return round(max(0.0, min(1.0, conf)), 4)


# ---------------------------------------------------------------------------
# SELF-TEST / __main__ DEMO
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("QF_BIAS  engine/confidence.py  —  DEMO & SELF-TEST")
    print("=" * 60)

    # ── Kasus 1: Semua searah → confidence tinggi ──
    all_agree = {
        "R_hard": {"score": 0.40, "weight": 0.60},
        "C":      {"score": 0.30, "weight": 0.25},
        "D":      {"score": 0.20, "weight": 0.15},
    }
    c1 = compute_confidence(all_agree, retail_agreement=0.90)
    print(f"\n[1] Semua 3 faktor SEARAH + retail agree 0.9 → {c1:.4f}")
    assert c1 > 0.70, f"FAIL: ekspektasi > 0.70, dapat {c1}"
    print("    ✓ PASS (> 0.70)")

    # ── Kasus 2: R_hard vs C berlawanan → confidence lebih rendah ──
    conflict_case = {
        "R_hard": {"score":  0.40, "weight": 0.60},
        "C":      {"score": -0.30, "weight": 0.25},  # berlawanan!
        "D":      {"score":  0.20, "weight": 0.15},
    }
    c2 = compute_confidence(conflict_case)
    print(f"\n[2] R_hard+ vs C- (konflik), D+  → {c2:.4f}")
    assert c2 < c1, f"FAIL: konflik harus confidence lebih rendah dari semua agree"
    print(f"    ✓ PASS (< kasus 1 = {c1:.4f})")

    # ── Kasus 3: C & D gate (score=0), hanya R_hard aktif ──
    only_r_hard = {
        "R_hard": {"score": 0.55, "weight": 0.60},
        "C":      {"score": 0.00, "weight": 0.25},
        "D":      {"score": 0.00, "weight": 0.15},
    }
    c3 = compute_confidence(only_r_hard)
    print(f"\n[3] Hanya R_hard aktif (C&D gate=0) → {c3:.4f}")
    assert abs(c3 - _BASELINE) < 0.01, (
        f"FAIL: 1 faktor aktif harus ≈ BASELINE={_BASELINE}, dapat {c3}"
    )
    print(f"    ✓ PASS (≈ BASELINE={_BASELINE})")

    # ── Kasus 4: Tidak ada faktor aktif → 0.0 ──
    no_active = {
        "R_hard": {"score": 0.00, "weight": 0.60},
        "C":      {"score": 0.00, "weight": 0.25},
        "D":      {"score": 0.00, "weight": 0.15},
    }
    c4 = compute_confidence(no_active)
    print(f"\n[4] Tidak ada faktor aktif → {c4:.4f}")
    assert c4 == 0.0, f"FAIL: ekspektasi 0.0, dapat {c4}"
    print("    ✓ PASS (= 0.0)")

    # ── Kasus 5: D aktif, retail_agreement rendah → lebih rendah dari kasus 1 ──
    d_active_low_retail = {
        "R_hard": {"score": 0.40, "weight": 0.60},
        "C":      {"score": 0.30, "weight": 0.25},
        "D":      {"score": 0.20, "weight": 0.15},
    }
    c5_low  = compute_confidence(d_active_low_retail, retail_agreement=0.10)
    c5_high = compute_confidence(d_active_low_retail, retail_agreement=0.95)
    print(f"\n[5] D aktif, retail_agreement rendah(0.10) vs tinggi(0.95):")
    print(f"    retail=0.10 → {c5_low:.4f}  |  retail=0.95 → {c5_high:.4f}")
    assert c5_high > c5_low, "FAIL: retail agree tinggi harus confidence lebih tinggi"
    print("    ✓ PASS (retail agreement berdampak positif)")

    # ── Kasus 6: Konflik total (R_hard+ vs C- vs D-) ──
    total_conflict = {
        "R_hard": {"score":  0.50, "weight": 0.60},
        "C":      {"score": -0.40, "weight": 0.25},
        "D":      {"score": -0.30, "weight": 0.15},
    }
    c6 = compute_confidence(total_conflict)
    print(f"\n[6] R_hard+ vs C- vs D- (konflik total 2:1) → {c6:.4f}")
    print(f"    (lebih rendah dari kasus 1={c1:.4f}: {'✓' if c6 < c1 else '✗'})")

    # ── Kasus 7: Dict kosong ──
    c7 = compute_confidence({})
    print(f"\n[7] Dict kosong → {c7:.4f}")
    assert c7 == 0.0, f"FAIL: kosong harus 0.0, dapat {c7}"
    print("    ✓ PASS (= 0.0)")

    print("\n── Ringkasan nilai ──")
    for label, val in [
        ("Semua searah + retail 0.9", c1),
        ("R_hard+ vs C- konflik",     c2),
        ("Hanya R_hard (baseline)",   c3),
        ("Tidak ada faktor aktif",    c4),
        ("Retail agree rendah 0.10",  c5_low),
        ("Retail agree tinggi 0.95",  c5_high),
        ("Konflik total 2:1",         c6),
        ("Dict kosong",               c7),
    ]:
        bar = "█" * int(val * 20)
        print(f"  {label:<30s}  {val:.4f}  {bar}")

    print("\n" + "=" * 60)
    print("Semua assertion passed. engine/confidence.py siap.")
    print("=" * 60)
