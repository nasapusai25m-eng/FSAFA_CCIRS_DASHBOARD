"""Shared test set-up.

The public deployment hides the demo company and the Backtest tab (config.ENABLE_DEMO / ENABLE_BACKTEST = False).
Most app smoke tests drive the app through the Apex demo, so they switch both back on here. Tests of the hidden
state set them to False explicitly; DEPLOY_FLAGS keeps the values shipped in config.py.
"""
import pytest

import config

DEPLOY_FLAGS = {"ENABLE_DEMO": config.ENABLE_DEMO, "ENABLE_BACKTEST": config.ENABLE_BACKTEST,
                "ENABLE_AI_MEMO": config.ENABLE_AI_MEMO}


@pytest.fixture(autouse=True)
def _demo_and_backtest_on(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_DEMO", True)
    monkeypatch.setattr(config, "ENABLE_BACKTEST", True)
