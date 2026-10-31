#!/usr/bin/env python3
"""SMART Sniffer App: the passwords the App suggests for its agent.

A suggestion is three words and a number, joined with "-": an adjective, a
spy word and an animal, in that order, with a 4-digit zero-padded number
put in one of the four places at random:

  sneaky-trenchcoat-falcon-4821
  4821-sneaky-trenchcoat-falcon
  sneaky-4821-trenchcoat-falcon

The words come from passwords.txt beside this file. Every choice uses the
secrets module (secrets.choice, secrets.randbelow), never random.

Strength, from the lists as shipped: 465 adjectives x 388 spy words x 369
animals x 10,000 numbers x 4 places = 2,662,999,200,000 passwords, about
2.7e12 (41 bits). test_passwords.py fails when a list changes size without
this count being brought up to date.

Run as a script on the App's first start ever (run.sh):

  python3 /opt/web/passwords.py --first-start /data

When /data holds nothing an earlier version left (only the options.json the
Supervisor writes) and the App's token option is empty, it makes a password,
saves it as the token option through the Supervisor (every other option
kept as it is) and prints it on stdout for run.sh. Anything else prints
nothing: an upgrade keeps its empty token. Messages go to stderr and never
hold the password.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

WORDS_FILE = Path(__file__).resolve().parent / "passwords.txt"
SECTIONS = ("adjectives", "spywords", "animals")
WORD_RE = re.compile(r"[a-z]+")
# What a new install's /data may hold before the App's first start: the
# options the Supervisor writes for every app.
FIRST_START_FILES = frozenset({"options.json"})
SUPERVISOR_URL = "http://supervisor"
SUPERVISOR_TIMEOUT = 10  # seconds


def read_lists(path: Path = WORDS_FILE) -> dict[str, list[str]]:
    """The sections of a word file, in file order: "[name]" lines open a
    section, "#" lines are comments, words are separated by white space."""
    lists: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = lists.setdefault(line[1:-1], [])
            continue
        if current is None:
            raise ValueError(f"{path.name}: words before the first section")
        current.extend(line.split())
    return lists


@functools.lru_cache(maxsize=1)
def word_lists() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(adjectives, spy words, animals) from passwords.txt."""
    lists = read_lists()
    return tuple(tuple(lists.get(name) or ()) for name in SECTIONS)  # type: ignore[return-value]


def generate() -> str:
    """One suggestion: adjective-spyword-animal with the number in one of
    the four places."""
    adjectives, spywords, animals = word_lists()
    parts = [secrets.choice(adjectives), secrets.choice(spywords), secrets.choice(animals)]
    parts.insert(secrets.randbelow(4), f"{secrets.randbelow(10000):04d}")
    return "-".join(parts)


def combinations() -> int:
    """How many different suggestions the shipped lists can make."""
    adjectives, spywords, animals = word_lists()
    return len(adjectives) * len(spywords) * len(animals) * 10000 * 4


# ── The App's first start ───────────────────────────────────────────────────

def is_first_start(data_dir: str) -> bool:
    """True when the App has never run here: data_dir holds nothing but the
    Supervisor's options.json. Every App version that ran leaves files of its
    own (agent-config.yaml from 0.3.1 on, app-state.json, drivedb.h), so an
    upgrade is never a first start. An unreadable directory is not one
    either."""
    try:
        names = set(os.listdir(data_dir))
    except OSError:
        return False
    return names <= FIRST_START_FILES


def _supervisor(method: str, path: str, token: str, body: object = None) -> tuple[int | None, object]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(SUPERVISOR_URL + path, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=SUPERVISOR_TIMEOUT) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return None, None
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def first_start(data_dir: str, supervisor_token: str) -> str:
    """The generated password, saved as the token option; "" when this is
    not a first start, a token is already set, or it could not be saved
    (the agent then starts without one, as before)."""
    if not is_first_start(data_dir):
        return ""
    if not supervisor_token:
        print("First start: no Supervisor access, so no password was made.", file=sys.stderr)
        return ""
    status, info = _supervisor("GET", "/addons/self/info", supervisor_token)
    data = info.get("data") if isinstance(info, dict) else None
    options = data.get("options") if isinstance(data, dict) else None
    if status != 200 or not isinstance(options, dict):
        print("First start: the App options could not be read, so no password was made.", file=sys.stderr)
        return ""
    if options.get("token"):
        return ""
    password = generate()
    status, _ = _supervisor("POST", "/addons/self/options", supervisor_token,
                            {"options": {**options, "token": password}})
    if status != 200:
        print(f"First start: the password could not be saved (HTTP {status}); "
              "the agent starts without one.", file=sys.stderr)
        return ""
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The App's password suggestions")
    parser.add_argument("--first-start", metavar="DATA_DIR",
                        help="on the App's first start, make and save a password and print it")
    args = parser.parse_args(argv)
    if args.first_start:
        password = first_start(args.first_start, os.environ.get("SUPERVISOR_TOKEN", ""))
        if password:
            print(password)
        return 0
    print(generate())
    return 0


if __name__ == "__main__":
    sys.exit(main())
