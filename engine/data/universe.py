"""
U.S. equity universe — GICS sector mappings for ~100 S&P 100 components.

Used by SectorExposureRule and any strategy that needs sector context.
Symbols not in this map are assigned sector "Unknown" (treated as their
own sector for exposure calculations).
"""

from __future__ import annotations

SECTOR_MAP: dict[str, str] = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "GOOGL": "Technology", "META": "Technology", "AVGO": "Technology",
    "ORCL": "Technology", "CRM": "Technology", "ADBE": "Technology",
    "NOW": "Technology", "INTC": "Technology", "QCOM": "Technology",
    "TXN": "Technology", "IBM": "Technology", "ACN": "Technology",
    "MU": "Technology", "AMD": "Technology",

    # Healthcare
    "UNH": "Healthcare", "JNJ": "Healthcare", "LLY": "Healthcare",
    "MRK": "Healthcare", "ABBV": "Healthcare", "TMO": "Healthcare",
    "ABT": "Healthcare", "DHR": "Healthcare", "ISRG": "Healthcare",
    "BMY": "Healthcare", "CVS": "Healthcare", "MDT": "Healthcare",
    "AMGN": "Healthcare", "GILD": "Healthcare", "REGN": "Healthcare",
    "PFE": "Healthcare", "ZTS": "Healthcare", "SYK": "Healthcare",

    # Financials
    "JPM": "Financials", "V": "Financials", "MA": "Financials",
    "BAC": "Financials", "WFC": "Financials", "GS": "Financials",
    "MS": "Financials", "C": "Financials", "BLK": "Financials",
    "AXP": "Financials", "SCHW": "Financials", "SPGI": "Financials",
    "BK": "Financials", "MET": "Financials", "AIG": "Financials",
    "USB": "Financials", "BRK-B": "Financials",

    # Consumer Discretionary
    "AMZN": "ConsumerDiscretionary", "TSLA": "ConsumerDiscretionary",
    "HD": "ConsumerDiscretionary", "MCD": "ConsumerDiscretionary",
    "NKE": "ConsumerDiscretionary", "SBUX": "ConsumerDiscretionary",
    "LOW": "ConsumerDiscretionary", "TGT": "ConsumerDiscretionary",
    "BKNG": "ConsumerDiscretionary", "F": "ConsumerDiscretionary",
    "GM": "ConsumerDiscretionary", "ORLY": "ConsumerDiscretionary",

    # Consumer Staples
    "WMT": "ConsumerStaples", "PG": "ConsumerStaples",
    "KO": "ConsumerStaples", "PEP": "ConsumerStaples",
    "COST": "ConsumerStaples", "PM": "ConsumerStaples",
    "MO": "ConsumerStaples", "MDLZ": "ConsumerStaples",
    "CL": "ConsumerStaples", "KHC": "ConsumerStaples",

    # Industrials
    "CAT": "Industrials", "HON": "Industrials", "UPS": "Industrials",
    "DE": "Industrials", "LMT": "Industrials", "RTX": "Industrials",
    "GE": "Industrials", "BA": "Industrials", "MMM": "Industrials",
    "ETN": "Industrials", "FDX": "Industrials", "GD": "Industrials",
    "EMR": "Industrials", "NOC": "Industrials", "CSX": "Industrials",

    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "EOG": "Energy", "OXY": "Energy", "SLB": "Energy",
    "PSX": "Energy", "MPC": "Energy", "VLO": "Energy",

    # Communication Services
    "NFLX": "Communication", "DIS": "Communication",
    "CMCSA": "Communication", "T": "Communication",
    "VZ": "Communication", "TMUS": "Communication",
    "CHTR": "Communication",

    # Utilities
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    "D": "Utilities", "EXC": "Utilities", "AEP": "Utilities",

    # Real Estate
    "AMT": "RealEstate", "PLD": "RealEstate",
    "CCI": "RealEstate", "EQIX": "RealEstate",

    # Materials
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials",
    "NEM": "Materials", "FCX": "Materials",
}

# Tradeable universe (SPY excluded — it's the regime/benchmark only)
UNIVERSE_SYMBOLS: list[str] = sorted(s for s in SECTOR_MAP)


def sector_of(symbol: str) -> str:
    return SECTOR_MAP.get(symbol.upper(), "Unknown")
