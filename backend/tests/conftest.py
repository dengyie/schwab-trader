import os

# Set test environment defaults before any app modules are imported
os.environ["BROKER_PROVIDER"] = "virtual"
os.environ["MARKET_DATA_PROVIDER"] = "simulation"
