"""
collectors/prices.py — OHLCV price collector.

Sources:
  FX majors, Gold, DXY : yfinance (Yahoo Finance unofficial API)
      Tickers used  → Yahoo symbol mapping at bottom of file.
  Crypto (BTC, ETH)    : ccxt Bybit public REST (no API key needed).
      Endpoint: GET /v5/market/kline (BTCUSDT, ETHUSDT, category=linear)

Schema returned (§4 prices):
{
  "as_of_utc":  "2026-06-01T07:00:00Z",
  "as_of_wib":  "2026-06-01 14:00",
  "prices": {
    "EURUSD": {"last": 1.0850, "chg_pct": 0.33, "atr14": 0.0060},
    ...
    "DXY":    {"last": 98.99,  "chg_pct": -0.22, "atr14": 0.45}
  }
}

Failure contract:
  - Network / parse errors per symbol → {"last": null, "chg_pct": null, "atr14": null, "_error": "<msg>"}
  - Source-level failure → all symbols from that source get null fields + flag.
  - NEVER raises to caller.

ATR14: Wilder's smoothed ATR (EWM com=13) over trailing 20 daily bars.
chg_pct: (today_close - prev_close) / prev_close * 100.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np
import pandas as pd

from utils.timeutils import fmt_iso_utc, fmt_wib_display, now_utc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol maps
# ---------------------------------------------------------------------------

# Yahoo Finance ticker → qf_bias symbol
# FX: "{BASE}{QUOTE}=X"  Gold: "GC=F" or "XAUUSD=X"  DXY: "DX-Y.NYB"
_YF_TICKER_MAP: dict[str, str] = {
    "EURUSD=X":   "EURUSD",
    "GBPUSD=X":   "GBPUSD",
    "USDJPY=X":   "USDJPY",
    "AUDUSD=X":   "AUDUSD",
    "NZDUSD=X":   "NZDUSD",
    "USDCAD=X":   "USDCAD",
    "USDCHF=X":   "USDCHF",
    "GC=F":       "XAUUSD",   # gold futures (XAUUSD=X sering delisted di Yahoo)
    "DX-Y.NYB":   "DXY",      # ICE US Dollar Index
    "BTC-USD":    "BTCUSD",   # crypto via Yahoo (Bybit diblok CloudFront utk IP datacenter)
    "ETH-USD":    "ETHUSD",
}

# ccxt Bybit symbol → qf_bias symbol
_BYBIT_SYMBOL_MAP: dict[str, str] = {
    "BTC/USDT": "BTCUSD",
    "ETH/USDT": "ETHUSD",
}

# Empty-slot template
_EMPTY_SLOT: dict[str, Any] = {"last": None, "chg_pct": None, "atr14": None}


# ---------------------------------------------------------------------------
# ATR helper (shared)
# ---------------------------------------------------------------------------

def _calc_atr14(high: pd.Series, low: pd.Series, close: pd.Series) -> float | None:
    """
    Wilder's ATR-14 using EWM (com=13, adjust=False).

    Returns None if fewer than 15 bars available.
    """
    if len(close) < 15:
        return None
    prev_close = close.shift(1).dropna()
    h = high.iloc[1:].values
    l = low.iloc[1:].values
    c_prev = prev_close.values

    tr = np.maximum(h - l, np.maximum(np.abs(h - c_prev), np.abs(l - c_prev)))
    atr_series = pd.Series(tr).ewm(com=13, adjust=False).mean()
    return float(round(atr_series.iloc[-1], 6))


# ---------------------------------------------------------------------------
# Yahoo Finance collector (FX + Gold + DXY)
# ---------------------------------------------------------------------------

def _fetch_yfinance(
    tickers: list[str], period: str = "25d", retries: int = 1, timeout: int = 15
) -> dict[str, pd.DataFrame]:
    """
    Download OHLCV DataFrames from yfinance for a list of tickers.

    Returns dict[ticker → DataFrame] (empty DataFrame on failure).
    Retries once on any exception before giving up.
    """
    import yfinance as yf

    attempt = 0
    while attempt <= retries:
        try:
            raw = yf.download(
                tickers,
                period=period,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
                timeout=timeout,
            )
            if raw.empty:
                raise ValueError("yfinance returned empty DataFrame")

            # yfinance returns multi-level columns when >1 ticker
            # Level 0: (Open/High/Low/Close/Volume), Level 1: ticker symbol
            result: dict[str, pd.DataFrame] = {}
            if isinstance(raw.columns, pd.MultiIndex):
                for tk in tickers:
                    try:
                        df = raw.xs(tk, axis=1, level=1).copy()
                        df.dropna(subset=["Close"], inplace=True)
                        result[tk] = df
                    except KeyError:
                        result[tk] = pd.DataFrame()
            else:
                # Single ticker → flat columns
                raw.dropna(subset=["Close"], inplace=True)
                result[tickers[0]] = raw

            return result

        except Exception as exc:
            logger.warning("yfinance attempt %d/%d failed: %s", attempt + 1, retries + 1, exc)
            attempt += 1
            if attempt <= retries:
                time.sleep(2)

    # All attempts exhausted → return empty frames for each ticker
    return {tk: pd.DataFrame() for tk in tickers}


def _parse_yf_slot(df: pd.DataFrame, symbol: str) -> dict[str, Any]:
    """Extract last, chg_pct, atr14 from a yfinance OHLCV DataFrame."""
    slot: dict[str, Any] = dict(_EMPTY_SLOT)
    if df.empty or len(df) < 2:
        slot["_error"] = "insufficient_data"
        return slot

    try:
        last_row = df.iloc[-1]
        prev_row = df.iloc[-2]

        last_close: float = float(last_row["Close"])
        prev_close: float = float(prev_row["Close"])
        chg_pct = round((last_close - prev_close) / prev_close * 100, 4)

        # For Gold (XAUUSD=X) Yahoo sometimes provides only Close without H/L
        if "High" in df.columns and "Low" in df.columns:
            atr14 = _calc_atr14(df["High"], df["Low"], df["Close"])
        else:
            # Fallback: estimate TR from close-only (less accurate)
            atr14 = None
            logger.warning("%s: High/Low missing — ATR14 not computable", symbol)

        slot["last"] = round(last_close, 6)
        slot["chg_pct"] = chg_pct
        slot["atr14"] = atr14

    except Exception as exc:
        slot["_error"] = str(exc)
        logger.error("Parsing yfinance slot for %s: %s", symbol, exc)

    return slot


# ---------------------------------------------------------------------------
# Bybit collector (Crypto)
# ---------------------------------------------------------------------------

def _fetch_bybit_ohlcv(
    ccxt_symbol: str, limit: int = 22, retries: int = 1, timeout: int = 12
) -> list[list]:
    """
    Fetch daily OHLCV candles from Bybit via ccxt public endpoint.

    Bybit REST: GET https://api.bybit.com/v5/market/kline
    ccxt wraps this as exchange.fetch_ohlcv(symbol, '1d', limit=N).
    No API key required (public market data).

    Returns list of [timestamp_ms, open, high, low, close, volume] candles,
    or empty list on failure.
    """
    import ccxt

    exchange = ccxt.bybit(
        {
            "enableRateLimit": True,
            "timeout": timeout * 1000,  # ccxt uses ms
        }
    )

    attempt = 0
    while attempt <= retries:
        try:
            ohlcv = exchange.fetch_ohlcv(ccxt_symbol, timeframe="1d", limit=limit)
            if not ohlcv:
                raise ValueError("Bybit returned empty OHLCV")
            return ohlcv

        except Exception as exc:
            logger.warning(
                "Bybit %s attempt %d/%d failed: %s",
                ccxt_symbol, attempt + 1, retries + 1, exc,
            )
            attempt += 1
            if attempt <= retries:
                time.sleep(2)

    return []


def _parse_bybit_slot(ohlcv: list[list], symbol: str) -> dict[str, Any]:
    """Extract last, chg_pct, atr14 from Bybit OHLCV candle list."""
    slot: dict[str, Any] = dict(_EMPTY_SLOT)
    if not ohlcv or len(ohlcv) < 2:
        slot["_error"] = "insufficient_data"
        return slot

    try:
        # ohlcv: [[ts_ms, O, H, L, C, V], ...]  sorted oldest→newest
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])

        last_close = float(df["close"].iloc[-1])
        prev_close = float(df["close"].iloc[-2])
        chg_pct = round((last_close - prev_close) / prev_close * 100, 4)
        atr14 = _calc_atr14(df["high"], df["low"], df["close"])

        slot["last"] = round(last_close, 2)
        slot["chg_pct"] = chg_pct
        slot["atr14"] = atr14

    except Exception as exc:
        slot["_error"] = str(exc)
        logger.error("Parsing Bybit slot for %s: %s", symbol, exc)

    return slot


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_prices() -> dict[str, Any]:
    """
    Fetch OHLCV for all QF_BIAS assets and return a schema-§4-compliant dict.

    FX / Gold / DXY  → yfinance (Yahoo Finance)
    BTC / ETH        → ccxt Bybit public

    On any per-symbol failure the slot contains null fields + "_error" key.
    The top-level dict is always returned (never raises).
    """
    ts = now_utc()
    result: dict[str, Any] = {
        "as_of_utc": fmt_iso_utc(ts),
        "as_of_wib": fmt_wib_display(ts),
        "prices": {},
    }

    prices = result["prices"]

    # --- Yahoo Finance batch --------------------------------------------------
    yf_tickers = list(_YF_TICKER_MAP.keys())
    logger.info("Fetching yfinance tickers: %s", yf_tickers)
    yf_frames = _fetch_yfinance(yf_tickers)

    for yf_tk, qf_symbol in _YF_TICKER_MAP.items():
        df = yf_frames.get(yf_tk, pd.DataFrame())
        prices[qf_symbol] = _parse_yf_slot(df, qf_symbol)

    # --- Crypto: via yfinance (BTC-USD/ETH-USD) sudah termasuk di _YF_TICKER_MAP ---
    # Bybit dilewati: CloudFront 403 untuk IP datacenter (Streamlit Cloud).
    # Fungsi _fetch_bybit_ohlcv tetap ada untuk pemakaian lokal/VPS bila diperlukan.

    logger.info(
        "get_prices() done — %d symbols, %d OK",
        len(prices),
        sum(1 for v in prices.values() if v.get("last") is not None),
    )
    return result


# ---------------------------------------------------------------------------
# Symbol maps (reference)
# ---------------------------------------------------------------------------
#
# Yahoo Finance ticker reference:
#   EURUSD=X, GBPUSD=X, USDJPY=X, AUDUSD=X, NZDUSD=X, USDCAD=X, USDCHF=X
#   XAUUSD=X   (spot Gold in USD; alternate: "GC=F" front-month futures if spot unavail)
#   DX-Y.NYB   (ICE US Dollar Index = DXY)
#
# Bybit ccxt symbols (linear perpetuals — sufficient for daily close / ATR):
#   BTC/USDT → BTCUSD
#   ETH/USDT → ETHUSD
#
# NOTE: Yahoo Finance is an unofficial/undocumented API. It may rate-limit or
# return 429/403. The 1-retry logic + 60-second Streamlit cache TTL should keep
# failures rare in production. If Yahoo changes the API, switch to a paid
# provider (Alpha Vantage, Polygon.io) or use Bybit/Binance for FX pairs too.
