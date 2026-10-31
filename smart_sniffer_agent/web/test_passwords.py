"""Tests for passwords.py: the suggestions, the word lists, and the App's
first start (a new install gets a password, an upgrade keeps none).

Run with:
  python3 -m pytest smart_sniffer_agent/web/test_passwords.py
"""

from __future__ import annotations

import json
import re
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import passwords

ADJECTIVES, SPYWORDS, ANIMALS = passwords.word_lists()
PATTERN = re.compile(r"^(?:\d{4}-)?[a-z]+-(?:\d{4}-)?[a-z]+-(?:\d{4}-)?[a-z]+(?:-\d{4})?$")


def split(password: str) -> tuple[list[str], str, int]:
    """(the three words in order, the number, its place 0..3)."""
    parts = password.split("-")
    place = next(i for i, p in enumerate(parts) if p.isdigit())
    number = parts.pop(place)
    return parts, number, place


# ── Suggestions ─────────────────────────────────────────────────────────────

def test_a_suggestion_is_three_words_and_a_four_digit_number():
    for _ in range(500):
        password = passwords.generate()
        assert PATTERN.fullmatch(password), password
        parts = password.split("-")
        assert len(parts) == 4 and sum(p.isdigit() for p in parts) == 1
        words, number, _ = split(password)
        assert len(number) == 4 and number.isdigit()
        assert words[0] in ADJECTIVES and words[1] in SPYWORDS and words[2] in ANIMALS


def test_the_number_goes_in_every_place_and_is_zero_padded(monkeypatch):
    places = Counter(split(passwords.generate())[2] for _ in range(2000))
    assert set(places) == {0, 1, 2, 3}
    assert min(places.values()) > 300  # each place about a quarter of the time
    monkeypatch.setattr(passwords.secrets, "randbelow", lambda n: 7 if n == 10000 else 0)
    assert passwords.generate().startswith("0007-")


def test_suggestions_vary():
    assert len({passwords.generate() for _ in range(200)}) == 200


def test_only_the_secrets_module_picks():
    source = Path(passwords.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import random|from random)", source, re.M)
    assert "secrets.choice" in source and "secrets.randbelow" in source


# ── The word lists ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("words", [ADJECTIVES, SPYWORDS, ANIMALS], ids=passwords.SECTIONS)
def test_each_list_follows_the_rules(words):
    assert len(words) >= 256
    assert all(re.fullmatch(r"[a-z]+", w) for w in words), [w for w in words if not re.fullmatch(r"[a-z]+", w)]
    assert len(set(words)) == len(words), [w for w, n in Counter(words).items() if n > 1]
    assert not any("-" in w or " " in w for w in words)


def test_the_file_has_the_three_sections_in_order():
    assert list(passwords.read_lists()) == list(passwords.SECTIONS)


def test_the_documented_strength_matches_the_lists():
    count = passwords.combinations()
    assert count == len(ADJECTIVES) * len(SPYWORDS) * len(ANIMALS) * 40000
    assert f"{count:,}" in Path(passwords.__file__).read_text(encoding="utf-8")
    assert count > 1e12


# ── The App's first start ───────────────────────────────────────────────────

class FakeSupervisor:
    """/addons/self/info and /addons/self/options, as the Supervisor answers."""

    def __init__(self, options: dict, save_status: int = 200) -> None:
        self.options = options
        self.save_status = save_status
        self.saved: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                assert self.headers["Authorization"] == "Bearer sv-token"
                self._reply(200, {"result": "ok", "data": {"options": fake.options}})

            def do_POST(self):
                assert self.path == "/addons/self/options"
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.saved.append(body)
                if fake.save_status == 200:
                    fake.options = body["options"]
                self._reply(fake.save_status, {"result": "ok", "data": {}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def supervisor(monkeypatch):
    made: list[FakeSupervisor] = []

    def make(options: dict, save_status: int = 200) -> FakeSupervisor:
        fake = FakeSupervisor(options, save_status)
        monkeypatch.setattr(passwords, "SUPERVISOR_URL", fake.url)
        made.append(fake)
        return fake

    yield make
    for fake in made:
        fake.close()


OPTIONS = {"port": 9099, "token": "", "scan_interval": 60, "hosts": [], "card_order": [],
           "hidden_cards": [], "thresholds": [{"card": "self|S1", "label": "Reallocated Sectors", "value": 3}],
           "card_names": [], "host_names": []}


def new_install(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "options.json").write_text(json.dumps(OPTIONS))
    return data


def test_a_new_install_gets_a_password_saved_with_every_other_option(tmp_path, supervisor):
    sv = supervisor(dict(OPTIONS))
    password = passwords.first_start(str(new_install(tmp_path)), "sv-token")
    assert PATTERN.fullmatch(password)
    assert sv.saved == [{"options": {**OPTIONS, "token": password}}]


def test_an_empty_data_dir_is_a_first_start_too(tmp_path, supervisor):
    supervisor(dict(OPTIONS))
    data = tmp_path / "data"
    data.mkdir()
    assert passwords.first_start(str(data), "sv-token")


@pytest.mark.parametrize("left", ["agent-config.yaml", "app-state.json", "drivedb.h", "fleet-cache.json"])
def test_an_upgrade_keeps_its_empty_token(tmp_path, supervisor, left):
    """0.4.x (and 0.3.x) leave their own files in /data: nothing is made."""
    sv = supervisor(dict(OPTIONS))
    data = new_install(tmp_path)
    (data / left).write_text("{}")
    assert passwords.first_start(str(data), "sv-token") == ""
    assert sv.saved == []


def test_a_token_already_set_is_kept(tmp_path, supervisor):
    sv = supervisor({**OPTIONS, "token": "my-own"})
    assert passwords.first_start(str(new_install(tmp_path)), "sv-token") == ""
    assert sv.saved == []


def test_a_password_that_could_not_be_saved_is_not_used(tmp_path, supervisor, capsys):
    sv = supervisor(dict(OPTIONS), save_status=400)
    assert passwords.first_start(str(new_install(tmp_path)), "sv-token") == ""
    assert len(sv.saved) == 1
    err = capsys.readouterr().err
    assert "could not be saved" in err and sv.saved[0]["options"]["token"] not in err


def test_no_supervisor_or_no_data_dir_makes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(passwords, "SUPERVISOR_URL", "http://127.0.0.1:9")
    assert passwords.first_start(str(new_install(tmp_path)), "") == ""
    assert passwords.first_start(str(new_install(tmp_path / "x")), "sv-token") == ""
    assert passwords.first_start(str(tmp_path / "missing"), "sv-token") == ""


def test_the_script_prints_the_password_only(tmp_path, supervisor, monkeypatch, capsys):
    supervisor(dict(OPTIONS))
    monkeypatch.setenv("SUPERVISOR_TOKEN", "sv-token")
    assert passwords.main(["--first-start", str(new_install(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert PATTERN.fullmatch(out.strip()) and out.count("\n") == 1
    data = new_install(tmp_path / "again")
    (data / "agent-config.yaml").write_text("{}")
    assert passwords.main(["--first-start", str(data)]) == 0
    assert capsys.readouterr().out == ""
