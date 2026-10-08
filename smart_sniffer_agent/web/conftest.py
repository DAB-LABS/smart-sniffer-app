"""Shared by the page tests: one Chromium for the whole run, since Playwright
allows one sync instance per thread. PW_CHROMIUM picks the executable."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as p:
        kwargs = {}
        if os.environ.get("PW_CHROMIUM"):
            kwargs["executable_path"] = os.environ["PW_CHROMIUM"]
        b = p.chromium.launch(**kwargs)
        yield b
        b.close()
