"""
IEC-1 model constants: factor names, industry labels, ordering.
"""

# ---------------------------------------------------------------------------
# Factor taxonomy
# ---------------------------------------------------------------------------

MARKET_FACTOR: str = "MARKET"

STYLE_FACTORS: list[str] = [
    "BETA",       # CAPM beta
    "SIZE",       # −ln(market cap)
    "STREV",      # Short-term reversal (−3m return)
    "LTMOM",      # 12−1 momentum
    "VALUE",      # Book-to-price
    "GROWTH",     # Reinvestment/expansion composite
    "LOWVOL",     # Low-volatility composite
    "LIQUIDITY",  # Turnover/trading-depth composite
    "LEVERAGE",   # Debt-burden composite
    "EARNYIELD",  # Earnings yield composite
    "EARNVAR",    # Earnings variability composite
    "PROFIT",     # Profitability / quality composite
]

INDUSTRY_FACTORS: list[str] = [
    "AERODEF",    # Aerospace & Defence
    "AUTOMOBL",   # Automobiles (OEMs)
    "AUTOCOMP",   # Auto Components
    "BANKS",      # Banks
    "BUSINSERV",  # Commercial & Professional Services
    "CAPGOODS",   # Capital Goods (Electrical, Machinery, Engineering, Conglomerates)
    "CHEMICALS",  # Chemicals
    "CONSUMDISC", # Consumer Discretionary (broad)
    "CONSUMDUR",  # Household Durables
    "CONSUMSTAP", # Consumer Staples (broad)
    "FINLSERV",   # Financial Services (non-bank)
    "HEALTHCARE", # Health Care Services & Equipment
    "ITSERVIC",   # IT Services & Outsourcing
    "MATERIALS",  # Materials (Packaging, Paper, Construction Materials)
    "METMINING",  # Metals & Mining
    "OILGAS",     # Oil, Gas & Energy Equipment
    "PHARMA",     # Pharmaceuticals & Biotechnology
    "POWERGEN",   # Independent Power & Renewables
    "REALESTATE", # Real Estate Management & Development
    "SOFTWARE",   # Software
    "TECHCOMP",   # Technology Hardware & Components
    "TELECOM",    # Telecommunication Services
    "TRADDIST",   # Trading Companies & Distributors
    "TRANSPORT",  # Transportation (all modes)
    "UTILITIES",  # Utilities (Electric, Gas, Water)
    "MISCELLAN",  # Miscellaneous / unclassified
]

ALL_FACTORS: list[str] = [MARKET_FACTOR] + STYLE_FACTORS + INDUSTRY_FACTORS
N_FACTORS: int = len(ALL_FACTORS)  # 39

# Slice indices for easy sub-selection
IDX_MARKET: int = 0
IDX_STYLE_START: int = 1
IDX_STYLE_END: int = 1 + len(STYLE_FACTORS)          # exclusive
IDX_INDUSTRY_START: int = IDX_STYLE_END
IDX_INDUSTRY_END: int = IDX_STYLE_END + len(INDUSTRY_FACTORS)  # exclusive

assert N_FACTORS == 39, f"Expected 39 factors, got {N_FACTORS}"

# ---------------------------------------------------------------------------
# Estimation defaults
# ---------------------------------------------------------------------------

DEFAULT_WINSOR_LOWER: float = 0.01   # 1st percentile
DEFAULT_WINSOR_UPPER: float = 0.99   # 99th percentile
DEFAULT_FCM_LOOKBACK: int = 252      # trading days for FCM estimation
DEFAULT_SPEC_LOOKBACK: int = 60      # trading days for specific risk
ANNUALISATION_FACTOR: int = 252
