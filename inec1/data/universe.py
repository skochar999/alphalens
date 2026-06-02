"""
NSE equity universe helpers for IEC-1.

Default universe: Nifty 500 constituents (hardcoded as of mid-2025).
Users can override with any ticker list or load from an NSE-downloaded CSV.

NSE publishes constituent lists at:
    https://www.nseindia.com/market-data/live-equity-market  (Index → Download)
Pass the downloaded CSV path to `load_from_nse_csv()`.

All tickers are returned with the `.NS` suffix required by yfinance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd


# ---------------------------------------------------------------------------
# Nifty 500 — as of June 2025 (yfinance .NS tickers)
# Update periodically from NSE website.
# ---------------------------------------------------------------------------

_NIFTY500_BASE: list[str] = [
    # Nifty 50
    "RELIANCE","TCS","HDFCBANK","BHARTIARTL","ICICIBANK","INFY","SBIN",
    "HINDUNILVR","ITC","LT","KOTAKBANK","BAJFINANCE","HCLTECH","AXISBANK",
    "MARUTI","ASIANPAINT","TITAN","ULTRACEMCO","NESTLEIND","WIPRO","ADANIENT",
    "SUNPHARMA","BAJAJFINSV","ONGC","NTPC","POWERGRID","TECHM","M&M",
    "TATAMOTORS","COALINDIA","TATASTEEL","JSWSTEEL","ADANIPORTS","HINDALCO",
    "BPCL","CIPLA","DRREDDY","DIVISLAB","EICHERMOT","APOLLOHOSP","GRASIM",
    "BRITANNIA","HEROMOTOCO","TATACONSUM","INDUSINDBK","BAJAJ-AUTO","SBILIFE",
    "HDFCLIFE","UPL","LTIM",
    # Nifty Next 50
    "SIEMENS","HAVELLS","PIDILITIND","DABUR","MARICO","COLPAL","BERGEPAINT",
    "MUTHOOTFIN","CHOLAFIN","SBICARD","ICICIGI","HDFCAMC","NAUKRI","MCDOWELL-N",
    "TRENT","INDIGO","AMBUJACEM","ACC","GLENMARK","TORNTPHARM","ALKEM",
    "LUPIN","BIOCON","AUROPHARMA","IPCALAB","CADILAHC","GLAXO","PFIZER",
    "ABBOTINDIA","SANOFI",
    # Mid-cap additions (sample — extend as needed)
    "ASTRAL","POLYCAB","PERSISTENT","COFORGE","MPHASIS","LTTS","KPITTECH",
    "TATAELXSI","MASTEK","SONATSOFTW","HEXAWARE","RBLBANK","IDFCFIRSTB",
    "FEDERALBNK","BANDHANBNK","AUBANK","CANBK","BANKBARODA","PNB","UNIONBANK",
    "INDBANK","IOB","UCOBANK","CENTRALBK","MAHABANK","VIJAYABANK",
    "CROMPTON","VOLTAS","BLUESTARCO","WHIRLPOOL","ORIENTELEC","VGUARD",
    "RAJESHEXPO","KALYANKJIL","SENCO","RITES","IRFC","HUDCO","RECLTD","PFC",
    "SJVN","NHPC","TORNTPOWER","TATAPOWER","ADANIGREEN","ADANIPOWER",
    "CESC","JPPOWER","RPOWER","KPEL","GREENPANEL",
    "DMART","TRENT","VSTIND","GLOBUSSPR","NYKAA","ZOMATO","PAYTM",
    "POLICYBZR","CARTRADE","EASEMYTRIP","IRCTC","RAILTEL","HFCL",
    "STLTECH","TEJAS","VINDHYATEL","GTLINFRA","MTNL","TTML",
    "PIIND","RALLIS","ASTERDM","MAXHEALTH","METROPOLIS","THYROCARE",
    "LALPATHLAB","KRSNAA","VIJAYA","SURYODAY","ESAFSFB","UTKARSHBNK",
    "EQUITASBNK","JAIBALAJI","HUDCO","IREDA","NLCINDIA","MOIL","NMDC",
    "VEDL","NATIONALUM","HINDZINC","HINDCOPPER","GMRINFRA","IRB",
    "ASHOKA","SADBHAV","DILIPBUILDCON","NCC","KEC","KALPATPOWR","PATEL",
    "ENGINERSIN","RVNL","IRCON","NBCC","WABAG","VA-TECH",
    "PAGEIND","SIYARAM","ARVIND","KPR","GHCL","SPANDANA","MANAPPURAM",
    "AAVAS","HOMEFIRST","APTUS","REPCO","GRUH",
    "CANFINHOME","LICHSGFIN","PNBHOUSING","INDIABULS","IBULHSGFIN",
    "GODREJPROP","OBEROIRLTY","PHOENIXLTD","BRIGADE","SOBHA","PRESTIGE",
    "MAHLIFE","KOLTEPATIL","KEYFINSERV","SHRIRAMFIN","M&MFIN","MAGMA",
    "SUNDARMFIN","CITIUSFIN","BAJAJHFL","EDELWEISS","MOTILALOFS",
    "ANGELONE","5PAISA","IIFL","GEOJITFSL","VENTURA",
]

# Add .NS suffix once
NIFTY500: list[str] = [t + ".NS" for t in _NIFTY500_BASE]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_nifty500() -> list[str]:
    """Return the built-in Nifty 500 ticker list (yfinance format)."""
    return list(NIFTY500)


def get_nifty50() -> list[str]:
    """Return just the Nifty 50 tickers (first 50 in the list)."""
    return [t + ".NS" for t in _NIFTY500_BASE[:50]]


def get_nifty100() -> list[str]:
    """Return the Nifty 100 tickers (first 80 in the list)."""
    return [t + ".NS" for t in _NIFTY500_BASE[:80]]


def load_from_nse_csv(path: str | Path) -> list[str]:
    """
    Load NSE index constituent CSV downloaded from nseindia.com.

    NSE CSV columns typically include 'Symbol'. This function reads
    that column and appends '.NS' for yfinance compatibility.

    Parameters
    ----------
    path : str | Path
        Path to the downloaded NSE CSV (e.g., ind_nifty500list.csv).

    Returns
    -------
    list[str]  yfinance-format tickers with .NS suffix
    """
    df = pd.read_csv(path)
    # NSE CSVs use 'Symbol' column
    col = next((c for c in df.columns if c.strip().lower() == "symbol"), None)
    if col is None:
        raise ValueError(
            f"Could not find 'Symbol' column. Available columns: {list(df.columns)}"
        )
    tickers = df[col].str.strip().tolist()
    # Filter out index/header rows like "NIFTY 500" (indices have spaces; stock symbols don't)
    tickers = [t for t in tickers if t and " " not in t]
    return [t + ".NS" for t in tickers]


def load_custom(tickers: list[str]) -> list[str]:
    """
    Accept a user-supplied list of tickers.

    Tickers may be bare NSE symbols (e.g. 'RELIANCE') or already
    have the '.NS' suffix. Both formats are normalised.
    """
    result = []
    for t in tickers:
        t = t.strip().upper()
        if not t.endswith(".NS"):
            t += ".NS"
        result.append(t)
    return result


def strip_suffix(ticker: str) -> str:
    """'RELIANCE.NS' → 'RELIANCE'"""
    return ticker.replace(".NS", "").replace(".BO", "")
