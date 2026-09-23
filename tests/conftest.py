"""Shared test configuration.

These env vars must be set BEFORE the app is imported (config.py reads
the environment once at import time).
"""

import os

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("ENVIRONMENT", "testing")
os.environ.setdefault("ALERTS_ENABLED", "true")
