# QF_BIAS_BUILD: COT arah dibaca dari long%25 (metrik sebanding), fix false-BEDA (AUD/CHF dst) + label-alignment konsisten + expander penjelasan cara-baca/rilis/freshness/dampak (2026-06-04x)
"""
app.py — QF_BIAS Dashboard (Streamlit)
========================================
Entry point tunggal: `streamlit run app.py`

Alur (§2 Data Flow):
  1. Header — timestamp UTC+WIB, status sumber, tombol Refresh.
  2. Collectors (cached TTL) — prices, macro, cot, retail, news, calendar.
  3. Engine — compute_all_assets → confidence → compute_pairs → compute_news_delta.
  4. Display — Bias Board | Pair Scanner | News Feed | Key Risk Events | Footer.

Konvensi import (arsitektur §1.1 — root = import root, tidak ada prefix qf_bias.):
  from config import ...
  from utils.timeutils import ...
  from collectors.prices import get_prices
  dll.

Secrets: .streamlit/secrets.toml  (FRED_API_KEY, MYFXBOOK_EMAIL/PASSWORD/SESSION)
"""

from __future__ import annotations

import sys
import os

# Pastikan root repo ada di sys.path (safety net — Streamlit Cloud biasanya sudah handle ini)
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import logging
from datetime import datetime
from typing import Any

import streamlit as st

# ---------------------------------------------------------------------------
# Setup logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s: %(message)s",
)
logger = logging.getLogger("app")

# ---------------------------------------------------------------------------
# Import modul internal (setelah path setup)
# ---------------------------------------------------------------------------
try:
    from config import (
        ASSETS_ALL, ASSETS_CRYPTO, ASSETS_FX, ASSET_GOLD,
        PAIRS, PAIR_META, TTL, bias_label,
    )
    from utils.timeutils import (
        now_utc, now_wib,
        fmt_iso_utc, fmt_wib_display,
        countdown_str, minutes_until, age_minutes,
    )
    from utils.cache import clear_all_caches, ttl_cache
    from collectors.prices import get_prices as _get_prices_raw
    from collectors.macro import get_macro as _get_macro_raw, build_surprises
    from collectors.cot import get_cot as _get_cot_raw
    from collectors.retail import get_retail as _get_retail_raw
    from collectors.news import get_news as _get_news_raw
    from collectors.calendar_evt import get_calendar as _get_calendar_raw
    from engine.scoring import compute_all_assets
    from engine.news_overlay import compute_news_delta, cluster_events
    from engine.groq_client import classify_headline as _groq_classify_raw
    from engine.groq_client import extract_calendar_image as _groq_vision_raw
    from engine.manual_actuals import make_event_id, apply_manual_actuals, match_vision_rows
    from engine.sigma_table import enrich_surprise_fields
    from collectors.actuals_eurostat import get_eu_actuals as _get_eu_actuals_raw, apply_eu_actuals
    from engine.confidence import compute_confidence
    from engine.pairs import compute_pairs, rank_pairs
    _IMPORTS_OK = True
except Exception as _import_err:
    _IMPORTS_OK = False
    _IMPORT_ERR_MSG = str(_import_err)

# ---------------------------------------------------------------------------
# Page config (harus sebelum st.* pertama lain)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="QF_BIAS — Market Bias Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# CSS minimal
# ---------------------------------------------------------------------------
st.markdown("""
<style>
/* Bias bar container */
.bias-bar-wrap { width: 100%; background: #e5e7eb; border-radius: 4px; height: 18px; margin: 4px 0; }
.bias-bar-fill { height: 18px; border-radius: 4px; transition: width 0.3s; }

/* Badge */
.badge-ok    { background:#16a34a; color:white; padding:2px 8px; border-radius:10px; font-size:0.75rem; font-weight:600; }
.badge-fail  { background:#dc2626; color:white; padding:2px 8px; border-radius:10px; font-size:0.75rem; font-weight:600; }
.badge-warn  { background:#d97706; color:white; padding:2px 8px; border-radius:10px; font-size:0.75rem; font-weight:600; }

/* Impact badges */
.impact-high { background:#ef4444; color:white; padding:1px 7px; border-radius:8px; font-size:0.72rem; font-weight:700; }
.impact-med  { background:#f59e0b; color:white; padding:1px 7px; border-radius:8px; font-size:0.72rem; font-weight:600; }
.impact-low  { background:#6b7280; color:white; padding:1px 7px; border-radius:8px; font-size:0.72rem; font-weight:500; }

/* Bias label colors */
.label-sbull { color:#15803d; font-weight:700; }
.label-bull  { color:#16a34a; font-weight:600; }
.label-neut  { color:#6b7280; font-weight:500; }
.label-bear  { color:#dc2626; font-weight:600; }
.label-sbear { color:#991b1b; font-weight:700; }

/* Asset card */
.asset-card { border:1px solid #e5e7eb; border-radius:8px; padding:12px; margin-bottom:6px; background:#fafafa; }

/* Direction sign */
.dir-plus { color:#16a34a; font-weight:700; }
.dir-minus { color:#dc2626; font-weight:700; }
.dir-zero  { color:#9ca3af; }

/* Footer */
.footer-note { color:#6b7280; font-size:0.78rem; padding:12px; border-top:1px solid #e5e7eb; margin-top:24px; }

/* ===== Metavulus-style dark cards ===== */
.qf-card { background:#0f141b; border:1px solid #1e2530; border-radius:14px; padding:16px 16px 14px;
           margin-bottom:14px; }
.qf-card.sel { border-color:#3b4658; box-shadow:0 0 0 1px #3b4658; }
.qf-head { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }
.qf-micro { color:#64748b; font-size:0.60rem; letter-spacing:0.09em; text-transform:uppercase; font-weight:700; }
.qf-asset { color:#e2e8f0; font-size:1.18rem; font-weight:800; line-height:1.15; }
.qf-chip { padding:3px 11px; border-radius:999px; font-weight:800; font-size:0.92rem; white-space:nowrap; }
.qf-chip-pos { background:#0e2a1a; color:#4ade80; }
.qf-chip-neg { background:#2c1417; color:#f87171; }
.qf-chip-neu { background:#1c232e; color:#9aa6b6; }
.qf-label { font-weight:700; font-size:0.92rem; margin-top:7px; }
.qf-bar { position:relative; height:8px; border-radius:6px; margin:9px 0 4px;
          background:linear-gradient(90deg,#dc2626 0%,#f59e0b 50%,#16a34a 100%); }
.qf-bar-mk { position:absolute; top:-3px; width:3px; height:14px; background:#fff; border-radius:2px;
             transform:translateX(-50%); box-shadow:0 0 4px rgba(0,0,0,.6); }
.qf-meta { color:#64748b; font-size:0.72rem; margin-top:3px; display:flex; justify-content:space-between; }
.qf-drv { border-top:1px solid #1b212b; padding-top:8px; margin-top:10px; }
.qf-drv-row { display:flex; justify-content:space-between; align-items:baseline; gap:8px; margin-top:7px; }
.qf-drv-name { font-weight:700; color:#cbd5e1; font-size:0.80rem; }
.qf-drv-w { color:#5b6677; font-size:0.70rem; font-weight:500; }
.qf-drv-val { font-weight:800; font-size:0.80rem; }
.qf-drv-detail { color:#8b97a7; font-size:0.735rem; line-height:1.4; margin-top:2px; }
.qf-pos { color:#4ade80; } .qf-neg { color:#f87171; } .qf-neu { color:#94a3b8; }
/* Pair scanner rows */
.qf-prow { display:grid; grid-template-columns:96px 72px 96px 56px 1fr; gap:12px; align-items:center;
           padding:11px 14px; border-bottom:1px solid #161c25; }
.qf-prow.head { color:#64748b; font-size:0.66rem; letter-spacing:.06em; text-transform:uppercase;
                font-weight:700; border-bottom:1px solid #243041; }
.qf-pair { font-weight:800; color:#e2e8f0; font-size:0.95rem; }
.qf-pnote { color:#8b97a7; font-size:0.76rem; }
.qf-wrap { background:#0b0f15; border:1px solid #1e2530; border-radius:14px; overflow:hidden; }
/* COT confidence table */
.qf-cotrow { display:grid; grid-template-columns:64px 104px 88px 92px 1fr; gap:12px; align-items:center;
             padding:11px 14px; border-bottom:1px solid #161c25; }
.qf-cotrow.head { color:#64748b; font-size:0.66rem; letter-spacing:.06em; text-transform:uppercase;
                  font-weight:700; border-bottom:1px solid #243041; }
/* Risk Events calendar rows */
.qf-calday { color:#9aa6b6; font-weight:800; font-size:0.92rem; margin:14px 0 4px;
             padding-bottom:6px; border-bottom:1px solid #1e2530; }
.qf-cal { display:grid; grid-template-columns:62px 52px 64px 1fr 132px 118px 92px; gap:10px;
          align-items:center; padding:10px 14px; border-bottom:1px solid #141a22; font-size:0.78rem; }
.qf-cal.head { color:#64748b; font-size:0.64rem; letter-spacing:.05em; text-transform:uppercase;
               font-weight:700; border-bottom:1px solid #243041; }
.qf-cal .ev { color:#dbe2ea; font-weight:600; }
.qf-cal .afp { color:#8b97a7; font-size:0.74rem; }
.qf-imp-h { color:#f87171; font-weight:700; } .qf-imp-m { color:#f59e0b; font-weight:700; }
.qf-imp-l { color:#6b7280; font-weight:600; }
/* COT side-by-side CFTC | A1 */
.qf-grp { color:#9aa6b6; font-weight:800; font-size:0.84rem; letter-spacing:.04em; text-transform:uppercase;
          margin:16px 0 6px; }
.qf-cmp { display:grid; grid-template-columns:74px 1fr 104px 1fr; gap:12px; align-items:center;
          padding:12px 14px; border-bottom:1px solid #161c25; }
.qf-cmp.head { color:#64748b; font-size:0.64rem; letter-spacing:.05em; text-transform:uppercase;
               font-weight:700; border-bottom:1px solid #243041; }
.qf-cmpname { font-weight:800; color:#e2e8f0; font-size:0.92rem; }
.qf-side-h { color:#5b6677; font-size:0.58rem; text-transform:uppercase; letter-spacing:.07em; font-weight:700; }
.qf-side-m { color:#8b97a7; font-size:0.72rem; margin-top:1px; }
.qf-lsbar { height:6px; border-radius:4px; overflow:hidden; display:flex; margin-top:4px; }
.qf-ls { color:#5b6677; font-size:0.66rem; margin-top:2px; }
.qf-align { text-align:center; font-weight:800; font-size:0.74rem; }
</style>""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Guard — jika import gagal
# ---------------------------------------------------------------------------
if not _IMPORTS_OK:
    st.error(f"❌ Import gagal: `{_IMPORT_ERR_MSG}`")
    st.info("Pastikan kamu menjalankan dari root direktori repo dan semua dependency terinstall.")
    st.stop()

# ===========================================================================
# CACHED COLLECTORS  (TTL dari config)
# ===========================================================================

@st.cache_data(ttl=TTL["prices"], show_spinner=False)
def cached_get_prices() -> dict:
    try:
        return _get_prices_raw()
    except Exception as exc:
        logger.error("get_prices() exception: %s", exc)
        return {"as_of_utc": fmt_iso_utc(now_utc()), "prices": {}, "_error": str(exc)}


@st.cache_data(ttl=TTL["macro"], show_spinner=False)
def cached_get_macro(calendar_events_json: str) -> dict:
    """calendar_events_json: json-encoded list agar st.cache_data bisa hash."""
    import json
    try:
        cal_events = json.loads(calendar_events_json) if calendar_events_json else []
        return _get_macro_raw(calendar_events=cal_events)
    except Exception as exc:
        logger.error("get_macro() exception: %s", exc)
        return {
            "as_of_utc": fmt_iso_utc(now_utc()),
            "rates": {}, "rate_diff": {}, "surprises": {},
            "_meta": {"sources_ok": [], "sources_failed": [f"macro error: {exc}"]},
            "_error": str(exc),
        }


@st.cache_data(ttl=TTL["cot"], show_spinner=False)
def cached_get_cot() -> dict:
    try:
        return _get_cot_raw()
    except Exception as exc:
        logger.error("get_cot() exception: %s", exc)
        return {
            "as_of_tuesday": "N/A", "released": "N/A", "days_since_snapshot": 99,
            "cot": {},
            "_meta": {"source": "CFTC TFF", "weeks_history": 0,
                      "assets_ok": [], "assets_missing": [], "stale": True},
            "_error": str(exc),
        }


@st.cache_data(ttl=TTL["retail"], show_spinner=False)
def cached_get_retail() -> dict:
    try:
        return _get_retail_raw()
    except Exception as exc:
        logger.error("get_retail() exception: %s", exc)
        return {
            "as_of_utc": fmt_iso_utc(now_utc()),
            "sources_ok": [], "sources_failed": ["all"], "retail": {},
            "_error": str(exc),
        }


@st.cache_data(ttl=TTL["news"], show_spinner=False)
def cached_get_news() -> dict:
    try:
        return _get_news_raw()
    except Exception as exc:
        logger.error("get_news() exception: %s", exc)
        return {
            "as_of_utc": fmt_iso_utc(now_utc()),
            "headlines": [], "_error": str(exc),
        }


@st.cache_data(ttl=TTL["calendar"], show_spinner=False)
def cached_get_calendar() -> dict:
    try:
        return _get_calendar_raw()
    except Exception as exc:
        logger.error("get_calendar() exception: %s", exc)
        return {
            "as_of_utc": fmt_iso_utc(now_utc()),
            "events": [], "_error": str(exc),
        }


@st.cache_data(ttl=TTL["macro"], show_spinner=False)
def cached_get_eu_actuals() -> dict:
    """Cache fetch actual EUR dari Eurostat (network; flash bulanan → TTL macro 6 jam)."""
    try:
        return _get_eu_actuals_raw()
    except Exception as exc:
        logger.error("get_eu_actuals() exception: %s", exc)
        return {}


@st.cache_data(ttl=TTL["news_overlay"], show_spinner=False)
def cached_compute_news_delta(headlines_json: str, override_json: str = "") -> tuple[dict, list]:
    """Cache news_overlay (proses mahal). Terima json string utk hashability.
    override_json: peta {event_title: {scores, impact}} dari Groq (kosong = keyword)."""
    import json
    try:
        headlines = json.loads(headlines_json) if headlines_json else []
        override = json.loads(override_json) if override_json else None
        return compute_news_delta(headlines, direction_override=override)
    except Exception as exc:
        logger.error("compute_news_delta() exception: %s", exc)
        return {}, []


def _groq_key() -> str:
    """Ambil GROQ_API_KEY dari st.secrets (atau env). '' kalau tak ada."""
    try:
        k = st.secrets.get("GROQ_API_KEY", "")
    except Exception:
        k = ""
    if not k:
        import os
        k = os.environ.get("GROQ_API_KEY", "")
    return k or ""


@st.cache_data(ttl=3600, show_spinner=False)
def cached_groq_classify(title: str) -> dict | None:
    """Klasifikasi arah 1 headline via Groq, di-cache 1 jam. None → fallback keyword.
    Groq MENGUKUR arah; engine tetap menghitung poin (news_overlay)."""
    key = _groq_key()
    if not key:
        return None
    try:
        return _groq_classify_raw(title, key)
    except Exception as exc:
        logger.warning("cached_groq_classify gagal: %s", exc)
        return None


def build_groq_override(headlines: list, max_calls: int = 12) -> tuple[dict, dict]:
    """
    Cluster headlines → klasifikasi Groq utk maks `max_calls` cluster paling besar
    (batasi kuota free-tier). Return (override_map, diag).
    override_map: {event_title: {scores, impact, reasoning}} — hanya yang Groq sukses.
    Fallback aman: cluster yang gagal/tak diklasifikasi → tetap keyword di engine.
    """
    from engine.news_overlay import magnitude as _mag
    diag = {"clusters": 0, "classified": 0, "groq_ok": 0, "fallback": 0}
    try:
        clusters = cluster_events(headlines)
    except Exception as exc:
        logger.error("cluster_events gagal (groq prefilter): %s", exc)
        return {}, diag
    diag["clusters"] = len(clusters)
    # Prefilter: prioritaskan cluster magnitude tertinggi (event paling material)
    ranked = sorted(clusters, key=lambda c: _mag(c), reverse=True)[:max_calls]
    override: dict[str, dict] = {}
    for c in ranked:
        diag["classified"] += 1
        res = cached_groq_classify(c["event_title"])
        if res and isinstance(res.get("scores"), dict):
            override[c["event_title"]] = res
            diag["groq_ok"] += 1
        else:
            diag["fallback"] += 1
    return override, diag


# ===========================================================================
# HELPERS DISPLAY
# ===========================================================================

def _bias_color(score: float) -> str:
    """Return hex color untuk skor bias."""
    if score >= 25:
        return "#16a34a"   # hijau
    elif score <= -25:
        return "#dc2626"   # merah
    return "#6b7280"       # abu netral


def _bias_bar_html(score: float, width_px: int = 180) -> str:
    """Render bias bar HTML dari -100 ke +100."""
    pct_abs = min(abs(score), 100) / 100.0
    color = _bias_color(score)
    bar_pct = pct_abs * 50   # 50% = separuh bar (dari tengah)
    if score >= 0:
        left = 50
        bar_width = bar_pct
    else:
        bar_width = bar_pct
        left = 50 - bar_pct
    return (
        f'<div style="position:relative;width:{width_px}px;height:14px;'
        f'background:#e5e7eb;border-radius:4px;">'
        f'<div style="position:absolute;left:50%;top:0;width:1px;height:14px;background:#9ca3af;"></div>'
        f'<div style="position:absolute;left:{left:.1f}%;width:{bar_width:.1f}%;height:14px;'
        f'background:{color};border-radius:3px;"></div>'
        f'</div>'
    )


def _label_html(label: str) -> str:
    css_map = {
        "Strong Bullish": "label-sbull",
        "Bullish": "label-bull",
        "Neutral": "label-neut",
        "Bearish": "label-bear",
        "Strong Bearish": "label-sbear",
    }
    css = css_map.get(label, "label-neut")
    return f'<span class="{css}">{label}</span>'


def _impact_badge(impact: str) -> str:
    cls = {"HIGH": "impact-high", "MED": "impact-med", "LOW": "impact-low"}.get(impact, "impact-low")
    return f'<span class="{cls}">{impact}</span>'


def _dir_html(sign: str) -> str:
    if sign == "+":
        return '<span class="dir-plus">▲</span>'
    elif sign == "-":
        return '<span class="dir-minus">▼</span>'
    return '<span class="dir-zero">—</span>'


def _conf_bar(conf: float) -> str:
    """Confidence bar kecil sebagai pct string."""
    pct = round(conf * 100)
    col = "#15803d" if pct >= 60 else "#d97706" if pct >= 35 else "#dc2626"
    return (
        f'<span style="font-size:0.75rem;color:{col};font-weight:600;">'
        f'{pct}%</span>'
    )


# ===========================================================================
# SECTION 1 — HEADER
# ===========================================================================

def render_header(sources_status: dict[str, Any], labels: dict[str, str] | None = None) -> None:
    """Render judul, timestamp, status sumber, tombol Refresh."""
    labels = labels or {}
    ts_utc = now_utc()
    ts_wib = now_wib()

    col_title, col_ts, col_btn = st.columns([3, 4, 1.2])

    with col_title:
        st.markdown("## 📊 QF_BIAS Dashboard")

    with col_ts:
        st.markdown(
            f"**UTC:** `{fmt_iso_utc(ts_utc)}`  \n"
            f"**WIB:** `{fmt_wib_display(ts_wib)} WIB`",
        )

    with col_btn:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🔄 Refresh", help="Bersihkan cache & muat ulang semua data"):
            clear_all_caches()
            st.rerun()

    # --- Status sumber ---
    if sources_status:
        badge_parts = []
        for src, status in sources_status.items():
            if src.startswith("_"):   # _cot_note dll → bukan badge
                continue
            lbl = labels.get(src, src)
            if status == "ok":
                badge_parts.append(f'<span class="badge-ok">✓ {lbl}</span>')
            elif status == "warn":
                badge_parts.append(f'<span class="badge-warn">⚠ {lbl}</span>')
            else:
                badge_parts.append(f'<span class="badge-fail">✗ {lbl}</span>')
        st.markdown(
            "<div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;'>"
            + " ".join(badge_parts)
            + "</div>",
            unsafe_allow_html=True,
        )
        cot_note = sources_status.get("_cot_note")
        if cot_note:
            st.caption(f"ℹ️ {cot_note}")

    st.divider()


# ===========================================================================
# SECTION 4a — BIAS BOARD
# ===========================================================================

_FACTOR_NAMES = {"R_hard": "Rate diff", "C": "COT", "D": "Retail", "F": "FF surprise",
                 "R_narrative": "News"}


def _chip(score: float, scale100: bool = True) -> str:
    """Pill skor berwarna. score dalam −100..100 (scale100) atau −1..1."""
    v = score if scale100 else score * 100
    cls = "qf-chip-pos" if v >= 8 else ("qf-chip-neg" if v <= -8 else "qf-chip-neu")
    sign = "+" if v >= 0 else ""
    txt = f"{sign}{v:.0f}" if scale100 else f"{score:+.3f}"
    return f'<span class="qf-chip {cls}">{txt}</span>'


def _pressure(score100: float) -> str:
    pos = max(0.0, min(100.0, (score100 + 100.0) / 2.0))
    return f'<div class="qf-bar"><div class="qf-bar-mk" style="left:{pos:.1f}%;"></div></div>'


def _vcls(v: float) -> str:
    return "qf-pos" if v > 0.0005 else ("qf-neg" if v < -0.0005 else "qf-neu")


def _asset_card_html(asset: str, group: str, score: float, label: str, conf: float,
                     drivers: dict, delta: float = 0.0, show_overlay: bool = False) -> str:
    """Kartu aset metavulus (self-contained) — driver selalu tampil & terbaca."""
    chip = _chip(score)
    lab_cls = _vcls(score / 100.0)
    meta_l = f"confidence {conf*100:.0f}%" if conf else "confidence —"
    meta_r = ""
    if show_overlay and abs(delta) > 0.1:
        dd = f"+{delta:.1f}" if delta >= 0 else f"{delta:.1f}"
        meta_r = f'<span class="{_vcls(delta)}">Δnews {dd}</span>'
    drv_html = ""
    for f, fd in (drivers or {}).items():
        fs = fd.get("score", 0.0)
        fw = fd.get("weight", 0.0)
        det = (fd.get("detail") or "–").strip()
        # pecah tiap ';' dan '|' ke baris baru biar tidak bertabrakan
        for sep in ("|", ";"):
            det = det.replace(sep, "\n")
        det_lines = [p.strip() for p in det.split("\n") if p.strip()]
        det_html = "<br>".join(det_lines) if det_lines else "–"
        nm = _FACTOR_NAMES.get(f, f)
        drv_html += (
            f'<div class="qf-drv-row"><span class="qf-drv-name">{nm} '
            f'<span class="qf-drv-w">w{fw:.2f}</span></span>'
            f'<span class="qf-drv-val {_vcls(fs)}">{fs:+.3f}</span></div>'
            f'<div class="qf-drv-detail">{det_html}</div>'
        )
    drv_block = f'<div class="qf-drv">{drv_html}</div>' if drv_html else ""
    return (
        f'<div class="qf-card">'
        f'<div class="qf-head"><div><div class="qf-micro">{group}</div>'
        f'<div class="qf-asset">{asset}</div></div>{chip}</div>'
        f'<div class="qf-label {lab_cls}">{label}</div>'
        f'{_pressure(score)}'
        f'<div class="qf-meta"><span>{meta_l}</span>{meta_r}</div>'
        f'{drv_block}</div>'
    )


def render_bias_board(
    asset_data: dict[str, dict],
    news_delta: dict[str, float],
    show_overlay: bool,
) -> None:
    """Render grid kartu per aset dengan bar skor, label, confidence, driver breakdown."""

    st.subheader("📈 Bias Board")
    st.caption("Mulai dari asetnya, lalu susun rencana. Skor = confluence faktor (bukan sinyal arah). "
               "Driver tampil penuh di tiap kartu.")

    if not asset_data:
        st.warning("Data bias aset kosong — periksa collectors.")
        return

    groups = [
        ("FX Majors", "FX", ASSETS_FX),
        ("Commodities", "METAL", [ASSET_GOLD]),
        ("Crypto", "CRYPTO", ASSETS_CRYPTO),
    ]

    for group_name, micro, group_assets in groups:
        st.markdown(f"#### {group_name}")
        # tata ulang jadi baris berisi 3 kartu (lebar → driver terbaca)
        for i in range(0, len(group_assets), 3):
            chunk = group_assets[i:i + 3]
            cols = st.columns(3)
            for col, asset in zip(cols, chunk):
                data = asset_data.get(asset)
                if data is None:
                    col.warning(f"{asset}: no data")
                    continue
                baseline = data.get("bias_baseline", 0.0)
                delta = news_delta.get(asset, 0.0)
                if show_overlay:
                    score = max(-100.0, min(100.0, baseline + delta))
                else:
                    score = baseline
                label = bias_label(score)
                conf = data.get("confidence", 0.0) or 0.0
                drivers = data.get("drivers", {})
                with col:
                    st.markdown(
                        _asset_card_html(asset, micro, score, label, conf, drivers, delta, show_overlay),
                        unsafe_allow_html=True,
                    )


# ===========================================================================
# SECTION 4b — PAIR SCANNER
# ===========================================================================

_FX_PREC = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"]


def _all_pairs_from_assets(asset_data: dict, overlay_map: dict | None = None) -> list[dict]:
    """Semua pair berdasar konvensi quoting FX (base=presedensi lebih tinggi) +
    XAU/BTC/ETH vs USD. Skor = base_bias − quote_bias (clamp ±100)."""
    src = overlay_map or asset_data

    def bias(a: str) -> float:
        return src.get(a, {}).get("bias_baseline", 0.0)

    def conf2(a: str, b: str):
        cs = [c for c in (asset_data.get(a, {}).get("confidence"),
                          asset_data.get(b, {}).get("confidence")) if c is not None]
        return sum(cs) / len(cs) if cs else None

    def mk(a: str, b: str) -> dict:
        sc = max(-100.0, min(100.0, bias(a) - bias(b)))
        return {"pair": f"{a}{b}", "bias_score": sc, "confidence": conf2(a, b),
                "label": bias_label(sc),
                "note": f"{a}·{bias(a):.2f} − {b}·{bias(b):.2f} = {sc:.2f}"}

    out: list[dict] = []
    fx = [c for c in _FX_PREC if c in asset_data]
    for i, a in enumerate(fx):
        for b in fx[i + 1:]:
            out.append(mk(a, b))                       # a presedensi > b → base=a
    for m in [ASSET_GOLD] + list(ASSETS_CRYPTO):
        if m in asset_data and "USD" in asset_data:
            out.append(mk(m, "USD"))
    return out


def render_pair_scanner(
    pair_data: dict[str, dict],
    asset_data: dict[str, dict],
    show_overlay: bool,
    news_delta: dict[str, float],
) -> None:
    """Render Pair Scanner: selectbox base/quote + tabel ranking pair terkuat."""

    st.subheader("🔍 Pair Scanner")

    if not pair_data:
        st.warning("Data pair kosong — periksa engine/scoring.")
        return

    all_assets = list(ASSETS_FX) + [ASSET_GOLD] + list(ASSETS_CRYPTO)
    col_b, col_q = st.columns(2)
    with col_b:
        sel_base = st.selectbox("Base Currency", options=all_assets, index=0, key="ps_base")
    with col_q:
        sel_quote = st.selectbox("Quote Currency", options=all_assets, index=1, key="ps_quote")

    # Panel info dirender SETELAH kedua selectbox terbaca (hindari stale value).
    info_box = st.container()
    with info_box:
        if sel_base == sel_quote:
            st.warning("Base dan quote tidak boleh sama.")
        else:
            # Cari pair symbol yang cocok
            pair_sym = f"{sel_base}{sel_quote}"
            pair_rev = f"{sel_quote}{sel_base}"

            pair_found = pair_data.get(pair_sym) or pair_data.get(pair_rev)
            actual_sym = pair_sym if pair_data.get(pair_sym) else pair_rev

            if pair_found:
                p = pair_found
                score = p.get("bias_score", 0.0)

                # Kalau overlay aktif, recalculate pair bias langsung
                if show_overlay:
                    base = p.get("base", sel_base)
                    quote = p.get("quote", sel_quote)
                    base_score = (asset_data.get(base, {}).get("bias_baseline", 0.0)
                                  + news_delta.get(base, 0.0))
                    quote_score = (asset_data.get(quote, {}).get("bias_baseline", 0.0)
                                   + news_delta.get(quote, 0.0))
                    score = max(-100.0, min(100.0, base_score - quote_score))

                label = bias_label(score)
                conf = p.get("confidence", None)
                note = p.get("note", "")

                sign_str = "+" if score >= 0 else ""
                st.markdown(
                    f"<div style='font-size:1.1rem;font-weight:700;'>{actual_sym}</div>"
                    f"<div style='font-size:2rem;font-weight:700;color:{_bias_color(score)};'>"
                    f"{sign_str}{score:.1f}</div>"
                    f"{_label_html(label)}&nbsp;",
                    unsafe_allow_html=True,
                )

                if conf is not None:
                    st.markdown(f"**Confidence:** {_conf_bar(conf)}", unsafe_allow_html=True)

                st.markdown(_bias_bar_html(score, width_px=220), unsafe_allow_html=True)

                if note:
                    st.caption(f"_{note}_")
            else:
                # Hitung manual dari asset bias
                base_data = asset_data.get(sel_base)
                quote_data = asset_data.get(sel_quote)
                if base_data is not None and quote_data is not None:
                    base_b = base_data.get("bias_baseline", 0.0)
                    quote_b = quote_data.get("bias_baseline", 0.0)
                    if show_overlay:
                        base_b += news_delta.get(sel_base, 0.0)
                        quote_b += news_delta.get(sel_quote, 0.0)
                    score = max(-100.0, min(100.0, base_b - quote_b))
                    label = bias_label(score)
                    sign_str = "+" if score >= 0 else ""
                    st.markdown(
                        f"<div style='font-size:1.1rem;font-weight:700;'>"
                        f"{sel_base}/{sel_quote} <span style='font-size:0.75rem;color:#9ca3af;'>(custom)</span></div>"
                        f"<div style='font-size:2rem;font-weight:700;color:{_bias_color(score)};'>"
                        f"{sign_str}{score:.1f}</div>"
                        f"{_label_html(label)}",
                        unsafe_allow_html=True,
                    )
                    st.markdown(_bias_bar_html(score, width_px=220), unsafe_allow_html=True)
                    st.caption(f"Dihitung manual: {sel_base}({base_b:.1f}) − {sel_quote}({quote_b:.1f})")
                else:
                    st.info(f"Pair {pair_sym} tidak ada di data terkomputasi.")

    # --- Ranking semua pair (sort skor tertinggi → terendah) ---
    st.markdown("#### Ranking Pair — skor tertinggi dulu")
    try:
        overlay_map = None
        if show_overlay:
            overlay_map = {}
            for asset, adata in asset_data.items():
                baseline = adata.get("bias_baseline", 0.0)
                overlay_map[asset] = {**adata,
                                      "bias_baseline": max(-100.0, min(100.0,
                                                       baseline + news_delta.get(asset, 0.0)))}
        ranked = _all_pairs_from_assets(asset_data, overlay_map)
        ranked = sorted(ranked, key=lambda r: r.get("bias_score", 0.0), reverse=True)

        if ranked:
            rows = ('<div class="qf-prow head"><span>Pair</span><span>Skor</span>'
                    '<span>Label</span><span>Conf</span><span>Kalkulasi</span></div>')
            for r in ranked:
                sc = r.get("bias_score", 0.0)
                conf_r = r.get("confidence")
                conf_str = f"{conf_r*100:.0f}%" if conf_r is not None else "–"
                note = (r.get("note") or "").strip() or "–"
                rows += (
                    f'<div class="qf-prow"><span class="qf-pair">{r["pair"]}</span>'
                    f'{_chip(sc)}'
                    f'<span class="qf-label {_vcls(sc/100.0)}" style="font-size:0.82rem;">'
                    f'{r.get("label", bias_label(sc))}</span>'
                    f'<span class="qf-pnote">{conf_str}</span>'
                    f'<span class="qf-pnote">{note}</span></div>'
                )
            st.markdown(f'<div class="qf-wrap">{rows}</div>', unsafe_allow_html=True)
            st.caption(f"{len(ranked)} pair (semua kombinasi FX + XAU/BTC/ETH vs USD).")
        else:
            st.info("Tidak ada pair yang bisa di-rank.")
    except Exception as exc:
        st.warning(f"Ranking pair gagal: {exc}")


# ===========================================================================
# SECTION 4c — NEWS FEED
# ===========================================================================

def render_news_feed(
    news_clusters: list[dict],
    news_delta_map: dict[str, float] | None = None,
    news_meta: dict | None = None,
    groq_diag: dict | None = None,
) -> None:
    """Render news clusters (sudah dedup) dari engine/news_overlay.

    news_delta_map : net Δ per aset (untuk ringkasan di atas feed).
    news_meta       : dict hasil get_news (untuk status sumber: ok/gagal).
    """
    news_delta_map = news_delta_map or {}
    news_meta = news_meta or {}

    hcol, rcol = st.columns([5, 1])
    with hcol:
        st.subheader("📰 News Feed (Sudah Keluar)")
    with rcol:
        if st.button("🔄 Refresh", key="refresh_news", help="Muat ulang berita terbaru", use_container_width=True):
            clear_all_caches()
            st.rerun()

    # --- Status sumber (mana yang hidup / gagal) — tutup celah "error ditelan" ---
    ok = news_meta.get("sources_ok", [])
    failed = news_meta.get("sources_failed", [])
    if ok or failed:
        parts = []
        if ok:
            parts.append("✅ aktif: " + ", ".join(ok))
        if failed:
            parts.append("✗ gagal: " + ", ".join(failed))
        st.caption(" &nbsp;·&nbsp; ".join(parts))
    if news_meta.get("error") and not news_clusters:
        st.warning(f"Semua feed news gagal: {news_meta['error']}")

    if groq_diag:
        st.caption(
            f"🤖 Groq arah: {groq_diag.get('clusters',0)} cluster · "
            f"{groq_diag.get('classified',0)} dikirim · **{groq_diag.get('groq_ok',0)} terklasifikasi Groq** · "
            f"{groq_diag.get('fallback',0)} fallback keyword. Engine tetap hitung poin (cap ±30 placeholder)."
        )

    if not news_clusters:
        st.info("Tidak ada news cluster saat ini — feed kosong atau semua event sudah decay.")
        return

    # --- Ringkasan net news Δ per aset (TAMBAHAN b) ---
    nz_delta = {a: v for a, v in news_delta_map.items() if abs(v) >= 0.05}
    if nz_delta:
        chips = []
        for a, v in sorted(nz_delta.items(), key=lambda kv: -abs(kv[1])):
            col = "#16a34a" if v > 0 else "#dc2626"
            chips.append(
                f"<span style='background:#111827;border:1px solid {col};color:{col};"
                f"padding:2px 8px;border-radius:10px;font-size:0.78rem;font-weight:700;'>"
                f"{a} {v:+.1f}</span>"
            )
        st.markdown(
            "<div style='font-size:0.72rem;color:#6b7280;text-transform:uppercase;"
            "letter-spacing:0.04em;margin-bottom:4px;'>Net News Δ (cap ±30, placeholder)</div>"
            "<div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;'>"
            + " ".join(chips) + "</div>",
            unsafe_allow_html=True,
        )

    # --- Kontrol: filter aset, sembunyikan netral, sort (TAMBAHAN a + c) ---
    avail_assets = sorted({
        a for c in news_clusters
        for a, v in c.get("direction", {}).items() if v != "0"
    })
    f1, f2, f3, f4 = st.columns([2.0, 1.3, 1.3, 1.3])
    with f1:
        asset_filter = st.multiselect(
            "Filter aset", options=avail_assets, default=[],
            key="nf_asset", help="Kosong = semua. Hanya tampilkan cluster yang menyentuh aset terpilih.",
        )
    with f2:
        hide_neutral = st.toggle(
            "Sembunyikan netral", value=True, key="nf_hideneutral",
            help="Sembunyikan cluster tanpa reaksi aset (–) — buang noise.",
        )
    with f3:
        sort_mode = st.selectbox(
            "Urutkan", options=["Terbaru", "Magnitude"], index=0, key="nf_sort",
        )
    with f4:
        impact_filter = st.multiselect(
            "Impact (Groq)", options=["high", "med", "low"], default=[],
            key="nf_impact", help="Filter by impact hasil Groq. Kosong = semua. "
                                   "Cluster tanpa klasifikasi Groq dianggap lolos.",
        )

    # --- Terapkan filter ---
    filtered = []
    for c in news_clusters:
        reactions = {a: v for a, v in c.get("direction", {}).items() if v != "0"}
        if hide_neutral and not reactions:
            continue
        if asset_filter and not any(a in reactions for a in asset_filter):
            continue
        # Impact filter: cluster tanpa klasifikasi Groq (impact "") dianggap lolos.
        if impact_filter and c.get("impact", "") and c.get("impact") not in impact_filter:
            continue
        filtered.append(c)

    if not filtered:
        st.info("Tidak ada cluster yang lolos filter saat ini.")
        return

    # --- Terapkan sort ---
    if sort_mode == "Magnitude":
        sorted_clusters = sorted(filtered, key=lambda c: c.get("magnitude", 0.0), reverse=True)
    else:
        sorted_clusters = sorted(filtered, key=lambda c: c.get("age_min", 9999))

    st.caption(f"Menampilkan {len(sorted_clusters)} dari {len(news_clusters)} cluster")

    for cluster in sorted_clusters:
        event_title = cluster.get("event", "–")
        n_hl = cluster.get("n_headlines", 1)
        age = cluster.get("age_min", 0.0)
        direction = cluster.get("direction", {})
        mag = cluster.get("magnitude", 0.0)

        # Format umur
        if age < 1:
            age_str = "baru saja"
        elif age < 60:
            age_str = f"{int(age)}m lalu"
        else:
            h = int(age // 60)
            m = int(age % 60)
            age_str = f"{h}j {m}m lalu" if m else f"{h}j lalu"

        # Reaksi aset yang non-zero
        reactions = {a: v for a, v in direction.items() if v != "0"}

        link = cluster.get("link", "")

        with st.container():
            col_event, col_meta, col_react, col_act = st.columns([3.6, 1.4, 2.2, 1.4])

            with col_event:
                # Judul + link "Buka" kalau ada
                if link:
                    st.markdown(
                        f"<div style='font-weight:600;font-size:0.9rem;'>{event_title} "
                        f"<a href='{link}' target='_blank' style='font-size:0.72rem;color:#60a5fa;text-decoration:none;'>🔗 buka</a></div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div style='font-weight:600;font-size:0.9rem;'>{event_title}</div>",
                        unsafe_allow_html=True,
                    )

            with col_meta:
                _impact = cluster.get("impact", "")
                _src = cluster.get("source", "keyword")
                _impact_html = ""
                if _impact:
                    _ic = {"high": "#dc2626", "med": "#d97706", "low": "#6b7280"}.get(_impact, "#6b7280")
                    _impact_html = (f"<br><span style='color:{_ic};font-weight:700;'>impact: {_impact}</span>"
                                    f" <span style='color:#6b7280;'>· {_src}</span>")
                st.markdown(
                    f"<div style='font-size:0.78rem;color:#6b7280;'>"
                    f"📰 {n_hl} hl &nbsp;|&nbsp; ⏱ {age_str}<br>"
                    f"mag: {mag:.2f}{_impact_html}</div>",
                    unsafe_allow_html=True,
                )

            with col_react:
                if reactions:
                    react_parts = []
                    for asset, sign in sorted(reactions.items()):
                        react_parts.append(f"{asset}{_dir_html(sign)}")
                    st.markdown(
                        "<div style='font-size:0.82rem;display:flex;gap:6px;flex-wrap:wrap;'>"
                        + " ".join(react_parts)
                        + "</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        "<span style='color:#9ca3af;font-size:0.78rem;'>–</span>",
                        unsafe_allow_html=True,
                    )

            with col_act:
                # On-demand: klasifikasi arah headline ini via Groq (Groq MENGUKUR;
                # engine yang hitung poin). Nonaktif bila tak ada API key.
                _gkey = bool(_groq_key())
                _bkey = f"groq_{abs(hash(event_title))%10**8}"
                if st.button(
                    "🤖 Groq context",
                    key=_bkey,
                    disabled=not _gkey,
                    help=("Klasifikasi arah + impact headline ini via Groq. "
                          "Groq mengukur arah; engine deterministik yang hitung poin."
                          if _gkey else "GROQ_API_KEY belum ada di Secrets."),
                    use_container_width=True,
                ):
                    _res = cached_groq_classify(event_title)
                    if _res:
                        _dirs = {a: ("+" if s > 0 else "-") for a, s in _res["scores"].items() if s != 0}
                        _dtxt = ", ".join(f"{a}{d}" for a, d in sorted(_dirs.items())) or "tak ada arah jelas"
                        st.caption(f"🤖 {_res.get('impact','?')} · {_dtxt}")
                        if _res.get("reasoning"):
                            st.caption(f"_{_res['reasoning']}_")
                    else:
                        st.caption("Groq tak tersedia (limit/down) — pakai keyword.")

            st.markdown(
                "<hr style='border:none;border-top:1px solid #1f2937;margin:4px 0;'>",
                unsafe_allow_html=True,
            )


# ===========================================================================
# SECTION 4d — KEY RISK EVENTS
# ===========================================================================

@st.cache_data(ttl=3600, show_spinner=False)
def cached_vision_extract(image_bytes: bytes, media_type: str) -> list | None:
    """Groq Scout baca screenshot kalender (cache per-gambar). None → input manual."""
    key = _groq_key()
    if not key:
        return None
    try:
        import base64
        b64 = base64.b64encode(image_bytes).decode()
        return _groq_vision_raw(b64, key, media_type=media_type)
    except Exception as exc:
        logger.warning("cached_vision_extract gagal: %s", exc)
        return None


def render_manual_actual_input(calendar_data: dict) -> None:
    """Form input ACTUAL manual + opsi baca screenshot (Groq vision). Disimpan ke
    st.session_state['manual_actuals'] → main() inject sebelum scoring (rerun)."""
    events = calendar_data.get("events", [])
    released = [e for e in events
                if e.get("status") == "released"
                and e.get("impact") in ("HIGH", "MED")]
    if not released:
        return

    st.session_state.setdefault("manual_actuals", {})
    st.session_state.setdefault("vision_sugg", {})

    with st.expander("✍️ Input Actual Manual (+ baca screenshot ForexFactory)", expanded=False):
        st.caption("Isi **actual** sambil lihat screenshot FF-mu. Forecast/previous sudah dari feed. "
                   "Tersimpan → engine hitung surprise → R_hard (σ placeholder). Manual menimpa sumber lain.")

        _gkey = bool(_groq_key())
        up = st.file_uploader("Screenshot kalender (opsional — Groq baca → pra-isi)",
                              type=["png", "jpg", "jpeg"], key="ma_upload",
                              disabled=not _gkey,
                              help="Groq Scout transkrip tabel → dicocokkan + cross-check forecast/previous. "
                                   "Kamu tetap konfirmasi sebelum disimpan." if _gkey
                                   else "GROQ_API_KEY belum ada di Secrets.")
        if up is not None and st.button("🔍 Baca screenshot (Groq)", key="ma_readbtn"):
            with st.spinner("Groq membaca screenshot…"):
                rows = cached_vision_extract(up.getvalue(), up.type or "image/png")
            if not rows:
                st.warning("Vision gagal/kosong — isi manual saja di bawah.")
            else:
                sugg = match_vision_rows(events, rows)
                st.session_state["vision_sugg"] = {s["event_id"]: s for s in sugg}
                n_hi = sum(1 for s in sugg if s["confidence"] == "high")
                st.success(f"Terbaca {len(rows)} baris → {len(sugg)} cocok ({n_hi} confidence tinggi). "
                           "Cek nilai di bawah, lalu Simpan.")

        sugg_map = st.session_state.get("vision_sugg", {})
        saved = st.session_state.get("manual_actuals", {})

        with st.form("manual_actual_form"):
            for ev in released:
                eid = make_event_id(ev)
                fc, pv = ev.get("forecast"), ev.get("previous")
                cur = ev.get("actual")
                prefill = saved.get(eid)
                if prefill is None and eid in sugg_map:
                    prefill = sugg_map[eid]["actual"]
                if prefill is None and cur is not None:
                    prefill = cur
                c1, c2, c3, c4 = st.columns([3.4, 1, 1, 1.6])
                with c1:
                    tag = ""
                    if eid in sugg_map:
                        sc = sugg_map[eid]
                        col = "#16a34a" if sc["confidence"] == "high" else "#d97706"
                        tag = f" <span style='color:{col};font-size:0.7rem;'>🤖 {sc['confidence']}</span>"
                    st.markdown(f"<div style='font-size:0.82rem;'><b>{ev.get('currency')}</b> "
                                f"{ev.get('name')}{tag}</div>", unsafe_allow_html=True)
                with c2:
                    st.caption(f"F: {fc if fc is not None else '–'}")
                with c3:
                    st.caption(f"P: {pv if pv is not None else '–'}")
                with c4:
                    st.number_input("actual", value=prefill, key=f"ma_{eid}",
                                    label_visibility="collapsed", format="%.4f")
            cc1, cc2 = st.columns([1, 1])
            submit = cc1.form_submit_button("💾 Simpan & hitung ulang", use_container_width=True)
            clear = cc2.form_submit_button("🗑 Hapus semua manual", use_container_width=True)

        if submit:
            new_map = {}
            for ev in released:
                eid = make_event_id(ev)
                val = st.session_state.get(f"ma_{eid}")
                if val is not None:
                    new_map[eid] = float(val)
            st.session_state["manual_actuals"] = new_map
            st.session_state["vision_sugg"] = {}
            st.success(f"{len(new_map)} actual disimpan — menghitung ulang…")
            st.rerun()
        if clear:
            st.session_state["manual_actuals"] = {}
            st.session_state["vision_sugg"] = {}
            st.rerun()


def render_key_risk_events(calendar_data: dict) -> None:
    """Risk events 3 mode: Hari Ini (07:00 WIB cycle), Minggu Ini (Sen-Min + filter hari),
    Historis (2 minggu ke belakang dgn aktual). + tombol refresh lokal."""

    from datetime import timedelta

    hcol, rcol = st.columns([5, 1])
    with hcol:
        st.subheader("⏰ Key Risk Events")
    with rcol:
        if st.button("🔄 Refresh", key="refresh_risk", help="Muat ulang kalendar", use_container_width=True):
            clear_all_caches()
            st.rerun()

    events = calendar_data.get("events", [])
    _d = calendar_data.get("_surprise_diag") or {}
    _eu = calendar_data.get("_eu_diag") or {}
    if _eu.get("matched", 0) > 0:
        st.caption(
            f"🇪🇺 Eurostat actual (EUR): {_eu.get('matched',0)} event cocok · "
            f"**{_eu.get('filled',0)} terisi** · {_eu.get('misaligned',0)} ditolak alignment guard · "
            f"{_eu.get('no_data',0)} tanpa data."
        )
    if _d.get("released", 0) > 0:
        st.caption(
            f"📈 Surprise → R_hard: {_d.get('released',0)} released · "
            f"{_d.get('released_actual',0)} ada actual (Eurostat utk EUR; sumber resmi lain menyusul) · "
            f"**{_d.get('scored',0)} di-score** · {_d.get('no_sigma',0)} tanpa σ (display-only) · "
            f"{_d.get('skipped',0)} di-skip (rate/speech). σ + bobot = placeholder sampai backtest."
        )
    else:
        st.caption("ℹ️ Belum ada event released minggu ini. Surprise → R_hard aktif begitu ada "
                   "actual (API resmi) pada indikator yang dikenal sigma_table.")
    if calendar_data.get("_error") and not events:
        st.warning(f"Calendar fetch gagal: {calendar_data['_error']}")
        return
    if not events:
        st.info("Tidak ada event dalam window.")
        return

    _md = calendar_data.get("_manual_diag") or {}
    if _md.get("applied", 0) > 0:
        st.caption(f"✍️ Actual manual aktif: **{_md['applied']} event** (menimpa sumber lain) → masuk R_hard.")
    render_manual_actual_input(calendar_data)

    now_w = now_wib()

    # --- Window "Hari Ini" = 07:00 WIB hari ini → 07:00 WIB besok ---
    today_anchor = now_w.replace(hour=7, minute=0, second=0, microsecond=0)
    if now_w.hour < 7:
        today_anchor = today_anchor - timedelta(days=1)  # belum jam 7 → cycle kemarin
    today_start = today_anchor
    today_end = today_anchor + timedelta(days=1)

    # --- Window "Minggu Ini" = Senin 00:00 → Minggu 23:59 WIB ---
    monday = (now_w - timedelta(days=now_w.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    sunday_end = monday + timedelta(days=7)

    def _ev_wib(ev: dict):
        """Parse ts_utc → WIB. Tahan-banting: tidak bergantung HANYA pada
        parse_iso_utc (yang bisa beda versi di deploy). Kalau gagal total,
        return None — tapi kegagalan ini DIHITUNG di diagnostik (bukan ditelan)."""
        ts = ev.get("ts_utc")
        if not ts:
            return None
        # 1) jalur normal
        try:
            return parse_iso_utc(ts).astimezone(now_w.tzinfo)
        except Exception:
            pass
        # 2) fallback parser mandiri (Z / offset / naive / spasi)
        try:
            from datetime import datetime as _dt, timezone as _tz
            s = str(ts).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            if " " in s and "T" not in s:
                s = s.replace(" ", "T", 1)
            d = _dt.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=_tz.utc)
            return d.astimezone(now_w.tzinfo)
        except Exception:
            return None

    # ---- MODE PICKER: Last / This / Upcoming Week ----
    mode = st.radio(
        "Tampilan",
        options=["📅 Hari Ini", "🗓️ Minggu Ini"],
        horizontal=True, key="re_mode", label_visibility="collapsed",
    )

    # ---- FILTER BAR (impact + currency selalu ada) ----
    fcol1, fcol2 = st.columns(2)
    with fcol1:
        impact_filter = st.multiselect(
            "Filter Impact", options=["HIGH", "MED", "LOW"],
            default=["HIGH", "MED"], key="re_impact",
        )
    with fcol2:
        avail_ccy = sorted({e.get("currency", "?") for e in events if e.get("currency")})
        ccy_filter = st.multiselect(
            "Filter Currency", options=avail_ccy, default=[], key="re_ccy",
            help="Kosong = semua.",
        )

    def _base_match(ev: dict) -> bool:
        if impact_filter and ev.get("impact", "LOW") not in impact_filter:
            return False
        if ccy_filter and ev.get("currency") not in ccy_filter:
            return False
        return True

    def _in_window(ev: dict, w_start, w_end) -> bool:
        ew = _ev_wib(ev)
        return ew is not None and w_start <= ew < w_end

    # Tentukan window minggu sesuai mode
    last_monday = monday - timedelta(days=7)
    next_monday = monday + timedelta(days=7)
    next_sunday_end = next_monday + timedelta(days=7)

    day_names = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    day_filter = None
    w_start = w_end = None
    label = ""

    if mode == "📅 Hari Ini":
        w_start, w_end = today_start, today_end
        st.caption(f"Window 07:00 WIB cycle: {today_start.strftime('%a %d %b %H:%M')} → "
                   f"{today_end.strftime('%a %d %b %H:%M')} WIB")
    else:  # Minggu Ini (Senin-Minggu) + filter hari
        w_start, w_end = monday, sunday_end
        day_choice = st.selectbox("Hari", options=["(Semua hari)"] + day_names, index=0, key="re_day")
        if day_choice != "(Semua hari)":
            day_filter = day_names.index(day_choice)
        rng = f"{w_start.strftime('%d %b')} → {(w_end-timedelta(days=1)).strftime('%d %b')} WIB"
        st.caption(rng + (f" · {day_names[day_filter]}" if day_filter is not None else ""))

    subset = [e for e in events if _base_match(e) and _in_window(e, w_start, w_end)]
    if day_filter is not None:
        d_start = w_start + timedelta(days=day_filter)
        d_end = d_start + timedelta(days=1)
        subset = [e for e in subset if _in_window(e, d_start, d_end)]

    # Diagnostik rinci: tunjukkan PERSIS berapa event lolos tiap tahap (akhiri tebakan)
    n_total = len(events)
    n_parse_fail = sum(1 for e in events if _ev_wib(e) is None)
    n_in_window = sum(1 for e in events if _in_window(e, w_start, w_end))
    n_impact = sum(1 for e in events if _in_window(e, w_start, w_end)
                   and (not impact_filter or e.get("impact", "LOW") in impact_filter))
    if not subset and events:
        # Pecah penyebab
        statuses = {}
        for e in events:
            if _in_window(e, w_start, w_end):
                statuses[e.get("status", "?")] = statuses.get(e.get("status", "?"), 0) + 1
        ccy_in_window = sorted({e.get("currency", "?") for e in events if _in_window(e, w_start, w_end)})
        sample_ts = [str(e.get("ts_utc")) for e in events[:3]]
        st.warning(
            f"⚠ Diagnostik filter:\n\n"
            f"- Total event ter-fetch: **{n_total}**\n"
            f"- **Gagal parse timestamp: {n_parse_fail}** ← kalau ini = total, masalahnya FORMAT ts_utc (version skew calendar_evt/timeutils), BUKAN window\n"
            f"- Lolos window waktu ini: **{n_in_window}** (status: {statuses})\n"
            f"- Setelah filter impact: **{n_impact}**\n"
            f"- Setelah filter currency: **{len(subset)}**\n\n"
            f"Contoh ts_utc mentah: {sample_ts}\n\n"
            f"Currency yang ADA di window: {ccy_in_window}\n\n"
            f"**Baca:** parse-fail=total → ganti calendar_evt.py+timeutils.py (skew). "
            f"parse-fail=0 tapi window=0 → memang tidak ada event di rentang ini. "
            f"window>0 tapi akhir 0 → filter impact/currency ketat."
        )
    elif n_in_window > 0:
        st.caption(f"📊 {n_in_window} event di window · {n_impact} lolos impact · {len(subset)} setelah currency")

    upcoming = sorted([e for e in subset if e.get("status") == "upcoming"], key=lambda e: e.get("ts_utc", ""))
    released = sorted([e for e in subset if e.get("status") == "released"], key=lambda e: e.get("ts_utc", ""), reverse=True)

    def _render_row(ev: dict, is_released: bool) -> None:
        try:
            ts_wib_str = ev.get("ts_wib", "")
            currency = ev.get("currency", "–")
            impact = ev.get("impact", "LOW")
            name = ev.get("name", "–")
            forecast = ev.get("forecast"); previous = ev.get("previous"); actual = ev.get("actual")
            ts_utc_str = ev.get("ts_utc", "")

            if is_released:
                cd_color = "#6b7280"; countdown = "selesai"
            else:
                mins = minutes_until(ts_utc_str) if ts_utc_str else None
                countdown = countdown_str(ts_utc_str) if ts_utc_str else "–"
                cd_color = "#ef4444" if (mins is not None and mins <= 15) else ("#d97706" if (mins is not None and mins <= 60) else "#6b7280")

            col_time, col_impact, col_ccy, col_name, col_a, col_f, col_p = st.columns([1.6, 0.9, 0.7, 2.4, 1.1, 1.1, 1.1])
            with col_time:
                st.markdown(f"<div style='font-weight:700;color:{cd_color};font-size:0.85rem;'>{countdown}</div>"
                            f"<div style='font-size:0.72rem;color:#9ca3af;'>{ts_wib_str} WIB</div>", unsafe_allow_html=True)
            with col_impact:
                st.markdown(_impact_badge(impact), unsafe_allow_html=True)
            with col_ccy:
                st.markdown(f"<span style='font-weight:700;font-size:0.85rem;'>{currency}</span>", unsafe_allow_html=True)
            with col_name:
                st.markdown(f"<div style='font-size:0.87rem;font-weight:600;'>{name}</div>", unsafe_allow_html=True)
                # Surprise tag: hanya untuk event yang actual-nya dari FRED + ada forecast
                pol = ev.get("surprise_polarity")
                if is_released and pol is not None and actual is not None and forecast is not None:
                    try:
                        delta = (float(actual) - float(forecast)) * float(pol)
                        if abs(delta) < 1e-9:
                            arrow, scol, lbl = "→", "#9ca3af", "in-line"
                        elif delta > 0:
                            arrow, scol, lbl = "▲", "#16a34a", f"{currency} bullish"
                        else:
                            arrow, scol, lbl = "▼", "#dc2626", f"{currency} bearish"
                        src = ev.get("actual_source", "")
                        st.markdown(
                            f"<div style='font-size:0.7rem;color:{scol};'>{arrow} surprise → {lbl}"
                            f"<span style='color:#6b7280;'> &nbsp;{src}</span></div>",
                            unsafe_allow_html=True,
                        )
                    except (TypeError, ValueError):
                        pass

            def _stat(label, val, color="#e5e7eb"):
                shown = val if val is not None else "–"
                return (f"<div style='text-align:center;'><div style='font-size:0.62rem;color:#6b7280;"
                        f"text-transform:uppercase;letter-spacing:0.04em;'>{label}</div>"
                        f"<div style='font-size:1.15rem;font-weight:800;color:{color};line-height:1.2;'>{shown}</div></div>")
            a_color = "#9ca3af"
            if actual is not None and forecast is not None:
                try: a_color = "#16a34a" if float(actual) >= float(forecast) else "#ef4444"
                except (TypeError, ValueError): a_color = "#e5e7eb"
            elif actual is not None:
                a_color = "#e5e7eb"
            with col_a: st.markdown(_stat("Actual", actual, a_color), unsafe_allow_html=True)
            with col_f: st.markdown(_stat("Forecast", forecast, "#93c5fd"), unsafe_allow_html=True)
            with col_p: st.markdown(_stat("Previous", previous, "#9ca3af"), unsafe_allow_html=True)
            st.markdown("<hr style='border:none;border-top:1px solid #1f2937;margin:6px 0;'>", unsafe_allow_html=True)
        except Exception as exc:
            st.caption(f"⚠ Gagal render event: {exc}")

    if mode == "🕓 Historis (2 mgg)":
        st.markdown(f"**🕓 Sudah Lewat ({len(released)}) — dengan hasil aktual**")
        if released:
            for ev in released: _render_row(ev, is_released=True)
        else:
            st.info("Tidak ada event historis sesuai filter (atau faireconomy tidak menyediakan).")
    else:
        is_weekly = (mode == "🗓️ Minggu Ini")
        st.markdown(f"**🔜 Akan Datang ({len(upcoming)})**")
        if upcoming:
            for ev in upcoming: _render_row(ev, is_released=False)
        else:
            st.info("Tidak ada upcoming event sesuai filter.")
        if released:
            if is_weekly:
                # FIX "Minggu Ini tidak muncul": di tengah minggu mayoritas event
                # sudah released → JANGAN kubur di expander tertutup. Tampilkan langsung.
                st.markdown(f"**✅ Sudah Lewat Minggu Ini ({len(released)}) — dengan aktual**")
                for ev in released: _render_row(ev, is_released=True)
            else:
                with st.expander(f"✅ Sudah lewat dalam window ini ({len(released)}) — dengan aktual"):
                    for ev in released: _render_row(ev, is_released=True)


def render_score_detail(
    asset_bias_map: dict[str, dict],
    news_delta: dict[str, float],
    cot_data: dict | None = None,
    retail_data: dict | None = None,
) -> None:
    """Tab Detail Skor: breakdown lengkap perhitungan bias satu currency terpilih."""

    st.subheader("🔬 Detail Perhitungan Skor")

    if not asset_bias_map:
        st.warning("Data skor kosong — periksa engine.")
        return

    assets = list(asset_bias_map.keys())
    sel = st.selectbox("Pilih mata uang / aset", options=assets, index=0, key="detail_asset")
    data = asset_bias_map.get(sel, {})
    drivers = data.get("drivers", {})
    baseline = data.get("bias_baseline", 0.0)
    nd = news_delta.get(sel, 0.0)
    overlaid = max(-100.0, min(100.0, baseline + nd))
    conf = data.get("confidence")
    freshness = data.get("freshness_cot")
    active = data.get("active_factors", [])

    # --- Ringkasan atas: angka besar ---
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(
            f"<div style='font-size:0.7rem;color:#6b7280;text-transform:uppercase;'>Baseline</div>"
            f"<div style='font-size:2rem;font-weight:800;color:{_bias_color(baseline)};'>"
            f"{'+' if baseline>=0 else ''}{baseline:.1f}</div>"
            f"{_label_html(bias_label(baseline))}",
            unsafe_allow_html=True)
    with c2:
        st.markdown(
            f"<div style='font-size:0.7rem;color:#6b7280;text-transform:uppercase;'>+ News Δ</div>"
            f"<div style='font-size:2rem;font-weight:800;color:{_bias_color(nd)};'>"
            f"{'+' if nd>=0 else ''}{nd:.1f}</div>"
            f"<div style='font-size:0.72rem;color:#9ca3af;'>cap ±30</div>",
            unsafe_allow_html=True)
    with c3:
        st.markdown(
            f"<div style='font-size:0.7rem;color:#6b7280;text-transform:uppercase;'>= Skor Final</div>"
            f"<div style='font-size:2rem;font-weight:800;color:{_bias_color(overlaid)};'>"
            f"{'+' if overlaid>=0 else ''}{overlaid:.1f}</div>"
            f"{_label_html(bias_label(overlaid))}",
            unsafe_allow_html=True)

    if conf is not None:
        st.markdown(f"**Confidence:** {_conf_bar(conf)} &nbsp; "
                    f"<span style='font-size:0.78rem;color:#9ca3af;'>(kesepakatan faktor aktif: "
                    f"{', '.join(active) if active else 'tidak ada'})</span>",
                    unsafe_allow_html=True)

    st.markdown("<hr style='border:none;border-top:1px solid #1f2937;margin:10px 0;'>", unsafe_allow_html=True)

    # --- Breakdown per faktor: score × weight efektif = kontribusi ---
    st.markdown("**Kontribusi per Faktor** &nbsp; <span style='font-size:0.75rem;color:#9ca3af;'>"
                "(baseline = Σ score×weight ÷ Σ weight aktif, lalu ×100)</span>", unsafe_allow_html=True)

    rows = []
    factor_names = {"R_hard": "R_hard (makro: rate diff + surprise)",
                    "C": "C (COT positioning)",
                    "D": "D (retail sentiment, kontrarian)",
                    "F": "F (ForexFactory surprise: actual vs forecast)"}
    for fkey in ["R_hard", "C", "D", "F"]:
        info = drivers.get(fkey, {})
        score = info.get("score", 0.0)
        w_eff = info.get("weight", 0.0)
        w_nom = info.get("weight_nominal", w_eff)
        detail = info.get("detail", "")
        contrib = score * w_eff
        is_active = abs(score) > 1e-9 and w_eff > 0
        rows.append({
            "Faktor": factor_names.get(fkey, fkey),
            "Score": f"{score:+.3f}",
            "Weight efektif": f"{w_eff:.3f}" + (f" (nom {w_nom:.2f})" if abs(w_eff-w_nom)>1e-6 else ""),
            "Kontribusi": f"{contrib:+.3f}",
            "Status": "✅ aktif" if is_active else "⚪ gate/0",
            "Penjelasan": detail,
        })
    import pandas as pd
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # --- Catatan freshness COT kalau relevan ---
    if freshness is not None and "C" in drivers:
        cnom = drivers["C"].get("weight_nominal", 0.25)
        ceff = drivers["C"].get("weight", 0.0)
        st.caption(
            f"❄️ **Freshness COT = {freshness:.3f}** → bobot C disesuaikan: "
            f"{cnom:.2f} × {freshness:.3f} = {ceff:.3f}. "
            f"(COT makin lama sejak snapshot Selasa → bobotnya makin kecil, bukan skornya.)"
        )

    # --- DUA SENTIMENT berdampingan: COT (lagging) + myfxbook (live) ---
    cot_slot = (cot_data or {}).get("cot", {}).get(sel, {}) if cot_data else {}
    dumb_net = cot_slot.get("dumb_net")
    if dumb_net is not None:
        dumb_idx = cot_slot.get("dumb_index")
        lean = "net-LONG" if dumb_net > 0 else ("net-SHORT" if dumb_net < 0 else "flat")
        idx_txt = f" · index {dumb_idx}/100" if dumb_idx is not None else ""
        div = cot_slot.get("smart_dumb_divergence")
        div_txt = (" &nbsp;<span style='color:#d97706;font-weight:700;'>⚠ divergence vs smart money "
                   "(setup kontrarian — display saja, belum di-score)</span>") if div else ""
        st.caption(
            f"🐑 **Dumb money — COT non-reportable (lagging, snapshot Selasa):** {sel} {lean} "
            f"({dumb_net:+,}){idx_txt}.{div_txt}",
            unsafe_allow_html=True,
        )
    # Retail live (myfxbook): pair yang melibatkan sel + long% (kontrarian feeds faktor D)
    retail_map = (retail_data or {}).get("retail", {}) if retail_data else {}
    rel_pairs = {p: v for p, v in retail_map.items()
                 if isinstance(p, str) and sel in p and sel not in ("XAU", "BTC", "ETH")}
    if rel_pairs:
        parts = []
        for p, v in sorted(rel_pairs.items()):
            lp = v.get("long_pct_agg") if isinstance(v, dict) else None
            if lp is not None:
                parts.append(f"{p} {lp:.0f}%L")
        if parts:
            st.caption("📊 **Retail live — myfxbook (feeds faktor D, kontrarian):** "
                       + " · ".join(parts) + " &nbsp;<span style='color:#6b7280;'>(% retail net-long; "
                       "ekstrem = sinyal fade)</span>", unsafe_allow_html=True)
    elif retail_data is not None and not retail_map:
        st.caption("📊 Retail live (myfxbook): kosong — set `MYFXBOOK_EMAIL`+`MYFXBOOK_PASSWORD` "
                   "di Secrets, atau IP Streamlit terblokir (cek status sumber).")
    with st.expander("📐 Rumus & cara baca"):
        _rumus_lines = [
            "- **Tiap faktor** menghasilkan *score* ∈ [−1, +1] (lihat kolom Penjelasan untuk asal angkanya).",
            "- **Weight efektif**: R_hard & D pakai bobot nominal; **C dikali freshness COT**.",
            "- **Kontribusi** = score × weight efektif.",
            "- **Baseline** = (Σ kontribusi faktor aktif) ÷ (Σ weight faktor aktif) × 100. Renormalisasi ini bikin faktor yang ter-*gate* (score 0) tidak menyeret hasil.",
            "- **Skor Final** = clamp(Baseline + News Δ, −100, +100).",
            "- **Confidence** = seberapa sepakat arah antar faktor aktif (bukan klaim akurasi).",
            "",
            "⚠️ Semua bobot = **placeholder** sampai backtest. Ini alat confluence, bukan sinyal arah.",
        ]
        st.markdown("\n".join(_rumus_lines))
        st.info("🤖 Groq aktif untuk **klasifikasi arah news** (toggle di sidebar): Groq mengukur "
                "arah+impact tiap cluster, engine deterministik yang hitung news_delta (cap ±30). "
                "Penjelasan naratif per-skor menyusul. Angka selalu dari engine, bukan Groq.")


# ===========================================================================
# SECTION 4e — FOOTER
# ===========================================================================

def render_footer() -> None:
    st.markdown(
        "<div class='footer-note'>"
        "⚠️ <b>Alat confluence, bukan sinyal arah. TA tetap primary. "
        "Bobot belum tervalidasi (placeholder).</b><br>"
        "Confidence = kesepakatan faktor, bukan klaim akurasi arah. "
        "Semua bobot = placeholder sampai forward-test & backtest selesai. "
        "Indices = ditunda v2."
        "</div>",
        unsafe_allow_html=True,
    )


# ===========================================================================
# FUNGSI UTAMA — DATA PIPELINE + LAYOUT
# ===========================================================================

def build_sources_status(
    prices: dict,
    macro: dict,
    cot: dict,
    retail: dict,
    news: dict,
    calendar: dict,
) -> dict[str, str]:
    """Bangun dict status per sumber: 'ok' | 'warn' | 'fail'."""
    status: dict[str, str] = {}

    # Prices
    px = prices.get("prices", {})
    ok_count = sum(1 for v in px.values() if v.get("last") is not None)
    if prices.get("_error"):
        status["prices"] = "fail"
    elif ok_count < len(px) * 0.5:
        status["prices"] = "warn"
    else:
        status["prices"] = "ok"

    # Macro / FRED
    macro_meta = macro.get("_meta", {})
    macro_failed = macro_meta.get("sources_failed", [])
    if macro.get("_error") or not macro.get("rates"):
        status["FRED"] = "fail"
    elif macro_failed:
        status["FRED"] = "warn"
    else:
        status["FRED"] = "ok"

    # COT — hijau kalau mayoritas aset dapat data; XAU missing itu normal (gold ada di
    # laporan Disaggregated, bukan TFF), jadi BUKAN alasan kuning.
    cot_meta = cot.get("_meta", {})
    cot_data = cot.get("cot", {})
    n_valid = sum(1 for v in cot_data.values() if v.get("net") is not None)
    if n_valid == 0:
        status["COT"] = "fail"          # benar-benar kosong
    elif cot_meta.get("stale") and n_valid < 3:
        status["COT"] = "warn"          # sedikit data + stale
    else:
        status["COT"] = "ok"            # ada data → hijau
        xau = cot_data.get("XAU", {})
        if xau.get("net") is None:
            status["_cot_note"] = "XAU N/A (gold ada di laporan Disaggregated, bukan TFF — wajar, tidak mempengaruhi 9 aset lain)"

    # Retail
    retail_ok = retail.get("sources_ok", [])
    retail_fail = retail.get("sources_failed", [])
    if retail.get("_error") or (not retail_ok and retail_fail):
        status["retail"] = "fail"
    elif retail_fail:
        status["retail"] = "warn"
    else:
        status["retail"] = "ok"

    # News RSS
    if news.get("_error") or not news.get("headlines"):
        status["news"] = "fail" if news.get("_error") else "warn"
    else:
        status["news"] = "ok"

    # Calendar
    if calendar.get("_error") and not calendar.get("events"):
        status["calendar"] = "fail"
    elif calendar.get("_error"):
        status["calendar"] = "warn"
    else:
        status["calendar"] = "ok"

    return status


def _get_pb():
    """Import parsebot_client defensif (collectors/ atau root). None kalau belum ada."""
    try:
        from collectors import parsebot_client as pb
        return pb
    except Exception:
        try:
            import parsebot_client as pb  # fallback root
            return pb
        except Exception:
            return None


_FOREX_CCY = {"EUR", "USD", "JPY", "GBP", "CHF", "AUD", "CAD", "NZD"}
_A1_INSTRUMENT_MAP = {  # nama A1 → (display, tag) untuk komoditas/crypto
    "GOLD": ("XAUUSD", "KOMODITAS"), "SILVER": ("XAGUSD", "KOMODITAS"),
    "BITCOIN": ("BTCUSD", "CRYPTO"), "Ethereum": ("ETHUSD", "CRYPTO"),
}


def _sentiment_card(rank: int, instrument: str, subtitle: str, tag: str,
                    long_pct: float, short_pct: float, mode: str = "fade",
                    extra: str = "") -> str:
    """Kartu sentiment. mode='fade' (retail→kontrarian) / 'follow' (COT smart→searah)."""
    lp = float(long_pct or 0.0)
    sp = float(short_pct if short_pct is not None else (100 - lp))
    crowd_long = lp >= 50
    # Bacaan: retail = lawan kerumunan; COT smart = ikut
    bull = (not crowd_long) if mode == "fade" else crowd_long
    if abs(lp - 50) < 10:
        reading, rc = "Netral", "#9ca3af"
    elif bull:
        reading, rc = "Cenderung bullish", "#22c55e"
    else:
        reading, rc = "Cenderung bearish", "#f97316"
    skew = int(round(abs(lp - 50) * 2))
    conv = "Tinggi" if skew >= 50 else ("Sedang" if skew >= 25 else "Rendah")
    lean = "long" if crowd_long else "short"
    label = "BACAAN KONTRARIAN" if mode == "fade" else "BACAAN SMART-MONEY"
    return (
        f"<div style='border:1px solid #2a2e39;border-radius:10px;padding:10px 14px;"
        f"margin-bottom:8px;background:#0e1117;'>"
        f"<div style='display:flex;justify-content:space-between;align-items:flex-start;'>"
        f"<div><span style='color:#6b7280;'>{rank}</span> "
        f"<b style='font-size:15px;'>{instrument}</b> "
        f"<span style='font-size:10px;color:#9ca3af;border:1px solid #2a2e39;border-radius:4px;"
        f"padding:1px 6px;margin-left:4px;'>{tag}</span>"
        f"<div style='font-size:11px;color:#6b7280;margin-top:2px;'>{subtitle}</div></div>"
        f"<div style='text-align:right;'><div style='font-size:9px;color:#6b7280;letter-spacing:1px;'>{label}</div>"
        f"<div style='color:{rc};font-weight:600;'>{reading}</div></div></div>"
        f"<div style='display:flex;justify-content:space-between;font-size:12px;color:#9ca3af;margin-top:8px;'>"
        f"<span>Long {lp:.0f}%</span><span>Short {sp:.0f}%</span></div>"
        f"<div style='height:7px;border-radius:4px;overflow:hidden;display:flex;margin-top:3px;'>"
        f"<div style='width:{lp}%;background:#22c55e;'></div>"
        f"<div style='width:{sp}%;background:#f97316;'></div></div>"
        f"<div style='font-size:11px;color:#6b7280;margin-top:6px;'>Kerumunan {lean} · "
        f"Kemiringan {skew} · Conviction: <b>{conv}</b>{extra}</div></div>"
    )


def _render_ff_calendar(ff: list[dict], ts: str, key_prefix: str = "ff") -> None:
    """Kalender FF (WIB) per-hari + filter ccy/impact + kolom Dampak→bias & Freshness."""
    from engine.ff_surprise import to_wib, score_event, _now_wib_naive
    released = [e for e in ff if str(e.get("actual", "")).strip()]
    st.caption(f"Ditarik: {ts} · {len(ff)} event minggu ini · {len(released)} sudah rilis · "
               "waktu = **WIB** (UTC+7).")
    all_ccy = sorted({(e.get("currency") or "").upper() for e in ff if e.get("currency")})
    fc1, fc2 = st.columns([3, 2])
    with fc1:
        sel_ccy = st.multiselect("Mata uang", all_ccy, default=all_ccy, key=f"{key_prefix}_ccy")
    with fc2:
        sel_imp = st.multiselect("Impact", ["high", "medium", "low"], default=["high", "medium"],
                                 key=f"{key_prefix}_imp")
    now_wib = _now_wib_naive()
    yr = now_wib.year

    # konversi tiap event ke WIB → regroup per tanggal WIB
    days: dict[str, list] = {}
    order: dict = {}
    for e in ff:
        if (e.get("currency") or "").upper() not in sel_ccy:
            continue
        if (e.get("impact") or "").lower() not in sel_imp:
            continue
        wib = to_wib(e.get("date", ""), e.get("time", ""), yr)
        if wib is not None:
            daykey = wib.strftime("%a %d %b")
            tdisp = wib.strftime("%H:%M")
            order.setdefault(daykey, wib.replace(hour=0, minute=0))
        else:
            daykey = (e.get("date") or "?") + " (sumber)"
            tdisp = e.get("time", "")
            order.setdefault(daykey, None)
        days.setdefault(daykey, []).append((tdisp, e))
    if not days:
        st.info("Tidak ada event sesuai filter.")
        return

    sorted_days = sorted(days.keys(), key=lambda k: (order.get(k) is None, order.get(k) or now_wib))
    _impc = {"high": "qf-imp-h", "medium": "qf-imp-m", "low": "qf-imp-l"}
    for day in sorted_days:
        st.markdown(f'<div class="qf-calday">{day} <span style="color:#5b6677;font-weight:500;'
                    f'font-size:0.72rem;">WIB</span></div>', unsafe_allow_html=True)
        body = ('<div class="qf-cal head"><span>Waktu</span><span>CCY</span><span>Impact</span>'
                '<span>Event</span><span>Act / Fc / Prev</span><span>Freshness</span>'
                '<span>Dampak→bias</span></div>')
        for tdisp, e in sorted(days[day], key=lambda te: te[0]):
            act = str(e.get("actual", "")).strip()
            fc = str(e.get("forecast", "")).strip()
            prev = str(e.get("previous", "")).strip()
            tag = ""
            sc = score_event(e, now_wib)        # poin per-event (None bila belum rilis / tak ada σ)
            if act and fc:
                try:
                    a = float(act.replace("%", "").replace("K", "").replace("M", "").replace("B", ""))
                    f = float(fc.replace("%", "").replace("K", "").replace("M", "").replace("B", ""))
                    tag = " 🟢" if a > f else (" 🔴" if a < f else " ⚪")
                except Exception:
                    tag = ""
            if sc is not None:
                dcls = _vcls(sc["points"])
                dampak = f'<span class="{dcls}">{sc["ccy"]} {sc["points"]:+.2f}</span>'
                ago = sc["days_ago"]
                ago_txt = "hari ini" if ago < 1 else f"{int(round(ago))} hr lalu"
                fresh = f'{sc["freshness"]:.2f} <span style="color:#5b6677;">({ago_txt})</span>'
            elif act:
                dampak = '<span class="qf-neu">— (tanpa σ)</span>'
                fresh = "—"
            else:
                dampak = '<span class="qf-neu">—</span>'
                fresh = "—"
            impc = _impc.get((e.get("impact") or "").lower(), "qf-imp-l")
            afp = f'{(act + tag) if act else "—"} / {fc or "—"} / {prev or "—"}'
            body += (
                f'<div class="qf-cal"><span>{tdisp}</span>'
                f'<span class="qf-pair" style="font-size:0.82rem;">{e.get("currency","")}</span>'
                f'<span class="{impc}">{(e.get("impact") or "").upper()}</span>'
                f'<span class="ev">{e.get("name","")}</span>'
                f'<span class="afp">{afp}</span>'
                f'<span>{fresh}</span><span>{dampak}</span></div>'
            )
        st.markdown(f'<div class="qf-wrap">{body}</div>', unsafe_allow_html=True)
    return


def render_retail_tab() -> None:
    """Tab Retail sentiment (A1) — kartu per instrumen forex/emas/crypto, kontrarian."""
    pb = _get_pb()
    st.subheader("📊 Retail Sentiment — A1 EdgeFinder")
    if pb is None:
        st.error("Modul `parsebot_client.py` belum ada di repo (collectors/).")
        return
    has_key = pb._api_key() is not None
    st.caption("Retail ritel = sumber **kontrarian**: kerumunan ekstrem di satu sisi → fade. "
               "Display saja, bukan skor bias. Klik untuk tarik (cache 1 jam, klik ulang = 0 kredit).")
    if st.button("🔄 Tarik / refresh retail", key="retail_tab_fetch", disabled=not has_key):
        try:
            with st.spinner("…"):
                st.session_state["a1_retail_data"] = pb.parse_a1_retail(
                    pb.fetch(pb.SCRAPERS["a1edge"], "get_retail_sentiment", {}, ttl=3_600))
                st.session_state["a1_retail_ts"] = _now_wib_str()
        except Exception as exc:
            st.error(f"retail: {exc}")
    if not has_key:
        st.warning("Set `PARSE_API_KEY` di Secrets dulu.")
    data = st.session_state.get("a1_retail_data")
    if not data:
        st.info("Belum ada data. Klik tombol di atas (atau tarik di tab Data Feeds).")
        return
    per_pair = data.get("per_pair", {})
    st.caption(f"Update: {st.session_state.get('a1_retail_ts','-')}")
    # bangun list instrumen: forex pair + emas + crypto
    items = []
    for name, v in per_pair.items():
        lp = v.get("long_pct")
        if lp is None:
            continue
        if len(name) == 6 and name[:3] in _FOREX_CCY and name[3:] in _FOREX_CCY:
            items.append((name, name[:3] + "/" + name[3:], "FOREX", lp, v.get("short_pct")))
        elif name in _A1_INSTRUMENT_MAP:
            disp, tag = _A1_INSTRUMENT_MAP[name]
            items.append((disp, name, tag, lp, v.get("short_pct")))
    # urut by conviction (|long-50|) desc lalu kelompokkan per kelas
    items.sort(key=lambda x: abs((x[3] or 50) - 50), reverse=True)
    _order = [("FOREX", "Currency"), ("KOMODITAS", "Komoditas"),
              ("CRYPTO", "Crypto"), ("INDEX", "Index")]
    rank = 0
    for tagkey, gname in _order:
        grp = [it for it in items if it[2] == tagkey]
        if not grp:
            continue
        st.markdown(f'<div class="qf-grp">{gname}</div>', unsafe_allow_html=True)
        for disp, sub, tag, lp, sp in grp:
            rank += 1
            st.markdown(_sentiment_card(rank, disp, sub, tag, lp, sp, mode="fade"),
                        unsafe_allow_html=True)
    # sisa tag lain (jaga-jaga)
    shown = {t for t, _ in _order}
    rest = [it for it in items if it[2] not in shown]
    if rest:
        st.markdown('<div class="qf-grp">Lainnya</div>', unsafe_allow_html=True)
        for disp, sub, tag, lp, sp in rest:
            rank += 1
            st.markdown(_sentiment_card(rank, disp, sub, tag, lp, sp, mode="fade"),
                        unsafe_allow_html=True)


_COT_CANON_ALIAS = {"XAU": "GOLD", "GOLD": "GOLD", "BITCOIN": "BTC", "ETHEREUM": "ETH",
                    "USOIL": "USOIL", "USOĪL": "USOIL"}
_COT_GROUPS = [
    ("Currency", {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "ZAR"}),
    ("Komoditas", {"GOLD", "SILVER", "PLATINUM", "COPPER", "USOIL"}),
    ("Crypto", {"BTC", "ETH"}),
    ("Index / Rates", {"SPX", "NASDAQ", "DOW", "RUSSELL", "NIKKEI", "US10T"}),
]


def _cot_canon(k: str) -> str:
    u = str(k).upper()
    return _COT_CANON_ALIAS.get(u, u)


def _cot_reading(slot: dict | None):
    """(label, warna, kategori) — kategori: 'long'/'short'/'neutral'/None.
    Arah dibaca dari long% (porsi long kotor = metrik yang SEBANDING di CFTC & A1),
    zona netral ±10. net/net_pct hanya fallback bila long% tak ada."""
    if not slot:
        return ("— tak ada", "#5b6677", None)
    lp = slot.get("long_pct")
    if lp is not None:
        if abs(lp - 50) < 10:
            return ("Netral", "#9aa6b6", "neutral")
        return ("Net-long", "#22c55e", "long") if lp >= 50 else ("Net-short", "#f97316", "short")
    raw = slot.get("net") if slot.get("net") is not None else slot.get("net_pct")
    if raw is None:
        return ("— tak ada", "#5b6677", None)
    if raw > 0:
        return ("Net-long", "#22c55e", "long")
    if raw < 0:
        return ("Net-short", "#f97316", "short")
    return ("Netral", "#9aa6b6", "neutral")


def _cot_side_html(src: str, slot: dict | None) -> str:
    read, rc, _cat = _cot_reading(slot)
    if not slot or _cat is None:
        return f'<div><div class="qf-side-h">{src}</div><div class="qf-neu">— tak ada</div></div>'
    lp = slot.get("long_pct")
    sp = slot.get("short_pct")
    metric = ""
    if slot.get("net") is not None:
        metric = f"net {slot['net']:+,}"
        if slot.get("cot_index") is not None:
            metric += f" · idx {slot['cot_index']:.0f}"
    elif slot.get("net_pct") is not None:
        metric = f"net {slot['net_pct']:+.2f}%"
    bar = ""
    if lp is not None:
        sp = sp if sp is not None else (100 - lp)
        bar = (f'<div class="qf-lsbar"><div style="width:{lp}%;background:#22c55e;"></div>'
               f'<div style="width:{sp}%;background:#f97316;"></div></div>'
               f'<div class="qf-ls">L {lp:.0f}% · S {sp:.0f}%</div>')
    return (f'<div><div class="qf-side-h">{src}</div>'
            f'<div style="color:{rc};font-weight:700;font-size:0.82rem;">{read}</div>'
            f'<div class="qf-side-m">{metric or "—"}</div>{bar}</div>')


def render_cot_tab(cot_data: dict | None = None) -> None:
    """Tab COT — CFTC (gratis) & A1 (validasi) tersanding per aset + indikator searah."""
    st.subheader("🏛️ COT — CFTC vs A1 (smart money)")
    st.caption("**CFTC TFF Leveraged Funds** = sumber utama (gratis, masuk skor faktor C). "
               "**A1** = CFTC Legacy non-commercial (kategori beda) → **cek arah saja, tidak masuk skor**. "
               "Non-commercial dibaca **searah** (ikut), bukan fade. Mingguan & lagging: snapshot Selasa, "
               "rilis Jumat ~15:30 ET. Generate A1 di bawah untuk menyandingkan.")

    with st.expander("❓ Cara baca COT — CFTC vs A1, rilis, freshness & dampak ke trade"):
        st.markdown(
            "**Angka CFTC (kiri):**\n"
            "- `net` = kontrak **long − short** (Leveraged Funds). Positif = net-long, negatif = net-short. "
            "Satuannya **kontrak**, bukan persen.\n"
            "- `idx` = **COT Index 0–100** = posisi `net` sekarang relatif rentang **3 tahun**. "
            "`>80` = mendekati paling long dalam 3 thn (crowded long), `<20` = crowded short, `~50` = tengah.\n"
            "- `L%/S%` = porsi long vs short **kotor** (long/(long+short)).\n\n"
            "**Angka A1 (kanan):**\n"
            "- `L%/S%` = porsi long/short non-commercial.\n"
            "- `net%` = metrik net versi A1 — **basis beda**, kadang tak konsisten dengan L%-nya sendiri "
            "(mis. 66% long tapi net% −4). Karena itu **arah dibaca dari L% (bukan net%)**.\n\n"
            "**Cara membandingkan:** angka `net` (kontrak) dan `net%` **tidak** disamakan/dipersentasikan — "
            "skalanya beda. Yang dibandingkan **ARAH** lewat L% vs 50 (zona netral ±10). "
            "**● SEARAH** = dua kelompok trader sepakat → konviksi naik. **▲ BEDA** = divergensi → hati-hati. "
            "`—` = salah satu netral/tak ada.\n\n"
            "**Kapan rilis & apa bedanya:**\n"
            "- **CFTC**: snapshot **Selasa**, rilis **Jumat ~15:30 ET**. Engine pakai **TFF Leveraged Funds** "
            "(klasifikasi modern CFTC, proxy hedge fund/CTA) → ini **faktor C**.\n"
            "- **A1**: diturunkan dari laporan CFTC yang **SAMA**, tapi kategori **Legacy Non-Commercial** "
            "(klasifikasi lama, lebih luas). Jadi **A1 TIDAK lebih fresh** — sama-sama Selasa/Jumat. "
            "Bedanya cuma **kategori trader**, bukan waktu.\n\n"
            "**Freshness & lagging — dampak ke trade-mu:**\n"
            "- COT itu **lambat**: posisi mingguan, lag ~3 hari saat rilis, dan makin basi sampai Selasa berikutnya. "
            "**Bukan sinyal entry intraday** — ini konteks **swing/positioning & sizing**.\n"
            "- `idx` ekstrem (`>80`/`<20`) = posisi **crowded** → risiko reversal/squeeze, tapi **timing jelek** "
            "(bisa ekstrem berminggu-minggu). Jangan entry hanya karena COT.\n"
            "- Pakai begini: **CFTC & A1 SEARAH + idx tidak ekstrem** → dukungan untuk bias searah. "
            "**BEDA** atau **idx ekstrem** → kurangi keyakinan/size, tunggu konfirmasi harga.\n\n"
            "**Mana yang dipakai?** **CFTC (TFF Leveraged Funds)** sebagai utama (masuk skor). "
            "**A1 hanya validasi arah** — tidak masuk skor (kategori beda, tak bisa digabung rata-rata)."
        )

    cftc = (cot_data or {}).get("cot", {}) if cot_data else {}

    pb = _get_pb()
    has_key = pb is not None and pb._api_key() is not None
    bcol1, _ = st.columns([2, 5])
    with bcol1:
        if st.button("🔄 Generate / refresh A1 COT", key="cot_a1_validate", disabled=not has_key,
                     use_container_width=True):
            try:
                with st.spinner("…"):
                    st.session_state["a1_cot_data"] = pb.parse_a1_cot(
                        pb.fetch(pb.SCRAPERS["a1edge"], "get_cot_report", {}, ttl=86_400))
                    st.session_state["a1_cot_ts"] = _now_wib_str()
            except Exception as exc:
                st.error(f"A1 COT: {exc}")
    if not has_key:
        st.caption("⚠️ Set `PARSE_API_KEY` di Secrets untuk generate A1.")
    a1 = st.session_state.get("a1_cot_data") or {}

    cftc_c = {_cot_canon(k): v for k, v in cftc.items()
              if isinstance(v, dict) and not v.get("_error")}
    a1_c = {_cot_canon(k): v for k, v in a1.items() if isinstance(v, dict)}

    if not cftc_c and not a1_c:
        st.info("CFTC belum termuat (cek badge COT) & A1 belum di-generate. "
                "Kalau CFTC kosong padahal harusnya ada → re-upload `collectors/cot.py` (versi terbaru "
                "menambah long%/short% untuk kartu).")
        return
    if cftc_c and not any(v.get("long_pct") is not None for v in cftc_c.values()):
        st.caption("ℹ️ CFTC ada arah (net) tapi tanpa long%/short% → **re-upload `collectors/cot.py`** "
                   "untuk bar long/short. Arah & perbandingan tetap jalan.")

    ts_a1 = st.session_state.get("a1_cot_ts")
    if a1_c and ts_a1:
        st.caption(f"A1 update: {ts_a1}")

    agree = total = 0
    all_canon = set(cftc_c) | set(a1_c)
    for gname, gset in _COT_GROUPS:
        members = [c for c in all_canon if c in gset]
        if not members:
            continue
        members.sort()
        st.markdown(f'<div class="qf-grp">{gname}</div>', unsafe_allow_html=True)
        body = ('<div class="qf-cmp head"><span>Aset</span><span>CFTC (utama)</span>'
                '<span>Arah</span><span>A1 (validasi)</span></div>')
        for c in members:
            cs = cftc_c.get(c)
            asl = a1_c.get(c)
            ccat = _cot_reading(cs)[2]
            acat = _cot_reading(asl)[2]
            if ccat in ("long", "short") and acat in ("long", "short"):
                total += 1
                same = ccat == acat
                agree += 1 if same else 0
                align = ('<span class="qf-pos qf-align">● SEARAH</span>' if same
                         else '<span class="qf-neg qf-align">▲ BEDA</span>')
            else:
                align = '<span class="qf-neu qf-align">—</span>'
            body += (
                f'<div class="qf-cmp"><span class="qf-cmpname">{c}</span>'
                f'{_cot_side_html("CFTC", cs)}{align}{_cot_side_html("A1", asl)}</div>'
            )
        st.markdown(f'<div class="qf-wrap">{body}</div>', unsafe_allow_html=True)

    if total:
        pct = round(100 * agree / total)
        verd = "mantap, sumber sepakat." if pct >= 80 else (
            "ada divergensi — cek event/kategori." if pct < 60 else "mayoritas sepakat.")
        st.markdown(f"**Searah CFTC ↔ A1: {agree}/{total} ({pct}%)** — {verd} "
                    "_Arah saja; A1 tidak memengaruhi skor (skor pakai CFTC)._")


def render_risk_events_ff(calendar_data: dict) -> None:
    """Risk Events berbasis ForexFactory (acuan + actual) + faktor F; baseline faireconomy di expander."""
    pb = _get_pb()
    has_key = pb is not None and pb._api_key() is not None
    st.subheader("⏰ Risk Events — ForexFactory")
    st.caption("Acuan event + **actual** dari ForexFactory (via parse.bot). Surprise rilis → faktor **F** "
               "di Currency Power (z×polaritas×impact×freshness). Tarik = 1 call (cache 6 jam).")
    if st.button("🔄 Tarik / refresh kalender FF (minggu ini)", key="re_ff_fetch", disabled=not has_key):
        try:
            with st.spinner("…"):
                st.session_state["pb_ff_data"] = pb.parse_ff_calendar(
                    pb.fetch(pb.SCRAPERS["forexfactory"], "get_calendar", {}, ttl=21_600))
                st.session_state["pb_ff_ts"] = _now_wib_str()
        except Exception as exc:
            st.error(f"FF: {exc}")
    if not has_key:
        st.warning("Set `PARSE_API_KEY` di Secrets untuk tarik FF.")
    ff = st.session_state.get("pb_ff_data")
    if ff:
        _render_ff_calendar(ff, st.session_state.get("pb_ff_ts", "-"), key_prefix="ffre")
        try:
            from engine.ff_surprise import compute_ff_surprise
            from datetime import datetime, timezone, timedelta
            fs = compute_ff_surprise(ff, datetime.now(timezone(timedelta(hours=7))))
            if fs:
                st.success("**Faktor F aktif (surprise → bias):** " + " · ".join(
                    f"{c} {v['score']:+.2f}" for c, v in
                    sorted(fs.items(), key=lambda kv: -abs(kv[1]["score"]))))
        except Exception:
            pass
    else:
        st.info("Belum tarik FF minggu ini. Klik tombol di atas untuk lihat actual + aktifkan faktor F. "
                "Baseline gratis (faireconomy) tetap ada di bawah.")
    with st.expander("📋 Baseline gratis (faireconomy) + input actual manual"):
        render_key_risk_events(calendar_data)


def render_data_feeds() -> None:
    """Panel click-to-run parse.bot — tarik data hanya saat tombol diklik (hemat kredit)."""
    try:
        from collectors import parsebot_client as pb
    except Exception:
        try:
            import parsebot_client as pb  # fallback: file ditaruh di root repo
        except Exception:
            st.subheader("🛰️ Data Feeds — parse.bot")
            st.error(
                "Modul `parsebot_client.py` belum ada di repo. Upload file ini ke GitHub "
                "di path **`collectors/parsebot_client.py`** (bukan root), commit, lalu reboot app. "
                "Tanpa file ini, tab Data Feeds tidak bisa jalan."
            )
            return

    st.subheader("🛰️ Data Feeds — parse.bot (click-to-run)")
    has_key = pb._api_key() is not None
    calls = pb.calls_this_session()
    c1, c2 = st.columns([3, 1])
    with c1:
        st.caption(
            "Data **tidak** ditarik otomatis — hanya saat kamu klik tombol, jadi kredit "
            "tidak habis liar. Klik ulang dalam masa cache = **0 kredit** (serve dari cache)."
        )
    with c2:
        st.metric("Call sesi ini", calls)
    if not has_key:
        st.warning("Set `PARSE_API_KEY` di Streamlit Secrets dulu untuk pakai panel ini.")
    else:
        _k = pb._api_key() or ""
        _looks_placeholder = ("$" in _k) or (_k.strip() != _k) or (len(_k) < 12)
        _msg = f"🔑 key terbaca: {len(_k)} karakter · …{_k[-4:] if len(_k) >= 4 else _k}"
        if _looks_placeholder:
            st.error(_msg + " — ⚠ mencurigakan (placeholder `$...`, ada spasi, atau terlalu pendek). "
                     "Ini sebab 401-nya. Paste key ASLI dari parse.bot/settings tanpa kutip/spasi.")
        else:
            st.caption(_msg)
    st.caption(
        "💳 Budget free tier ~200 kredit/bln (verifikasi di dashboard-mu; buat scraper ~75, "
        "edit ~50; call situs anti-bot BISA >1 kredit). Rencana hemat di bawah."
    )
    st.divider()

    # --- 1) ForexFactory: kalender minggu ini (actual/forecast/previous) ---
    st.markdown("**📅 Kalender ForexFactory (minggu ini)** — 1 call = seluruh minggu. Cache 6 jam.")
    if st.button("Tarik kalender minggu ini", key="pb_ff", disabled=not has_key):
        try:
            with st.spinner("Mengambil kalender FF…"):
                resp = pb.fetch(pb.SCRAPERS["forexfactory"], "get_calendar", {}, ttl=21_600)
            st.session_state["pb_ff_data"] = pb.parse_ff_calendar(resp)
            st.session_state["pb_ff_ts"] = _now_wib_str()
        except Exception as exc:
            st.error(f"FF gagal: {exc}")
    ff = st.session_state.get("pb_ff_data")
    if ff:
        _render_ff_calendar(ff, st.session_state.get("pb_ff_ts", "-"))

    st.divider()
    # --- 2) myfxbook: suku bunga bank sentral (untuk benerin rate_diff mayor) ---
    st.markdown("**🏦 Suku bunga bank sentral (myfxbook)** — jarang berubah. Cache 24 jam.")
    if st.button("Tarik suku bunga", key="pb_rates", disabled=not has_key):
        try:
            with st.spinner("Mengambil suku bunga…"):
                resp = pb.fetch(pb.SCRAPERS["myfxbook"], "get_interest_rates", {}, ttl=86_400)
            st.session_state["pb_rates_data"] = pb.parse_myfxbook_rates(resp)
            st.session_state["pb_rates_ts"] = _now_wib_str()
        except Exception as exc:
            st.error(f"Rates gagal: {exc}")
    rates = st.session_state.get("pb_rates_data")
    if rates:
        st.caption(f"Ditarik: {st.session_state.get('pb_rates_ts','-')} · {len(rates)} bank")
        st.dataframe(
            [{"Bank": r["bank"], "Negara": r["country"], "Rate": r["current_rate"],
              "Sebelum": r["previous_rate"], "Δ": r["change"], "Rapat terakhir": r["last_meeting"]}
             for r in rates],
            use_container_width=True, hide_index=True,
        )

    st.divider()
    # --- 3) A1 EdgeFinder (scraper custom) — sumber utama sentiment/COT/strength ---
    st.markdown("**🛰️ A1 EdgeFinder** — retail sentiment, COT, currency strength. Cache 1-6 jam.")
    a1 = pb.SCRAPERS["a1edge"]
    cols = st.columns(4)
    with cols[0]:
        if st.button("Retail sentiment", key="a1_retail", disabled=not has_key):
            try:
                with st.spinner("…"):
                    st.session_state["a1_retail_data"] = pb.parse_a1_retail(
                        pb.fetch(a1, "get_retail_sentiment", {}, ttl=3_600))
                    st.session_state["a1_retail_ts"] = _now_wib_str()
            except Exception as exc:
                st.error(f"retail: {exc}")
    with cols[1]:
        if st.button("COT (smart)", key="a1_cot", disabled=not has_key):
            try:
                with st.spinner("…"):
                    st.session_state["a1_cot_data"] = pb.parse_a1_cot(
                        pb.fetch(a1, "get_cot_report", {}, ttl=86_400))
                    st.session_state["a1_cot_ts"] = _now_wib_str()
            except Exception as exc:
                st.error(f"cot: {exc}")
    with cols[2]:
        if st.button("Currency strength", key="a1_heat", disabled=not has_key):
            try:
                with st.spinner("…"):
                    st.session_state["a1_heat_data"] = pb.parse_a1_strength(
                        pb.fetch(a1, "get_currency_heatmap", {}, ttl=3_600))
                    st.session_state["a1_heat_ts"] = _now_wib_str()
            except Exception as exc:
                st.error(f"heatmap: {exc}")
    with cols[3]:
        if st.button("Rates (⚠cek bug)", key="a1_rates", disabled=not has_key):
            try:
                with st.spinner("…"):
                    rr = pb.fetch(a1, "get_interest_rates", {}, ttl=86_400)
                    ii = pb.fetch(a1, "get_inflation_data", {}, ttl=86_400)
                    st.session_state["a1_rates_data"] = pb.parse_a1_rates(rr)
                    st.session_state["a1_rates_dupe"] = pb.rates_look_like_cpi(rr, ii)
                    st.session_state["a1_rates_ts"] = _now_wib_str()
            except Exception as exc:
                st.error(f"rates: {exc}")

    rd = st.session_state.get("a1_retail_data")
    if rd and rd.get("per_currency"):
        st.caption(f"Retail per mata uang · {st.session_state.get('a1_retail_ts','-')} "
                   "— kita pakai **long% mentah** (engine hitung kontrarian); 'signal' = display saja.")
        st.dataframe(
            [{"CCY": c, "Long %": v["long_pct"], "Short %": v["short_pct"],
              "A1 signal": v["signal"]} for c, v in rd["per_currency"].items()],
            use_container_width=True, hide_index=True)
    ct = st.session_state.get("a1_cot_data")
    if ct:
        st.caption(f"COT non-commercial (smart money) · {st.session_state.get('a1_cot_ts','-')} "
                   "— ⚠ overlap dgn collector CFTC; pilih satu, jangan double-count.")
        st.dataframe(
            [{"Asset": a, "Long %": v["long_pct"], "Short %": v["short_pct"], "Net %": v["net_pct"]}
             for a, v in ct.items()], use_container_width=True, hide_index=True)
    ht = st.session_state.get("a1_heat_data")
    if ht:
        st.caption(f"Currency strength (Δ% harga 1 hari) · {st.session_state.get('a1_heat_ts','-')} "
                   "— **lensa price/TA terpisah, BUKAN faktor bias**. Untuk divergensi bias-vs-harga.")
        st.dataframe(
            [{"CCY": c, "Strength (avg Δ%)": s} for c, s in
             sorted(ht.items(), key=lambda kv: kv[1], reverse=True)],
            use_container_width=True, hide_index=True)
    ra = st.session_state.get("a1_rates_data")
    if ra:
        if st.session_state.get("a1_rates_dupe"):
            st.error("🚨 get_interest_rates = duplikat CPI (bug scraper). JANGAN pakai untuk "
                     "rate_diff — akan korupsi R_hard. Perbaiki endpoint di parse.bot dulu.")
        st.dataframe(
            [{"CCY": r["currency"], "Rate?": r["current_rate"], "Prev?": r["previous_rate"],
              "Bank": r["bank"]} for r in ra], use_container_width=True, hide_index=True)


def _now_wib_str() -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M WIB")


def _a1_retail_to_scores(per_ccy: dict) -> dict[str, dict]:
    """A1 retail per-currency long% → skor D kontrarian [-1,1] (gating ekstrem 70/30)."""
    EXTREME = 70
    out: dict[str, dict] = {}
    for ccy, v in (per_ccy or {}).items():
        lp = v.get("long_pct") if isinstance(v, dict) else None
        if lp is None:
            continue
        lp = float(lp)
        if lp >= EXTREME:
            s = -min(1.0, (lp - EXTREME) / (100 - EXTREME))   # crowd long → fade short
            tag = "fade→bearish"
        elif lp <= (100 - EXTREME):
            s = min(1.0, ((100 - EXTREME) - lp) / (100 - EXTREME))  # crowd short → fade long
            tag = "fade→bullish"
        else:
            s, tag = 0.0, "netral (tak ekstrem)"
        out[ccy] = {"score": round(s, 4), "detail": f"A1 retail {lp:.0f}% long → {tag}"}
    return out


def main() -> None:
    """Entry point Streamlit — satu rerun = satu siklus data pipeline + display."""

    # -----------------------------------------------------------------------
    # STEP 1 — Placeholder header (timestamp dulu, sumber diisi nanti)
    # -----------------------------------------------------------------------
    header_placeholder = st.empty()

    # Groq toggle (sidebar) — DEFAULT OFF → refresh normal tak pernah panggil Groq
    # (nol risiko kuota / crash). ON = klasifikasi arah news via Groq (engine tetap hitung).
    with st.sidebar:
        st.markdown("### 🤖 Groq AI")
        _has_key = bool(_groq_key())
        use_groq = st.toggle(
            "Klasifikasi arah news (Groq)",
            value=False,
            disabled=not _has_key,
            help="Groq MENGUKUR arah news (nuansa yang keyword lewatkan, mis. 'BoJ should "
                 "slow bond buying'=hawkish JPY). Engine tetap menghitung poin. "
                 "Maks ~12 cluster/refresh, di-cache 1 jam (hemat kuota free-tier).",
        )
        if not _has_key:
            st.caption("⚠️ GROQ_API_KEY belum ada di Secrets — toggle nonaktif.")
        elif use_groq:
            st.caption("Aktif: arah dari Groq, fallback keyword bila limit/down.")
        else:
            st.caption("Nonaktif: arah dari keyword (default).")

    # -----------------------------------------------------------------------
    # STEP 2 — Fetch collectors (semua cached TTL)
    # -----------------------------------------------------------------------
    with st.spinner("Memuat data pasar…"):
        prices_data = cached_get_prices()

        # Calendar dulu — diperlukan oleh macro untuk surprises
        calendar_data = cached_get_calendar()

        # SURPRISE ENRICHMENT (Modul A):
        #   faireconomy TIDAK mengirim `actual` (hanya forecast/previous). Actual masuk
        #   dari: Eurostat (EUR/HICP), manual/Groq-vision (di bawah), dan untuk faktor F
        #   dari scrape ForexFactory. engine/sigma_table.py mengisi historical_std +
        #   surprise_polarity untuk indikator high-impact yang dikenal (σ = SEED
        #   PLACEHOLDER → ganti dgn σ terukur dari histori + backtest).
        #   Keanggotaan tabel = gate scoring; indikator tak dikenal tetap display-only.
        import json

        # MODUL A #1 — Actual real-time via API resmi Eurostat (EUR/HICP).
        # faireconomy TERBUKTI tak mengirim actual → diambil dari penerbit (Eurostat).
        # apply_eu_actuals men-set actual+actual_source dgn ALIGNMENT GUARD (previous
        # seri ≈ previous kalender); kalau tak align → tolak (tak ada actual palsu).
        _eu_actuals = cached_get_eu_actuals()
        _eu_diag = apply_eu_actuals(calendar_data.get("events", []), _eu_actuals)
        calendar_data["_eu_diag"] = _eu_diag

        # MODUL A #2 — Actual MANUAL (input user / hasil vision yang dikonfirmasi).
        # Prioritas tertinggi → menimpa Eurostat. Manusia = verifikator.
        _manual_map = st.session_state.get("manual_actuals", {})
        _manual_diag = apply_manual_actuals(calendar_data.get("events", []), _manual_map)
        calendar_data["_manual_diag"] = _manual_diag

        _surprise_diag = enrich_surprise_fields(calendar_data.get("events", []))
        calendar_data["_surprise_diag"] = _surprise_diag

        # Released events → surprises untuk macro. Lolos hanya bila actual ADA
        # (dari faireconomy) DAN σ ter-set (dikenal sigma_table). build_surprises
        # → z=(actual−forecast)/σ ×polarity → R_hard (decay 2 hari), tertelusur di Detail Skor.
        released_events = [
            e for e in calendar_data.get("events", [])
            if e.get("status") == "released"
               and e.get("actual") is not None
               and e.get("historical_std") is not None
        ]
        cal_json = json.dumps(released_events)
        macro_data = cached_get_macro(cal_json)

        cot_data = cached_get_cot()
        retail_data = cached_get_retail()
        news_data = cached_get_news()

    # -----------------------------------------------------------------------
    # STEP 3 — Engine
    # -----------------------------------------------------------------------
    # Toggle faktor Currency Power (engine v2) — OFF = faktor dianggap netral
    with st.sidebar:
        st.markdown("### ⚙️ Faktor Currency Power")
        st.caption("Matikan faktor yang dirasa kurang akurat → dianggap netral (keluar dari skor).")
        en_rate = st.checkbox("Rate differential (R_hard)", value=True, key="en_rate")
        en_cot = st.checkbox("COT — CFTC smart money (C)", value=True, key="en_cot")
        en_ret = st.checkbox("Retail sentiment — A1 kontrarian (D)", value=True, key="en_ret")
        en_ff = st.checkbox("FF surprise — actual vs forecast (F)", value=True, key="en_ff")
        st.caption("News overlay = delta aditif di atas baseline (beda dari 4 faktor di atas).")
        show_overlay = st.checkbox(
            "★ News overlay ke bias (±30)", value=False, key="en_news",
            help="bias_score = baseline + news_delta (cap ±30). Off = baseline murni.")
    enabled = {f for f, on in [("R_hard", en_rate), ("C", en_cot), ("D", en_ret), ("F", en_ff)] if on}

    # Faktor F (surprise). FIX double-count: surprise tidak lagi di R_hard (kini carry murni).
    # PRIORITAS SUMBER ACTUAL:
    #   PRIMARY  = scrape ForexFactory (pb_ff_data) — faireconomy TIDAK mengirim actual
    #              (hanya forecast/previous), makanya FF di-scrape untuk dapat actual.
    #   FALLBACK = actual dari kalender (Eurostat EUR / manual / Groq vision) yang sudah
    #              masuk released_events — dipakai HANYA bila FF belum di-scrape.
    # Either/or (bukan gabung) → tak ada double-count antar dua sumber di dalam F.
    ff_scores: dict[str, dict] = {}
    retail_override: dict[str, dict] = {}
    try:
        from engine.ff_surprise import (
            compute_ff_surprise,
            compute_ff_surprise_from_calendar,
        )
        if st.session_state.get("pb_ff_data"):
            from datetime import datetime, timezone, timedelta
            _wib_now = datetime.now(timezone(timedelta(hours=7)))
            ff_scores = compute_ff_surprise(st.session_state["pb_ff_data"], _wib_now)
        if not ff_scores:
            ff_scores = compute_ff_surprise_from_calendar(released_events)
    except Exception as _ff_exc:
        logger.warning("ff_scores build gagal: %s", _ff_exc)
        ff_scores = {}
    _a1ret = (st.session_state.get("a1_retail_data") or {}).get("per_currency", {})
    if _a1ret:
        retail_override = _a1_retail_to_scores(_a1ret)

    with st.spinner("Menghitung bias…"):
        # 3a. Compute all assets (baseline)
        try:
            asset_bias_map = compute_all_assets(
                macro=macro_data,
                cot=cot_data,
                retail=retail_data,
                prices=prices_data,
                ff_scores=ff_scores,
                retail_override=retail_override,
                enabled=enabled,
            )
        except Exception as exc:
            st.error(f"compute_all_assets() gagal: {exc}")
            asset_bias_map = {}

        # 3b. Compute confidence per aset
        for asset, adata in asset_bias_map.items():
            drivers = adata.get("drivers", {})
            # Retail agreement untuk faktor D
            retail_agreement_val: float | None = None
            try:
                _ASSET_TO_PAIR_LOOKUP = {
                    "EUR": "EURUSD", "GBP": "GBPUSD", "JPY": "USDJPY",
                    "AUD": "AUDUSD", "NZD": "NZDUSD", "CAD": "USDCAD",
                    "CHF": "USDCHF", "USD": "EURUSD",
                    "XAU": "XAUUSD", "BTC": "BTCUSD", "ETH": "ETHUSD",
                }
                pair_key = _ASSET_TO_PAIR_LOOKUP.get(asset)
                if pair_key:
                    pair_retail = retail_data.get("retail", {}).get(pair_key, {})
                    retail_agreement_val = pair_retail.get("agreement")
            except Exception:
                pass

            try:
                conf = compute_confidence(
                    driver_dict=drivers,
                    retail_agreement=retail_agreement_val,
                )
            except Exception as exc:
                logger.warning("compute_confidence[%s] gagal: %s", asset, exc)
                conf = 0.0
            adata["confidence"] = conf

        # 3c. Compute pairs (baseline)
        try:
            pair_bias_map = compute_pairs(asset_bias_map)
        except Exception as exc:
            st.error(f"compute_pairs() gagal: {exc}")
            pair_bias_map = {}

        # 3d. News delta (cached — mahal)
        try:
            headlines = news_data.get("headlines", [])
            headlines_json = json.dumps(headlines)
            override_json = ""
            _groq_diag = None
            if use_groq:
                _override, _groq_diag = build_groq_override(headlines)
                override_json = json.dumps(_override) if _override else ""
            news_delta_map, news_clusters = cached_compute_news_delta(headlines_json, override_json)
        except Exception as exc:
            logger.error("compute_news_delta gagal: %s", exc)
            news_delta_map = {}
            news_clusters = []
            _groq_diag = None

    # -----------------------------------------------------------------------
    # STEP 4 — Header (dengan status sumber lengkap)
    # -----------------------------------------------------------------------
    sources_status = build_sources_status(
        prices_data, macro_data, cot_data,
        retail_data, news_data, calendar_data,
    )
    # Override status sesuai aturan UX (sumber click-to-run dibaca dari session_state)
    _has_a1_retail = bool((st.session_state.get("a1_retail_data") or {}).get("per_currency"))
    _has_a1_cot = bool(st.session_state.get("a1_cot_data"))
    _has_ff = bool(st.session_state.get("pb_ff_data"))
    # COT: pakai status ASLI dari build_sources_status (hijau hanya kalau CFTC net ada);
    # cuma tambah label A1 kalau A1 sudah di-generate. (Satu badge, tidak dobel.)
    sources_status["retail"] = "ok" if _has_a1_retail else "fail"  # A1 belum ditarik → merah
    sources_status["news"] = "ok"                                  # selalu ada
    sources_status["calendar"] = "ok" if _has_ff else "fail"       # FF belum ditarik → merah
    labels = {
        "COT": "COT (CFTC + A1)" if _has_a1_cot else "COT (CFTC)",
        "retail": "retail (A1)" if _has_a1_retail else "retail (tarik A1)",
        "calendar": "calendar (FF)" if _has_ff else "calendar (tarik FF)",
        "news": "news",
    }

    with header_placeholder.container():
        render_header(sources_status, labels)

    # -----------------------------------------------------------------------
    # STEP 5 — (badge merah retail/calendar = "belum ditarik", bukan error;
    #           sudah tersampaikan via badge. Tidak ada warning myfxbook lagi.)
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # STEP 6 — Toggle Baseline vs News-Overlaid
    # -----------------------------------------------------------------------
    # STEP 7 — Tabs display
    # -----------------------------------------------------------------------
    tab_board, tab_pairs, tab_detail, tab_news, tab_events, tab_retail, tab_cot, tab_feeds = st.tabs([
        "📈 Bias Board",
        "🔍 Pair Scanner",
        "🔬 Detail Skor",
        "📰 News Feed",
        "⏰ Risk Events",
        "📊 Retail",
        "🏛️ COT",
        "🛰️ Data Feeds",
    ])

    with tab_board:
        try:
            render_bias_board(asset_bias_map, news_delta_map, show_overlay)
        except Exception as exc:
            st.error(f"Bias Board error: {exc}")

    with tab_pairs:
        try:
            render_pair_scanner(pair_bias_map, asset_bias_map, show_overlay, news_delta_map)
        except Exception as exc:
            st.error(f"Pair Scanner error: {exc}")

    with tab_detail:
        try:
            render_score_detail(asset_bias_map, news_delta_map, cot_data, retail_data)
        except Exception as exc:
            st.error(f"Detail Skor error: {exc}")

    with tab_news:
        try:
            render_news_feed(news_clusters, news_delta_map, news_data, _groq_diag)
        except Exception as exc:
            st.error(f"News Feed error: {exc}")

    with tab_events:
        try:
            render_risk_events_ff(calendar_data)
        except Exception as exc:
            st.error(f"Key Risk Events error: {exc}")

    with tab_retail:
        try:
            render_retail_tab()
        except Exception as exc:
            st.error(f"Retail tab error: {exc}")

    with tab_cot:
        try:
            render_cot_tab(cot_data)
        except Exception as exc:
            st.error(f"COT tab error: {exc}")

    with tab_feeds:
        try:
            render_data_feeds()
        except Exception as exc:
            st.error(f"Data Feeds error: {exc}")

    # -----------------------------------------------------------------------
    # STEP 8 — Footer
    # -----------------------------------------------------------------------
    render_footer()


# ===========================================================================
# ENTRY
# ===========================================================================
if __name__ == "__main__":
    main()
