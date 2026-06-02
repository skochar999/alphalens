"""
IEC-1 data ingestion layer.

Sources
-------
yf_source   : yfinance — prices, returns, market cap, fundamentals (~80% coverage)
screener    : Screener.in — 5-year P&L / balance sheet for growth & variability metrics

Entry point
-----------
    from inec1.data.pipeline import DataPipeline
    pipeline = DataPipeline(cache_dir="./cache")
    cross_section = pipeline.build(tickers)   # → pd.DataFrame matching REQUIRED_COLUMNS
"""
