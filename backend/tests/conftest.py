import os

# Set test environment defaults before any app modules are imported
os.environ["BROKER_PROVIDER"] = "virtual"
os.environ["MARKET_DATA_PROVIDER"] = "simulation"
# No network calls while testing: fallback FX rates are deterministic and the
# international (Yahoo) feed stays dark.
os.environ["FX_ENABLED"] = "0"
os.environ["INTL_MARKET_DATA"] = "0"
# No on-disk book: tests must never inherit positions or orders from a previous
# run (persistence itself is covered explicitly in test_multi_market.py).
os.environ["STATE_DIR"] = ""
