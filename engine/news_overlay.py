"""
engine/news_overlay.py — News Overlay Engine (QF_BIAS)

Pipeline:
    get_news() headlines  →  cluster_events()  →  classify_direction()
    →  magnitude() × time_decay()  →  compute_news_delta()  →  capped delta per asset.

§3 Arsitektur (kontrak wajib diikuti):
- 1 event = hitung SEKALI (cluster by fuzzy title + time window).
- Arah CONTENT-AWARE: escalation ≠ de-escalation; hawkish ≠ dovish.
- Time-decay: exp(-age_min / NEWS_DECAY_MIN).
- Cap total delta per asset ke ±NEWS_CAP.
- Output news_clusters kompatibel schema bias.json §4.

⚠  v1 menggunakan rule/keyword. TODO-UPGRADE: ganti classify_direction()
   dengan LLM call (mis. via Anthropic API) untuk klasifikasi yang lebih
   akurat — terutama kalimat ambigu atau campuran tanda.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import TypedDict

from config import NEWS_CAP, NEWS_DECAY_MIN
from utils.timeutils import parse_iso_utc

# ---------------------------------------------------------------------------
# TYPE ALIASES
# ---------------------------------------------------------------------------

Headline = dict  # {"ts_utc": str, "title": str, "raw_category": str, ...}

class ClusterResult(TypedDict):
    event_title: str
    n_headlines: int
    latest_ts_utc: str
    age_min: float
    raw_category: str
    link: str


class NewsClusterDisplay(TypedDict):
    """Schema bias.json §4 — news_clusters entry."""
    event: str
    n_headlines: int
    direction: dict[str, str]   # asset → "+", "-", "0"
    magnitude: float
    age_min: float
    link: str
    impact: str                 # "" | "low" | "med" | "high" (dari Groq; display-only)
    source: str                 # "keyword" | "groq" (asal klasifikasi arah)


# ---------------------------------------------------------------------------
# §0 — KEYWORD DICTIONARIES (content-aware, mudah di-extend)
# ---------------------------------------------------------------------------
# Setiap entry: keyword (lowercase) → {asset: raw_score ∈ {+1, -1, 0}}
# Raw score kemudian dikali magnitude × decay saat akumulasi.
#
# Aturan tanda:
#   XAU  : +1 = bullish gold (risk-off / safe haven demand)
#   BTC  : +1 = bullish crypto (risk-on / reflation)
#   USD  : +1 = bullish USD (risk-off flight / hawkish) — DXY = regime context, tidak di-score
#   USD/EUR/GBP/JPY/AUD/NZD/CAD/CHF : +1 = bullish currency tersebut
#
# ⚠ TODO-UPGRADE: Ganti dict ini dengan LLM zero/few-shot classification
#   ketika v1 keyword coverage terbukti tidak cukup (false-positive tinggi
#   pada headline ambigu). Interface classify_direction() tidak perlu berubah.

# --- GEOPOLITICAL / MACRO RISK ---
_RISK_OFF_KEYWORDS: list[str] = [
    "escalation", "escalate", "strike", "strikes", "struck",
    "airstrike", "bombing", "attack", "attacked", "invasion",
    "war", "conflict", "sanction", "sanctions", "blockade",
    "crisis", "emergency", "threat", "threatens", "explosion",
    "missile", "nuclear", "chemical weapon",
]
# Risk-off: XAU+, BTC-, DXY+, NAS-, JPY+ (safe haven), CHF+

_RISK_ON_KEYWORDS: list[str] = [
    "ceasefire", "de-escalation", "deescalation", "truce",
    "peace deal", "peace talks", "agreement", "accord",
    "mediator", "mediation", "resolution", "resolved",
    "deal reached", "breakthrough", "withdrawal", "pullback",
]
# Risk-on: XAU-, BTC+, DXY-, NAS+, JPY-, CHF-

# --- MONETARY POLICY / MACRO DATA ---
_HAWKISH_KEYWORDS: list[str] = [
    "hawkish", "rate hike", "hike", "tightening",
    "hot cpi", "strong cpi", "above forecast", "beats forecast",
    "strong nfp", "strong jobs", "strong payroll",
    "strong gdp", "strong pce", "hot pce",
    "inflation surges", "inflation rises", "inflation higher",
    "beats expectations", "above expectations",
    "less easing", "no cut", "delay cut", "pushes back cut",
]
# Hawkish (relevant to specific currency — applied when currency mentioned)

_DOVISH_KEYWORDS: list[str] = [
    "dovish", "rate cut", "cut rates", "easing",
    "soft cpi", "miss cpi", "below forecast", "misses forecast",
    "weak nfp", "weak jobs", "weak payroll",
    "weak gdp", "soft gdp", "contraction",
    "inflation slows", "inflation falls", "disinflation",
    "misses expectations", "below expectations",
    "more easing", "deeper cuts", "front-loaded cut",
]
# Dovish → currency that's mentioned gets negative

# --- CURRENCY-TICKER DETECTION ---
# Maps keywords (lower) → asset code; used to assign hawkish/dovish to the
# correct currency rather than broadcasting to all.
_CURRENCY_KEYWORDS: dict[str, str] = {
    "fed": "USD", "fomc": "USD", "powell": "USD",
    "ecb": "EUR", "lagarde": "EUR",
    "boe": "GBP", "bank of england": "GBP", "bailey": "GBP",
    "boj": "JPY", "bank of japan": "JPY", "ueda": "JPY",
    "rba": "AUD", "reserve bank of australia": "AUD",
    "rbnz": "NZD", "reserve bank of new zealand": "NZD",
    "boc": "CAD", "bank of canada": "CAD",
    "snb": "CHF", "swiss national bank": "CHF",
    "gold": "XAU", "xauusd": "XAU",
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
}

# Semua aset yang di-score oleh overlay
_ALL_ASSETS: list[str] = [
    "USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF",
    "XAU", "BTC", "ETH",
]


def _neutral_direction() -> dict[str, str]:
    return {a: "0" for a in _ALL_ASSETS}


def _neutral_scores() -> dict[str, float]:
    return {a: 0.0 for a in _ALL_ASSETS}


# ---------------------------------------------------------------------------
# STEP 1 — CLUSTERING
# ---------------------------------------------------------------------------

def _title_similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio antara dua judul (case-insensitive)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _parse_ts(ts_utc: str) -> datetime:
    """Parse ISO-8601 UTC string → datetime UTC-aware (pakai canonical timeutils)."""
    return parse_iso_utc(ts_utc)



def _direction_conflict(title_a: str, title_b: str) -> bool:
    """True kalau dua judul punya arah klasifikasi yang BERLAWANAN.

    Mencegah dedup menelan headline de-escalation ke cluster escalation
    (atau hawkish vs dovish) hanya karena teksnya mirip (banyak kata sama).
    Ini menjaga prinsip §3: escalation vs de-escalation = tanda berlawanan.
    """
    da = classify_direction(title_a)
    db = classify_direction(title_b)
    for asset in da:
        sa, sb = da[asset], db[asset]
        if sa != 0.0 and sb != 0.0 and (sa > 0) != (sb > 0):
            return True  # ada aset yang tandanya berlawanan → bukan event sama
    return False


def cluster_events(
    headlines: list[Headline],
    similarity_threshold: float = 0.80,
    window_minutes: float = 30.0,
    now_utc: datetime | None = None,
) -> list[ClusterResult]:
    """
    Kelompokkan headline yang mirip dalam window waktu menjadi 1 event.

    Algoritma greedy:
    - Iterasi headline diurutkan dari terbaru ke terlama.
    - Setiap headline dicoba di-merge ke cluster existing yang memenuhi:
        (a) similarity ratio judul ≥ similarity_threshold
        (b) selisih waktu antara headline ini dan latest_ts cluster ≤ window_minutes
    - Kalau tidak ada yang cocok → buat cluster baru.

    Args:
        headlines: List headline dari collectors/news.py.
        similarity_threshold: Minimum SequenceMatcher ratio (0–1) untuk dianggap "event sama".
        window_minutes: Jendela waktu (menit); headline di luar window tidak di-merge.
        now_utc: Waktu referensi untuk menghitung age_min. Default = datetime.now(UTC).

    Returns:
        List ClusterResult, diurutkan age_min ascending (terbaru dulu).
    """
    if not headlines:
        return []

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # Urutkan: terbaru dulu
    sorted_hl = sorted(headlines, key=lambda h: h["ts_utc"], reverse=True)

    clusters: list[dict] = []  # internal: {titles, latest_ts, n, raw_category}

    for hl in sorted_hl:
        ts = _parse_ts(hl["ts_utc"])
        title = hl.get("title") or ""
        raw_cat = hl.get("raw_category") or ""

        link = hl.get("link") or ""
        merged = False
        for c in clusters:
            # (a) time window check
            delta_min = abs((c["latest_ts"] - ts).total_seconds()) / 60.0
            if delta_min > window_minutes:
                continue
            # (b) title similarity check — bandingkan dengan representative title cluster
            sim = _title_similarity(title, c["repr_title"])
            if sim >= similarity_threshold and not _direction_conflict(title, c["repr_title"]):
                c["n"] += 1
                # update latest_ts kalau lebih baru
                if ts > c["latest_ts"]:
                    c["latest_ts"] = ts
                    c["repr_title"] = title  # perbaharui representatif
                    c["link"] = link
                merged = True
                break

        if not merged:
            clusters.append({
                "repr_title": title,
                "latest_ts": ts,
                "n": 1,
                "raw_category": raw_cat,
                "link": link,
            })

    results: list[ClusterResult] = []
    for c in clusters:
        age_min = (now_utc - c["latest_ts"]).total_seconds() / 60.0
        age_min = max(age_min, 0.0)  # jangan negatif (clock skew kecil)
        results.append(ClusterResult(
            event_title=c["repr_title"],
            n_headlines=c["n"],
            latest_ts_utc=c["latest_ts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            age_min=round(age_min, 1),
            raw_category=c["raw_category"],
            link=c.get("link", ""),
        ))

    # Urutkan terbaru dulu
    results.sort(key=lambda r: r["age_min"])
    return results


# ---------------------------------------------------------------------------
# STEP 2 — CONTENT-AWARE DIRECTION
# ---------------------------------------------------------------------------


def _kw_hit(text_lower: str, keywords: list[str]) -> bool:
    """True kalau ada keyword yang match sebagai WORD (bukan substring).

    Mencegah 'deescalation' ter-match oleh 'escalation', atau 'no cut'
    ter-match parsial. Multi-word keyword dicocokkan apa adanya (sudah aman).
    """
    for kw in keywords:
        if " " in kw or "-" in kw:
            if kw in text_lower:
                return True
        else:
            if re.search(r"\b" + re.escape(kw) + r"\b", text_lower):
                return True
    return False


def _detect_currencies(text_lower: str) -> list[str]:
    """
    Deteksi currency/asset mana yang relevan dari teks headline.
    Return list asset codes; kosong = tidak ada yang spesifik.
    """
    found: list[str] = []
    for kw, asset in _CURRENCY_KEYWORDS.items():
        if " " in kw:
            hit = kw in text_lower
        else:
            hit = re.search(r"\b" + re.escape(kw) + r"\b", text_lower) is not None
        if hit and asset not in found:
            found.append(asset)
    return found


def classify_direction(event_text: str) -> dict[str, float]:
    """
    Klasifikasikan arah event terhadap setiap aset.

    Returns:
        dict {asset: raw_score ∈ {-1.0, 0.0, +1.0}}.
        +1 = bullish aset itu, -1 = bearish, 0 = tidak relevan.

    ⚠ v1: rule/keyword-based.
    TODO-UPGRADE: ganti body fungsi ini dengan LLM inference
    (mis. Anthropic claude-haiku via API) untuk akurasi lebih tinggi
    pada headline ambigu. Signature dan return type TIDAK perlu berubah.

    TANDA (content-aware, BUKAN kategori tetap):
    - Risk-off keywords  → XAU+, USD+, JPY+, CHF+  |  BTC-, ETH-, AUD-, NZD-, CAD-
    - Risk-on keywords   → XAU-, USD-, JPY-, CHF-  |  BTC+, ETH+, AUD+, NZD+, CAD+
    Note: DXY = regime context (tidak di-score); efek pada DXY direfleksikan ke USD.
    - Hawkish keyword    → currency yang disebutkan +; kalau tidak spesifik → USD+
    - Dovish keyword     → currency yang disebutkan -; kalau tidak spesifik → USD-
    """
    text_lower = event_text.lower()
    scores = _neutral_scores()

    # --- Check risk-off ---
    risk_off_hit = _kw_hit(text_lower, _RISK_OFF_KEYWORDS)
    # --- Check risk-on ---
    risk_on_hit = _kw_hit(text_lower, _RISK_ON_KEYWORDS)
    # --- Check hawkish/dovish ---
    hawkish_hit = _kw_hit(text_lower, _HAWKISH_KEYWORDS)
    dovish_hit = _kw_hit(text_lower, _DOVISH_KEYWORDS)

    # Risk-off dan risk-on bisa saling menetralkan (headline ambigu)
    # Kalau keduanya hit → net effect kecil (saling offset)
    if risk_off_hit and not risk_on_hit:
        # Klasik risk-off: safe haven demand
        scores["XAU"] += 1.0
        scores["USD"] += 1.0   # flight to USD (DXY = regime context, tidak di-score sendiri)
        scores["JPY"] += 1.0   # safe haven yen
        scores["CHF"] += 1.0   # safe haven franc
        scores["BTC"] -= 1.0   # crypto sell-off
        scores["ETH"] -= 1.0
        # Risk currencies (pro-cyclical) turun
        for ccy in ["AUD", "NZD", "CAD"]:
            scores[ccy] -= 1.0

    elif risk_on_hit and not risk_off_hit:
        # Risk appetite meningkat
        scores["XAU"] -= 1.0
        scores["USD"] -= 1.0   # DXY = regime context; efeknya ke USD scored
        scores["JPY"] -= 1.0
        scores["CHF"] -= 1.0
        scores["BTC"] += 1.0
        scores["ETH"] += 1.0
        for ccy in ["AUD", "NZD", "CAD"]:
            scores[ccy] += 1.0

    elif risk_off_hit and risk_on_hit:
        # Ambigu / mixed signal → semua skor 0 (tidak ada edge)
        pass

    # Hawkish / dovish (currency-specific)
    if hawkish_hit and not dovish_hit:
        relevant = _detect_currencies(text_lower)
        targets = relevant if relevant else ["USD"]  # default ke USD kalau tidak jelas
        for asset in targets:
            if asset in scores:
                scores[asset] += 1.0

    elif dovish_hit and not hawkish_hit:
        relevant = _detect_currencies(text_lower)
        targets = relevant if relevant else ["USD"]
        for asset in targets:
            if asset in scores:
                scores[asset] -= 1.0

    # Clamp ke {-1, 0, +1} setelah akumulasi (kombinasi risk+policy bisa >1)
    for asset in scores:
        scores[asset] = max(-1.0, min(1.0, scores[asset]))

    return scores


# ---------------------------------------------------------------------------
# STEP 3 — MAGNITUDE
# ---------------------------------------------------------------------------

def magnitude(cluster: ClusterResult) -> float:
    """
    Skala kasar magnitude event ∈ (0, 1].

    Logika:
    - Base = 0.5 (setiap event minimal punya bobot sedang).
    - Bonus dari jumlah headline (mencerminkan seberapa banyak media meliput):
        n=1 → +0.0
        n=2 → +0.15
        n=3 → +0.20
        n≥5 → +0.30 (cap)
    - Bonus dari raw_category:
        HIGH_IMPACT / CENTRAL_BANK / GEOPOLITICAL → +0.20
        MEDIUM / ECONOMIC → +0.10
        lainnya → +0.0
    - Total di-clamp ke [0.1, 1.0].

    Dokumentasi skala:
        0.1–0.3 : noise / mention singkat
        0.3–0.6 : event relevan, cukup diliput
        0.6–0.8 : event signifikan, banyak headline
        0.8–1.0 : breaking major event

    ⚠ PLACEHOLDER — nilai ini belum dikalibrasi dari distribusi historis.
    """
    base = 0.50

    # Bonus headline count
    n = cluster["n_headlines"]
    if n >= 5:
        headline_bonus = 0.30
    elif n >= 3:
        headline_bonus = 0.20
    elif n >= 2:
        headline_bonus = 0.15
    else:
        headline_bonus = 0.00

    # Bonus kategori
    cat = (cluster.get("raw_category") or "").upper()
    if cat in {"HIGH_IMPACT", "CENTRAL_BANK", "GEOPOLITICAL", "MONETARY_POLICY"}:
        cat_bonus = 0.20
    elif cat in {"MEDIUM", "ECONOMIC", "MACRO"}:
        cat_bonus = 0.10
    else:
        cat_bonus = 0.00

    total = base + headline_bonus + cat_bonus
    return round(max(0.1, min(1.0, total)), 4)


# ---------------------------------------------------------------------------
# STEP 4 — TIME DECAY
# ---------------------------------------------------------------------------

def time_decay(age_min: float) -> float:
    """
    Decay eksponensial berdasarkan umur event.

    Formula: exp(-age_min / NEWS_DECAY_MIN)
    NEWS_DECAY_MIN = 120.0 menit (PLACEHOLDER dari config).

    Contoh nilai:
        age=0   → 1.000
        age=60  → 0.607
        age=120 → 0.368  (half-life ≈ 83 mnt)
        age=240 → 0.135
        age=480 → 0.018  (~8 jam sudah hampir nol)
    """
    return math.exp(-age_min / NEWS_DECAY_MIN)


# ---------------------------------------------------------------------------
# STEP 5 — COMPUTE NEWS DELTA (entry point utama)
# ---------------------------------------------------------------------------

def compute_news_delta(
    headlines: list[Headline],
    similarity_threshold: float = 0.80,
    window_minutes: float = 30.0,
    now_utc: datetime | None = None,
    direction_override: dict[str, dict] | None = None,
) -> tuple[dict[str, float], list[NewsClusterDisplay]]:
    """
    Pipeline lengkap: headlines → delta per asset + display clusters.

    Args:
        headlines: Raw output dari collectors/news.py (list headline).
        similarity_threshold: Threshold clustering (default 0.80).
        window_minutes: Window waktu clustering dalam menit (default 30.0).
        now_utc: Waktu referensi. Default = datetime.now(UTC).

    Returns:
        (news_delta, news_clusters) di mana:
        - news_delta: {asset: float} ∈ [-NEWS_CAP, +NEWS_CAP] untuk tiap aset.
          Siap dijumlahkan ke bias_baseline di app.py.
        - news_clusters: List display cluster sesuai schema bias.json §4.

    Prinsip anti-double-count:
        Setiap cluster (= 1 event nyata) hanya berkontribusi SEKALI ke delta,
        tidak peduli berapa banyak headline yang membentuknya.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # --- 1. Cluster ---
    clusters = cluster_events(
        headlines,
        similarity_threshold=similarity_threshold,
        window_minutes=window_minutes,
        now_utc=now_utc,
    )

    # --- 2. Akumulasi delta (per cluster = sekali hitung) ---
    raw_delta: dict[str, float] = _neutral_scores()
    display_clusters: list[NewsClusterDisplay] = []

    for cluster in clusters:
        # Klasifikasi arah: kalau ada override Groq utk judul ini → pakai skor Groq
        # (Groq MENGUKUR arah). Kalau tidak → fallback keyword classify_direction.
        # Magnitude/decay/scale/cap di bawah TETAP dihitung engine (tak berubah).
        ov = direction_override.get(cluster["event_title"]) if direction_override else None
        if ov and isinstance(ov.get("scores"), dict):
            scores = {a: float(ov["scores"].get(a, 0.0)) for a in _ALL_ASSETS}
            cl_impact = ov.get("impact", "") or ""
            cl_source = "groq"
        else:
            scores = classify_direction(cluster["event_title"])
            cl_impact = ""
            cl_source = "keyword"
        mag = magnitude(cluster)
        decay = time_decay(cluster["age_min"])
        weight = mag * decay  # kontribusi event ini

        direction_display: dict[str, str] = {}
        for asset, s in scores.items():
            contribution = s * weight
            raw_delta[asset] += contribution
            # Tanda untuk display: +, -, 0
            if s > 0:
                direction_display[asset] = "+"
            elif s < 0:
                direction_display[asset] = "-"
            else:
                direction_display[asset] = "0"

        display_clusters.append(NewsClusterDisplay(
            event=cluster["event_title"],
            n_headlines=cluster["n_headlines"],
            direction=direction_display,
            magnitude=round(mag, 3),
            age_min=cluster["age_min"],
            link=cluster.get("link", ""),
            impact=cl_impact,
            source=cl_source,
        ))

    # --- 3. Scale ke ±100 range (delta dalam satuan raw score × weight,
    #        bukan langsung poin bias). Konversi ke skala yang bermakna:
    #        news_delta ∈ [-NEWS_CAP, +NEWS_CAP] di mana NEWS_CAP = 30.0.
    #        Faktor scale: asumsikan maks kontribusi teoritis = 3.0 unit raw
    #        (3 event besar sekaligus), skalakan ke NEWS_CAP.
    #
    #        Dengan NEWS_CAP=30 dan maks_raw=3.0 → scale_factor = 10.0.
    #        ⚠ PLACEHOLDER — kalibrasi dari distribusi event historis. ---
    # ⚠ PLACEHOLDER KERAS: SCALE_FACTOR=10 → satu event risk-off besar (raw~0.8)
    # langsung ~8 poin; 2-3 event searah langsung mentok cap ±30 (overlay tumpul,
    # kurang gradasi). WAJIB dikalibrasi dari distribusi event historis sebelum
    # dipercaya. Pertimbangkan scale non-linear / per-kategori saat backtest.
    SCALE_FACTOR: float = 10.0
    news_delta: dict[str, float] = {}
    for asset, raw in raw_delta.items():
        scaled = raw * SCALE_FACTOR
        news_delta[asset] = round(max(-NEWS_CAP, min(NEWS_CAP, scaled)), 4)

    return news_delta, display_clusters


# ---------------------------------------------------------------------------
# SELF-TEST / __main__ DEMO
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta

    print("=" * 65)
    print("QF_BIAS  engine/news_overlay.py  —  DEMO & SELF-TEST")
    print("=" * 65)

    now = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)

    def _ts(minutes_ago: float) -> str:
        t = now - timedelta(minutes=minutes_ago)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Dummy headlines ---
    headlines = [
        # KASUS 1: 6 headline near-identik → WAJIB jadi 1 cluster (anti-double-count)
        {"ts_utc": _ts(5),  "title": "Iran launches missile strike on Israel", "raw_category": "GEOPOLITICAL"},
        {"ts_utc": _ts(6),  "title": "Iran launches missiles strike on Israel", "raw_category": "GEOPOLITICAL"},
        {"ts_utc": _ts(7),  "title": "Iran launches missile strikes on Israel", "raw_category": "GEOPOLITICAL"},
        {"ts_utc": _ts(8),  "title": "Iran launches a missile strike on Israel", "raw_category": "GEOPOLITICAL"},
        {"ts_utc": _ts(9),  "title": "Iran launches missile strike on Israeli targets", "raw_category": "GEOPOLITICAL"},
        {"ts_utc": _ts(10), "title": "Iran launches missile strikes on Israeli targets", "raw_category": "GEOPOLITICAL"},
        # KASUS 2: De-escalation → tanda BERLAWANAN vs cluster 1
        {"ts_utc": _ts(15), "title": "Iran-Israel ceasefire agreement reached after talks", "raw_category": "GEOPOLITICAL"},
        # KASUS 3: Hawkish Fed (currency-specific)
        {"ts_utc": _ts(45), "title": "Fed Powell signals rate hike ahead, inflation too hot", "raw_category": "CENTRAL_BANK"},
        # KASUS 4: Dovish ECB
        {"ts_utc": _ts(90), "title": "ECB Lagarde: dovish pivot likely, eurozone growth slows", "raw_category": "CENTRAL_BANK"},
        # KASUS 5: Unrelated stale headline (old)
        {"ts_utc": _ts(300), "title": "G20 summit scheduled for next month", "raw_category": "MACRO"},
    ]

    print(f"\nInput: {len(headlines)} headlines  (now_utc = {now.isoformat()})")

    # --- Step 1: Cluster ---
    clusters = cluster_events(headlines, now_utc=now)
    print(f"\n── CLUSTERS (dikelompokkan, {len(clusters)} dari {len(headlines)} headline) ──")
    for i, c in enumerate(clusters, 1):
        print(f"  [{i}] n={c['n_headlines']:2d}  age={c['age_min']:5.1f}m  {c['event_title'][:60]}")

    assert len(clusters) == 5, (
        f"FAIL: ekspektasi 5 cluster (6 headline near-identik → 1 cluster), "
        f"dapat {len(clusters)}"
    )
    iran_cluster = next(c for c in clusters if "iran" in c["event_title"].lower() and "ceasefire" not in c["event_title"].lower())
    assert iran_cluster["n_headlines"] == 6, (
        f"FAIL: 6 headline identik harus jadi 1 cluster n=6, dapat {iran_cluster['n_headlines']}"
    )
    print("\n  ✓ PASS: 6 headline near-identik → 1 cluster (anti-double-count verified)")

    # --- Step 2: classify_direction demo ---
    print("\n── DIRECTION CLASSIFICATION ──")
    cases = [
        "Iran launches missile strike on Israel",            # risk-off
        "Iran-Israel ceasefire agreement reached after talks", # risk-on (berlawanan)
        "Fed Powell signals rate hike ahead, inflation too hot", # hawkish USD
        "ECB Lagarde: dovish pivot likely, eurozone growth slows", # dovish EUR
    ]
    for text in cases:
        d = classify_direction(text)
        relevant = {k: v for k, v in d.items() if v != 0.0}
        print(f"\n  «{text[:55]}»")
        print(f"   → {relevant}")

    # Verify: de-escalation = berlawanan vs escalation
    esc_scores = classify_direction("Iran launches missile strike on Israel")
    deesc_scores = classify_direction("Iran-Israel ceasefire agreement reached after talks")
    assert esc_scores["XAU"] > 0 and deesc_scores["XAU"] < 0, (
        "FAIL: escalation harus XAU+, de-escalation harus XAU-"
    )
    assert esc_scores["BTC"] < 0 and deesc_scores["BTC"] > 0, (
        "FAIL: escalation harus BTC-, de-escalation harus BTC+"
    )
    print("\n  ✓ PASS: escalation vs de-escalation = tanda BERLAWANAN verified (XAU, BTC)")

    # Verify: hawkish USD+ dan dovish EUR-
    hawk = classify_direction("Fed Powell signals rate hike ahead, inflation too hot")
    dove = classify_direction("ECB Lagarde: dovish pivot likely, eurozone growth slows")
    assert hawk["USD"] > 0, "FAIL: hawkish Fed harus USD+"
    assert dove["EUR"] < 0, "FAIL: dovish ECB harus EUR-"
    print("  ✓ PASS: hawkish Fed → USD+  |  dovish ECB → EUR-")

    # --- Step 3: time_decay demo ---
    print("\n── TIME DECAY ──")
    for age in [0, 30, 60, 120, 240, 480]:
        print(f"  age={age:3d}m → decay={time_decay(age):.4f}")

    # --- Step 4: Full pipeline ---
    print("\n── COMPUTE_NEWS_DELTA (full pipeline) ──")
    delta, display = compute_news_delta(headlines, now_utc=now)
    print("\n  news_delta per asset:")
    for asset, d in sorted(delta.items()):
        bar = "▓" * int(abs(d) / 2)
        sign = "+" if d > 0 else ""
        print(f"  {asset:4s}  {sign}{d:+7.2f}  {bar}")

    print(f"\n  news_clusters ({len(display)} entries):")
    for dc in display:
        dir_str = {k: v for k, v in dc["direction"].items() if v != "0"}
        print(f"  • [{dc['n_headlines']}hl age={dc['age_min']}m mag={dc['magnitude']}]  "
              f"{dc['event'][:50]}  → {dir_str}")

    # --- Edge case: kosong ---
    delta_empty, clusters_empty = compute_news_delta([])
    assert all(v == 0.0 for v in delta_empty.values()), "FAIL: empty input harus semua delta=0"
    assert clusters_empty == [], "FAIL: empty input harus return clusters kosong"
    print("\n  ✓ PASS: empty input → semua delta=0, clusters=[]")

    print("\n" + "=" * 65)
    print("Semua assertion passed. engine/news_overlay.py siap.")
    print("=" * 65)
