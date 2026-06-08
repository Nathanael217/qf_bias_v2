"""
engine/news_filter.py — Filter ketat berbasis DAMPAK-PASAR (bukan kebenaran).

Prinsip (disetujui user):
  - Kriteria lolos = "apakah ini menggoyang harga/market?" — BUKAN apakah beritanya
    benar. Headline fake yang sempat menggoyang market (mis. fake "Trump 90-day
    tariff pause" April 2025) TETAP relevan: pasar tetap bergerak. Tidak ada
    mekanisme verifikasi/retraksi di sini.
  - Default DROP. Sebuah headline hanya lolos kalau mengandung kata-kunci
    market-moving (HIGH/MED). News tanpa dampak apa pun → dibuang.
  - Item Trump WAJIB mengandung kata-kunci aksi/policy (tarif, sanksi, Fed, pajak,
    China, oil, dll). Serangan personal / repost / promo rally → DROP.
  - Source-agnostic: berlaku sama untuk RSS, Telegram, dsb. Sumber bising
    (mis. ForexLive) bisa di-ketat-kan via SOURCE_MIN_IMPACT.

Semua daftar kata-kunci = TUNABLE (placeholder). Sesuaikan dari pengalaman.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Tier kata-kunci dampak-pasar. Pakai regex word-boundary supaya "war" tidak
# match "forward"/"warren", dst.
# ---------------------------------------------------------------------------

# HIGH — penggerak pasar besar (kebijakan moneter, data inti, perang/tarif, fiskal).
_HIGH_KEYWORDS = [
    # Monetary policy / central banks
    r"fed", r"fomc", r"powell", r"ecb", r"lagarde", r"boe", r"bailey", r"boj", r"ueda",
    r"rate cut", r"rate hike", r"rate decision", r"interest rate", r"interest rates",
    r"hawkish", r"dovish", r"basis points", r"\d+\s?bps", r"quantitative",
    r"rate statement", r"monetary policy",
    # Inflation / jobs (data inti)
    r"cpi", r"core cpi", r"inflation", r"pce", r"nonfarm", r"non-farm", r"payrolls",
    r"nfp", r"unemployment", r"jobless", r"jobs report",
    # Trade / tariffs / sanctions
    r"tariff", r"tariffs", r"trade war", r"trade deal", r"sanction", r"sanctions",
    r"embargo", r"export ban", r"import ban", r"levy", r"duties",
    # Geopolitics / war (market-moving)
    r"war", r"strike", r"airstrike", r"missile", r"attack", r"invasion", r"invade",
    r"ceasefire", r"nuclear", r"escalation", r"retaliation", r"military", r"troops",
    # Fiscal / debt / systemic
    r"default", r"debt ceiling", r"downgrade", r"shutdown", r"stimulus", r"bailout",
    # Energy / commodity supply
    r"opec", r"crude", r"oil production", r"supply cut", r"output cut",
    # FX intervention / currency regime
    r"intervention", r"devaluation", r"currency peg",
    # Crypto-specific market movers
    r"bitcoin etf", r"spot etf", r"strategic reserve", r"sec.*(approv|reject)",
]

# MED — penggerak sekunder (survei, aktivitas, pembicara CB, rilis tier-2).
_MED_KEYWORDS = [
    r"pmi", r"ism", r"retail sales", r"gdp", r"consumer confidence", r"durable goods",
    r"trade balance", r"housing", r"factory orders", r"industrial production",
    r"speaks", r"speech", r"minutes", r"beige book", r"forecast", r"guidance",
    r"jobless claims", r"sentiment", r"consumer", r"manufacturing",
]

# Kata-kunci AKSI Trump (policy/market). "Trump in action" = Trump + salah satu ini.
_TRUMP_ACTION_KEYWORDS = [
    r"tariff", r"tariffs", r"sanction", r"sanctions", r"trade deal", r"trade war",
    r"executive order", r"fed", r"powell", r"rate", r"rates", r"interest rate",
    r"tax", r"taxes", r"china", r"oil", r"opec", r"dollar", r"deploy", r"troops",
    r"military", r"strike", r"nominate", r"impose", r"levy", r"duties", r"ban",
    r"deal", r"fire", r"fired", r"crypto", r"bitcoin", r"reserve", r"stimulus",
]

_TRUMP_RE = re.compile(r"\btrump\b", re.IGNORECASE)


def _compile(words: list[str]) -> re.Pattern:
    # Bungkus tiap kata dengan word-boundary; gabung jadi satu alternation.
    return re.compile(r"(?<!\w)(?:" + "|".join(words) + r")(?!\w)", re.IGNORECASE)


_HIGH_RE = _compile(_HIGH_KEYWORDS)
_MED_RE = _compile(_MED_KEYWORDS)
_TRUMP_ACTION_RE = _compile(_TRUMP_ACTION_KEYWORDS)

_IMPACT_RANK = {"drop": 0, "med": 1, "high": 2}

# Sumber tertentu di-ketat-kan (hanya HIGH yang lolos). ForexLive = banyak noise teknikal.
SOURCE_MIN_IMPACT: dict[str, str] = {
    "ForexLive": "high",   # filter ketat: hanya headline high-impact dari ForexLive
}


def classify_news_impact(
    title: str,
    source: str = "",
    raw_category: str = "",
) -> dict[str, Any]:
    """Klasifikasi dampak-pasar satu headline.

    Returns dict:
      impact       : "high" | "med" | "drop"
      trump_action : bool — True kalau Trump + kata-kunci aksi/policy
      matched      : str  — contoh kata-kunci yang match (untuk debug/display)
      reason       : str
    """
    text = f"{title or ''} {raw_category or ''}".strip()
    is_trump = bool(_TRUMP_RE.search(text)) or (source or "").strip().lower() == "trump"

    high_m = _HIGH_RE.search(text)
    med_m = _MED_RE.search(text)
    trump_action = bool(is_trump and _TRUMP_ACTION_RE.search(text))

    # Item Trump: WAJIB ada kata-kunci market. Tanpa itu = noise personal/politik → DROP.
    if is_trump and not (high_m or med_m or trump_action):
        return {"impact": "drop", "trump_action": False, "matched": "",
                "reason": "trump tanpa aksi/policy market → noise"}

    if high_m or (trump_action and _HIGH_RE.search(text)):
        return {"impact": "high", "trump_action": trump_action,
                "matched": (high_m.group(0) if high_m else "trump-action"),
                "reason": "kata-kunci high-impact"}
    if trump_action:
        # Trump + aksi tapi belum kena HIGH eksplisit → tetap diperlakukan tinggi (Trump policy)
        return {"impact": "high", "trump_action": True, "matched": "trump-action",
                "reason": "trump in action (policy)"}
    if med_m:
        return {"impact": "med", "trump_action": False, "matched": med_m.group(0),
                "reason": "kata-kunci med-impact"}

    return {"impact": "drop", "trump_action": False, "matched": "",
            "reason": "tak ada kata-kunci market-moving → no impact"}


def filter_headlines(
    headlines: list[dict],
    *,
    min_impact: str = "med",
    source_min_impact: dict[str, str] | None = None,
) -> list[dict]:
    """Saring list headline; hanya yang lolos gerbang dampak yang dikembalikan.

    Tiap headline output diberi field tambahan: "impact", "trump_action", "matched".
    min_impact         : ambang global ("med" default; "high" untuk horizon panjang).
    source_min_impact  : override per-source (default SOURCE_MIN_IMPACT).
    """
    src_min = SOURCE_MIN_IMPACT if source_min_impact is None else source_min_impact
    global_rank = _IMPACT_RANK.get(min_impact, 1)
    out: list[dict] = []
    for h in headlines or []:
        if not isinstance(h, dict):
            continue
        title = h.get("title", "")
        source = h.get("source", "")
        raw_cat = h.get("raw_category", "") or ""
        cls = classify_news_impact(title, source, raw_cat)
        if cls["impact"] == "drop":
            continue
        # Ambang efektif = max(global, per-source)
        req = max(global_rank, _IMPACT_RANK.get(src_min.get(source, "med"), 1))
        if _IMPACT_RANK[cls["impact"]] < req:
            continue
        enriched = dict(h)
        enriched["impact"] = cls["impact"]
        enriched["trump_action"] = cls["trump_action"]
        enriched["matched"] = cls["matched"]
        out.append(enriched)
    return out
