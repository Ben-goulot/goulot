"""CLI behaviour that does not need a real Terminal: alert setup and `open --print`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from goulot.cli import main as cli
from goulot.db.connection import Database, get_setting, set_setting
from goulot.security.secrets import get_secret, set_secret

TOKEN = "123456789:AAFakeTelegramTokenForTestsOnly_abcdefghij"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "goulot.db")
    database.migrate()
    return database


def _answers(monkeypatch: pytest.MonkeyPatch, typed: list[str], hidden: list[str]) -> None:
    typed_it: Iterator[str] = iter(typed)
    hidden_it: Iterator[str] = iter(hidden)
    monkeypatch.setattr("builtins.input", lambda _="": next(typed_it))
    monkeypatch.setattr("getpass.getpass", lambda _="": next(hidden_it))


def test_setup_keeps_existing_telegram_token(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    set_secret("telegram_bot", TOKEN)
    with db.transaction() as conn:
        set_setting(conn, "telegram_chat_id", "42")
    _answers(monkeypatch, ["t", "", ""], [])  # Telegram, keep chat id, do not replace the token
    cli._setup_alerts(db)
    assert get_secret("telegram_bot") == TOKEN
    with db.connect() as conn:
        assert get_setting(conn, "alert_channel") == "telegram"
        assert get_setting(conn, "telegram_chat_id") == "42"


def test_setup_asks_telegram_token_when_absent(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _answers(monkeypatch, ["t", "42"], [TOKEN])
    cli._setup_alerts(db)
    assert get_secret("telegram_bot") == TOKEN


def test_setup_secrets_skips_alert_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    labels: list[str] = []

    def record(label: str) -> str:
        labels.append(label)
        return ""

    monkeypatch.setattr(cli, "_ask_secret", record)
    cli._setup_secrets(None)  # type: ignore[arg-type]  # settings only used to test a typed key
    assert labels
    assert not any("Telegram" in label or "SMTP" in label for label in labels)


def test_open_print_does_not_launch_browser(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    cli._open_url("http://localhost:8742/auth#t=abc", print_only=True)
    assert not opened
    assert "http://localhost:8742/auth#t=abc" in capsys.readouterr().out
    cli._open_url("http://localhost:8742/auth#t=def")
    assert opened == ["http://localhost:8742/auth#t=def"]


def test_open_and_add_passkey_accept_print() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["open", "--print"]).print_only
    assert not parser.parse_args(["open"]).print_only
    assert parser.parse_args(["vault", "add-passkey", "--print"]).print_only


def test_setup_tests_a_price_key_and_never_prints_it(monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    import httpx2

    from goulot.net.allowlist import Allowlist
    from goulot.net.client import build_client

    key = "FAKE" + "k" * 36
    answers = iter([key])

    def ask(label: str) -> str:
        return next(answers) if label.startswith("Clé EIA") else ""

    def refusing(*_: object, **__: object) -> httpx2.Client:
        return build_client(Allowlist(set()), transport=httpx2.MockTransport(lambda r: httpx2.Response(
            403, json={"code": "API_KEY_INVALID", "message": f"invalid api_key {key}"})))

    monkeypatch.setattr(cli, "_ask_secret", ask)
    monkeypatch.setattr("goulot.net.client.build_client", refusing)
    monkeypatch.setattr("goulot.net.allowlist.load_allowlist", lambda *_: Allowlist(set()))
    from goulot.settings import Settings

    cli._setup_secrets(Settings())
    out = capsys.readouterr().out
    assert "clé refusée ou inactive" in out and "API_KEY_INVALID" in out and "40 caractères" in out
    assert key not in out and get_secret("eia") == key  # kept, so that the user can fix it by typing it again
