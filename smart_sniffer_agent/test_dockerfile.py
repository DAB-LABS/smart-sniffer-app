"""The Dockerfile ships the agent of the release it names.

SS_TAG picks both the Go agent binary and the rule modules. The agent must
never come from "latest", which changes under an app version that is
already out and makes a local rebuild ship whatever was newest that day.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).parent / "Dockerfile"


def _text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _agent_run() -> str:
    """The RUN instruction that downloads the agent binary, continuation
    lines joined."""
    text = _text().replace("\\\n", " ")
    runs = [line for line in text.splitlines()
            if line.startswith("RUN ") and "smartha-agent" in line]
    assert len(runs) == 1, runs
    return runs[0]


def test_ss_tag_names_a_release_once():
    tags = re.findall(r"^ARG SS_TAG=(\S+)$", _text(), re.MULTILINE)
    assert len(tags) == 1, tags
    assert re.fullmatch(r"v\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", tags[0]), tags[0]


def test_agent_is_downloaded_from_the_ss_tag_release():
    run = _agent_run()
    urls = re.findall(r'RELEASE_URL="([^"]+)"', run)
    assert urls == [
        "https://github.com/DAB-LABS/smart-sniffer/releases/download/"
        "${SS_TAG}/smartha-agent-linux-${GO_ARCH}"
    ]
    assert 'curl -fSL -o /usr/bin/smartha-agent "${RELEASE_URL}"' in run


def test_nothing_comes_from_latest():
    assert "releases/latest" not in _text()


def test_ss_tag_is_set_before_the_agent_download():
    text = _text()
    assert text.index("ARG SS_TAG=") < text.index('RELEASE_URL="')


def test_rules_come_from_the_same_tag():
    assert re.search(
        r"^ARG SS_TARBALL=https://github\.com/DAB-LABS/smart-sniffer/archive/"
        r"\$\{SS_TAG\}\.tar\.gz$", _text(), re.MULTILINE)
