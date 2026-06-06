"""
collectors/cot.py — CFTC Commitments of Traders (COT) collector.

Report: Traders in Financial Futures (TFF) — futures finansial.
  Annual history : https://www.cftc.gov/files/dea/history/fut_fin_txt_{YYYY}.zip
  Latest week     : https://www.cftc.gov/files/dea/newcot/FinFutWk.txt

Kategori → kolom TFF (net = long − short):
  leveraged_funds : Lev_Money_Positions_Long_All  − Lev_Money_Positions_Short_All   (FX, BTC)
  managed_money   : Asset_Mgr_Positions_Long_All  − Asset_Mgr_Positions_Short_All   (Gold)
  (config.COT_CATEGORY menentukan mana yang dipakai per-aset)

COT Index = posisi net saat ini sebagai percentile vs range 156-minggu (3thn):
  cot_index = (net - min_3yr) / (max_3yr - min_3yr) × 100   → clip [0, 100]
  <52 minggu history → cot_index = None + flag insufficient.

Schema §4:
{
  "as_of_tuesday": "2026-05-26", "released": "2026-05-29", "days_since_snapshot": 6,
  "cot": {"USD": {"category":"leveraged_funds","net":-12345,"cot_index":72.5}, ...},
  "_meta": {"source":"CFTC TFF","weeks_history":156,"assets_ok":[...],"assets_missing":[...],"stale":false}
}

Failure contract:
  - Network gagal → cot={} + _meta.stale=true, NEVER raises.
  - Aset tidak ketemu → {"net":null,"cot_index":null,"_error":"not_found_in_cftc"}.
  - History <52 minggu → cot_index=null + flag.
  - Stale (shutdown/delay) → days_since_snapshot besar; engine/freshness diskon bobot.
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from typing import Any

import pandas as pd
import requests

from config import COT_CATEGORY
from utils.timeutils import (
    cot_release_friday,
    days_since_cot_snapshot,
    fmt_iso_utc,
    last_cot_tuesday,
    now_utc,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CFTC URL
# ---------------------------------------------------------------------------

_CFTC_ANNUAL_TFF_URL = "https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip"
_CFTC_WEEKLY_TFF_URL = "https://www.cftc.gov/dea/newcot/FinFutWk.txt"  # path tanpa /files/

_REQUEST_TIMEOUT = 25
_RETRIES = 1
_MIN_WEEKS_FOR_INDEX = 52     # di bawah ini → cot_index None
_FULL_WINDOW_WEEKS = 156      # 3 tahun

# ---------------------------------------------------------------------------
# CFTC market name → qf_bias asset (substring, case-insensitive)
# ---------------------------------------------------------------------------

_MARKET_MATCH: dict[str, str] = {
    "USD INDEX - ICE FUTURES":                 "USD",   # ICE USDX (DXY futures)
    "U.S. DOLLAR INDEX":                       "USD",   # alias lama
    "EURO FX - CHICAGO MERCANTILE":            "EUR",
    "BRITISH POUND - CHICAGO MERCANTILE":      "GBP",
    "BRITISH POUND STERLING - CHICAGO":        "GBP",   # alias lama
    "JAPANESE YEN - CHICAGO MERCANTILE":       "JPY",
    "AUSTRALIAN DOLLAR - CHICAGO MERCANTILE":  "AUD",
    "NEW ZEALAND DOLLAR - CHICAGO":            "NZD",
    "CANADIAN DOLLAR - CHICAGO MERCANTILE":    "CAD",
    "SWISS FRANC - CHICAGO MERCANTILE":        "CHF",
    "GOLD - COMMODITY EXCHANGE":               "XAU",
    "GOLD - COMMODITY EXCHANGE INC":           "XAU",
    "GOLD - COMMODITY EXCHANGE, INC":          "XAU",
    "BITCOIN - CHICAGO MERCANTILE":            "BTC",
}

_ETH_NOTE = "ETH not available in CFTC TFF as of v1"

# ---------------------------------------------------------------------------
# Kolom TFF (dinormalisasi: strip spasi)
# ---------------------------------------------------------------------------

_COL_NAME = "Market_and_Exchange_Names"
_COL_DATE = "Report_Date_as_YYYY-MM-DD"   # TFF modern; fallback ke MM_DD_YYYY di bawah

_CAT_COLS: dict[str, tuple[str, str]] = {
    "leveraged_funds": ("Lev_Money_Positions_Long_All", "Lev_Money_Positions_Short_All"),
    "managed_money":   ("Asset_Mgr_Positions_Long_All", "Asset_Mgr_Positions_Short_All"),
    # DUMB MONEY: Non-Reportable = small traders di bawah ambang pelaporan (retail-ish).
    # Kolom ada di file TFF yang SAMA (sudah di-download). Proxy dumb-money gratis, no-auth.
    "nonreportable":   ("NonRept_Positions_Long_All", "NonRept_Positions_Short_All"),
}


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _http_get_bytes(url: str, session: requests.Session | None = None) -> bytes | None:
    """GET → bytes. None saat gagal. Retry sekali untuk timeout/transient."""
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", "qf_bias/1.0 (research tool)")
    for attempt in range(_RETRIES + 1):
        try:
            resp = sess.get(url, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.HTTPError as exc:
            logger.error("CFTC HTTP error %s: %s", url, exc)
            return None  # 4xx → no retry
        except Exception as exc:
            logger.warning("CFTC fetch %s gagal (attempt %d/%d): %s",
                           url, attempt + 1, _RETRIES + 1, exc)
            if attempt < _RETRIES:
                time.sleep(2)
    return None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _load_zip(content: bytes) -> pd.DataFrame | None:
    """Ekstrak .txt dari zip CFTC → DataFrame."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            txt = [n for n in z.namelist() if n.lower().endswith(".txt")]
            if not txt:
                logger.error("Zip CFTC tanpa .txt: %s", z.namelist())
                return None
            raw = z.read(txt[0])
        df = pd.read_csv(io.BytesIO(raw), low_memory=False)
        df.columns = df.columns.str.strip()
        df = df.loc[:, ~df.columns.duplicated()]   # buang kolom duplikat → cegah concat reindex crash
        return df
    except Exception as exc:
        logger.error("Gagal parse zip CFTC: %s", exc)
        return None


def _load_txt(content: bytes) -> pd.DataFrame | None:
    """Parse file weekly plain-text (comma-separated)."""
    try:
        df = pd.read_csv(io.BytesIO(content), low_memory=False)
        df.columns = df.columns.str.strip()
        df = df.loc[:, ~df.columns.duplicated()]   # buang kolom duplikat → cegah concat reindex crash
        return df
    except Exception as exc:
        logger.error("Gagal parse weekly TFF: %s", exc)
        return None


def _fetch_history(years_back: int = 4) -> pd.DataFrame | None:
    """Download + gabung TFF annual untuk beberapa tahun (utk window 156 minggu)."""
    current_year = now_utc().year
    frames: list[pd.DataFrame] = []
    session = requests.Session()
    for yr in range(current_year, current_year - years_back - 1, -1):
        raw = _http_get_bytes(_CFTC_ANNUAL_TFF_URL.format(year=yr), session=session)
        if raw is None:
            logger.warning("CFTC annual %d tidak tersedia; skip", yr)
            continue
        df_yr = _load_zip(raw)
        if df_yr is not None and not df_yr.empty:
            frames.append(df_yr)
            logger.info("CFTC annual %d: %d baris", yr, len(df_yr))
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


def _fetch_latest_week() -> pd.DataFrame | None:
    """Ambil file minggu terbaru (kadang lebih baru dari annual)."""
    raw = _http_get_bytes(_CFTC_WEEKLY_TFF_URL)
    if raw is None:
        return None
    return _load_txt(raw)


# ---------------------------------------------------------------------------
# Row / net / index helpers
# ---------------------------------------------------------------------------

def _date_col(df: pd.DataFrame) -> str | None:
    """Cari nama kolom tanggal (TFF berubah-ubah: YYYY-MM-DD atau MM_DD_YYYY)."""
    for c in df.columns:
        cl = c.lower()
        if "report" in cl and "date" in cl:
            return c
    return None


def _match_asset(market_name: str) -> str | None:
    """Map nama market CFTC → asset qf_bias via substring."""
    up = str(market_name).upper()
    for substr, asset in _MARKET_MATCH.items():
        if substr in up:
            return asset
    return None


def _extract_net(row: pd.Series, category: str) -> int | None:
    """Net = long − short untuk kategori. None kalau kolom hilang/non-numeric."""
    cols = _CAT_COLS.get(category)
    if cols is None:
        logger.error("Kategori COT tidak dikenal: %s", category)
        return None
    long_col, short_col = cols
    if long_col not in row.index or short_col not in row.index:
        return None
    try:
        return int(float(row[long_col]) - float(row[short_col]))
    except (TypeError, ValueError):
        return None


def _compute_cot_index(
    asset: str,
    current_net: int,
    history: pd.DataFrame,
    category: str,
    date_col: str,
) -> float | None:
    """COT Index = percentile net vs range 156-minggu. None kalau history <52 mgg."""
    # Filter baris untuk asset ini (reset index dulu → cegah reindex error pada index non-unik)
    h = history.reset_index(drop=True)
    mask = h[_COL_NAME].apply(lambda n: _match_asset(n) == asset)
    sub = h[mask].copy()
    if sub.empty:
        return None

    # Hitung net per baris historis
    nets: list[int] = []
    for _, r in sub.iterrows():
        n = _extract_net(r, category)
        if n is not None:
            nets.append(n)

    if len(nets) < _MIN_WEEKS_FOR_INDEX:
        logger.debug("COT %s: history %d mgg (<%d) → index None",
                     asset, len(nets), _MIN_WEEKS_FOR_INDEX)
        return None

    # Window 156 minggu terakhir (atau seluruhnya kalau kurang)
    window = nets[-_FULL_WINDOW_WEEKS:] if len(nets) > _FULL_WINDOW_WEEKS else nets
    lo, hi = min(window), max(window)
    if hi == lo:
        return 50.0  # flat range → netral
    idx = (current_net - lo) / (hi - lo) * 100.0
    return round(max(0.0, min(100.0, idx)), 1)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_cot() -> dict[str, Any]:
    """Fetch COT TFF, hitung net + COT Index per aset. Schema §4. Never raises."""
    snapshot = last_cot_tuesday()
    released = cot_release_friday(snapshot)
    days_since = days_since_cot_snapshot(snapshot)

    result: dict[str, Any] = {
        "as_of_tuesday": snapshot.strftime("%Y-%m-%d"),
        "released": released.strftime("%Y-%m-%d"),
        "days_since_snapshot": days_since,
        "cot": {},
        "_meta": {
            "source": "CFTC TFF",
            "weeks_history": 0,
            "assets_ok": [],
            "assets_missing": [],
            "stale": False,
        },
    }

    # --- Fetch ---
    frames: list[pd.DataFrame] = []
    hist = _fetch_history()
    if hist is not None and not hist.empty:
        frames.append(hist)
    weekly = _fetch_latest_week()
    if weekly is not None and not weekly.empty:
        frames.append(weekly)

    if not frames:
        logger.error("Semua sumber CFTC gagal — COT kosong")
        result["_meta"]["stale"] = True
        result["_meta"]["assets_missing"] = list(COT_CATEGORY.keys())
        return result

    combined = pd.concat(frames, ignore_index=True)

    # === ANTI-REINDEX: konversi ke list-of-dicts SEGERA, lalu pakai Python murni ===
    # Apapun keanehan DataFrame (index/kolom duplikat, kolom 2D), records aman.
    # Kolom duplikat: ambil kemunculan PERTAMA tiap nama kolom.
    seen_cols: dict[str, int] = {}
    col_positions: list[tuple[str, int]] = []
    for pos, cname in enumerate(combined.columns):
        if cname not in seen_cols:
            seen_cols[cname] = pos
            col_positions.append((cname, pos))
    # Bangun records via posisi (iloc) → tidak tersentuh masalah index/nama duplikat.
    values = combined.to_numpy()
    records: list[dict] = []
    for row in values:
        rec = {}
        for cname, pos in col_positions:
            rec[cname] = row[pos]
        records.append(rec)

    if _COL_NAME not in seen_cols:
        logger.error("Kolom %s tidak ada di data CFTC", _COL_NAME)
        result["_meta"]["stale"] = True
        result["_meta"]["assets_missing"] = list(COT_CATEGORY.keys())
        return result

    # Cari kolom tanggal (nama, dari records)
    date_col = None
    for cname, _ in col_positions:
        cl = cname.lower()
        if "report" in cl and "date" in cl:
            date_col = cname
            break

    # Parse tanggal + sort + dedup per (market, date) — Python murni
    def _parse_date(v):
        try:
            return pd.to_datetime(v, errors="coerce")
        except Exception:
            return pd.NaT

    if date_col:
        for rec in records:
            rec["_d"] = _parse_date(rec.get(date_col))
        # sort by date (NaT paling akhir)
        records.sort(key=lambda r: (r["_d"] is pd.NaT or pd.isna(r["_d"]), r["_d"] if not pd.isna(r["_d"]) else pd.Timestamp.min))
        # dedup per (market, date) keep last
        dedup: dict[tuple, dict] = {}
        for rec in records:
            key = (str(rec.get(_COL_NAME)), str(rec.get("_d")))
            dedup[key] = rec   # last wins
        records = list(dedup.values())
        # weeks_history = jumlah tanggal unik
        uniq_dates = {str(r["_d"]) for r in records if not pd.isna(r.get("_d"))}
        result["_meta"]["weeks_history"] = len(uniq_dates)

    # Stale flag
    if days_since > 10:
        result["_meta"]["stale"] = True
        logger.warning("COT %d hari lama (snapshot %s) — kemungkinan shutdown/delay",
                       days_since, result["as_of_tuesday"])

    # Helper net dari dict (bukan Series)
    def _net_from_rec(rec: dict, category: str):
        cols = _CAT_COLS.get(category)
        if cols is None:
            return None
        long_col, short_col = cols
        if long_col not in rec or short_col not in rec:
            return None
        try:
            return int(float(rec[long_col]) - float(rec[short_col]))
        except (TypeError, ValueError):
            return None

    # --- Per-asset (Python murni, nol pandas indexing) ---
    for asset, category in COT_CATEGORY.items():
        slot: dict[str, Any] = {"category": category, "net": None, "cot_index": None}

        if asset == "ETH":
            slot["_error"] = _ETH_NOTE
            result["cot"][asset] = slot
            result["_meta"]["assets_missing"].append(asset)
            continue

        # Semua baris utk asset ini (urut kronologis krn records sudah di-sort)
        asset_rows = [r for r in records if _match_asset(r.get(_COL_NAME)) == asset]
        if not asset_rows:
            slot["_error"] = "not_found_in_cftc"
            result["cot"][asset] = slot
            result["_meta"]["assets_missing"].append(asset)
            continue

        latest = asset_rows[-1]
        net = _net_from_rec(latest, category)
        if net is None:
            slot["_error"] = "net_extraction_failed"
            result["cot"][asset] = slot
            result["_meta"]["assets_missing"].append(asset)
            continue

        slot["net"] = net
        # long%/short% dari komponen long & short (untuk kartu UI, samakan dgn A1)
        _cols = _CAT_COLS.get(category)
        if _cols:
            try:
                _l = float(latest.get(_cols[0], 0))
                _s = float(latest.get(_cols[1], 0))
                _tot = _l + _s
                if _tot > 0:
                    slot["long_pct"] = round(_l / _tot * 100, 1)
                    slot["short_pct"] = round(_s / _tot * 100, 1)
            except (TypeError, ValueError):
                pass
        # COT Index = percentile vs window 156 mgg (Python murni)
        nets = [n for n in (_net_from_rec(r, category) for r in asset_rows) if n is not None]
        if len(nets) >= _MIN_WEEKS_FOR_INDEX:
            window = nets[-_FULL_WINDOW_WEEKS:] if len(nets) > _FULL_WINDOW_WEEKS else nets
            lo, hi = min(window), max(window)
            if hi == lo:
                slot["cot_index"] = 50.0
            else:
                idx = (net - lo) / (hi - lo) * 100.0
                slot["cot_index"] = round(max(0.0, min(100.0, idx)), 1)

        # --- DUMB MONEY (Non-Reportable / small traders) — DISPLAY-ONLY ---
        # Tidak di-wire ke poin bias (itu keputusan backtest). Diekspos sebagai konteks +
        # divergence vs smart money. Net positif = retail net-long aset itu.
        dumb_net = _net_from_rec(latest, "nonreportable")
        if dumb_net is not None:
            slot["dumb_net"] = dumb_net
            dnets = [n for n in (_net_from_rec(r, "nonreportable") for r in asset_rows) if n is not None]
            if len(dnets) >= _MIN_WEEKS_FOR_INDEX:
                dwin = dnets[-_FULL_WINDOW_WEEKS:] if len(dnets) > _FULL_WINDOW_WEEKS else dnets
                dlo, dhi = min(dwin), max(dwin)
                slot["dumb_index"] = 50.0 if dhi == dlo else round(max(0.0, min(100.0, (dumb_net - dlo) / (dhi - dlo) * 100.0)), 1)
            # Divergence: smart (net) vs dumb (dumb_net) tanda berlawanan = setup kontrarian klasik
            if net is not None and dumb_net != 0 and (net > 0) != (dumb_net > 0):
                slot["smart_dumb_divergence"] = True

        result["cot"][asset] = slot
        result["_meta"]["assets_ok"].append(asset)

    logger.info("get_cot() done — OK:%s MISSING:%s days_since:%d",
                result["_meta"]["assets_ok"], result["_meta"]["assets_missing"], days_since)
    return result


# ---------------------------------------------------------------------------
# Self-test (dummy — tidak fetch network)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Test helper murni tanpa network
    print("=== COT helper self-test ===")

    # _match_asset
    assert _match_asset("EURO FX - CHICAGO MERCANTILE EXCHANGE") == "EUR"
    assert _match_asset("GOLD - COMMODITY EXCHANGE INC.") == "XAU"
    assert _match_asset("RANDOM CONTRACT") is None
    print("✓ _match_asset OK")

    # _extract_net
    row = pd.Series({
        "Lev_Money_Positions_Long_All": 50000,
        "Lev_Money_Positions_Short_All": 30000,
    })
    assert _extract_net(row, "leveraged_funds") == 20000
    assert _extract_net(row, "managed_money") is None  # kolom tidak ada
    print("✓ _extract_net OK (net = long − short = 20000)")

    # _compute_cot_index via DataFrame dummy
    hist = pd.DataFrame({
        _COL_NAME: ["EURO FX - CHICAGO MERCANTILE"] * 60,
        "Lev_Money_Positions_Long_All": list(range(60)),
        "Lev_Money_Positions_Short_All": [0] * 60,
    })
    # net per baris = 0..59; current net=59 → percentile ~100
    idx = _compute_cot_index("EUR", 59, hist, "leveraged_funds", None)
    assert idx == 100.0, f"dapat {idx}"
    idx_mid = _compute_cot_index("EUR", 30, hist, "leveraged_funds", None)
    print(f"✓ _compute_cot_index OK (net=59→{idx}, net=30→{idx_mid})")

    # history < 52 minggu → None
    short_hist = hist.head(40)
    idx_none = _compute_cot_index("EUR", 30, short_hist, "leveraged_funds", None)
    assert idx_none is None
    print("✓ history <52 mgg → cot_index None")

    print("\nSemua helper self-test passed. (get_cot() butuh network CFTC — test saat deploy.)")
