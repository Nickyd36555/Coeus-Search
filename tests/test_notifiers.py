import pytest

from fbmarket.config import ConfigError, validate
from fbmarket.models import Listing
from fbmarket.notifiers import Dispatcher, build_notifiers
from fbmarket.notifiers.base import resolve_env


def test_env_vars_are_expanded(monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "secret-token")
    notifiers = build_notifiers(
        [{"type": "telegram", "bot_token": "${TG_TOKEN}", "chat_id": "42"}]
    )
    assert notifiers[0].config["bot_token"] == "secret-token"


def test_env_expansion_is_recursive(monkeypatch):
    monkeypatch.setenv("T", "v")
    assert resolve_env({"h": {"a": "${T}"}, "l": ["${T}"]}) == {"h": {"a": "v"}, "l": ["v"]}


def test_disabled_channels_are_skipped():
    notifiers = build_notifiers(
        [{"type": "console"}, {"type": "desktop", "enabled": False}]
    )
    assert [n.type_name for n in notifiers] == ["console"]


def test_unknown_channel_type_is_rejected():
    with pytest.raises(ValueError, match="unknown notifier type"):
        build_notifiers([{"type": "carrier-pigeon"}])


def test_missing_required_config_is_reported():
    notifier = build_notifiers([{"type": "telegram", "chat_id": "1"}])[0]
    with pytest.raises(ValueError, match="bot_token"):
        notifier.send("hi", [Listing(id="1", title="car")])


class Boom:
    type_name = "boom"

    def send(self, *a, **k):
        raise RuntimeError("down")


class Ok:
    type_name = "ok"

    def __init__(self):
        self.sent = 0

    def send(self, *a, **k):
        self.sent += 1


def test_one_dead_channel_does_not_block_the_others():
    good = Ok()
    dispatcher = Dispatcher([Boom(), good])
    assert dispatcher.send("s", [Listing(id="1", title="car")]) is True
    assert good.sent == 1


def test_all_channels_failing_reports_failure():
    assert Dispatcher([Boom()]).send("s", [Listing(id="1", title="car")]) is False


def test_ntfy_placeholder_topic_is_rejected():
    config = {
        "searches": [{"name": "a", "location": "x"}],
        "notify": {"channels": [{"type": "ntfy", "topic": "coeus-CHANGE-THIS-now"}]},
    }
    with pytest.raises(ConfigError, match="placeholder"):
        validate(config)


def test_disabled_placeholder_channel_is_not_checked():
    config = {
        "searches": [{"name": "a", "location": "x"}],
        "notify": {
            "channels": [{"type": "ntfy", "topic": "CHANGE-THIS", "enabled": False}]
        },
    }
    validate(config)  # must not raise


def test_dotenv_is_loaded_without_overriding_real_env(tmp_path, monkeypatch):
    from fbmarket.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "DISCORD_WEBHOOK_URL=https://example.test/hook\n"
        "export QUOTED='shhh'\n"
        "ALREADY_SET=from-file\n"
        "\n"
        "malformed line\n"
    )
    monkeypatch.setenv("ALREADY_SET", "from-shell")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)

    load_dotenv(env)

    import os

    assert os.environ["DISCORD_WEBHOOK_URL"] == "https://example.test/hook"
    assert os.environ["QUOTED"] == "shhh"
    # A real exported value must win over the file.
    assert os.environ["ALREADY_SET"] == "from-shell"


def test_missing_dotenv_is_not_an_error(tmp_path):
    from fbmarket.config import load_dotenv

    assert load_dotenv(tmp_path / "nope.env") == 0


def test_windows_bom_files_still_parse(tmp_path, monkeypatch):
    """Notepad and PowerShell's Out-File write UTF-8 with a BOM. Without
    utf-8-sig the first key becomes "﻿KEY" and silently never resolves."""
    import os

    import yaml

    from fbmarket.config import load_config, load_dotenv

    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfDISCORD_WEBHOOK_URL=https://example.test/hook\n")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    load_dotenv(env)
    assert os.environ["DISCORD_WEBHOOK_URL"] == "https://example.test/hook"

    config = {
        "database": str(tmp_path / "db.sqlite3"),
        "notify": {"channels": [{"type": "console"}]},
        "searches": [{"name": "a", "location": "slc"}],
    }
    path = tmp_path / "config.yaml"
    path.write_bytes(b"\xef\xbb\xbf" + yaml.safe_dump(config).encode())
    assert load_config(str(path))["searches"][0]["name"] == "a"


def _channel_config(tmp_path):
    import yaml

    config = {
        "database": str(tmp_path / "db.sqlite3"),
        "notify": {
            "channels": [
                {"type": "ntfy", "enabled": False, "topic": "mine"},
                {"type": "discord", "enabled": False, "webhook_url": "x"},
                {"type": "email", "enabled": False},
            ]
        },
        "searches": [{"name": "a", "location": "slc"}],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _states(path):
    import yaml

    return {
        c["type"]: c.get("enabled")
        for c in yaml.safe_load(path.read_text())["notify"]["channels"]
    }


def test_channel_command_enables_only_the_named_channel(tmp_path):
    from fbmarket.cli import main

    path = _channel_config(tmp_path)
    assert main(["-c", str(path), "channel", "discord", "--on"]) == 0

    states = _states(path)
    assert states["discord"] is True
    # The other channels share the same `enabled: false` line; a blunt
    # find-and-replace would switch them all on.
    assert states["ntfy"] is False
    assert states["email"] is False


def test_channel_command_disables(tmp_path):
    from fbmarket.cli import main

    path = _channel_config(tmp_path)
    main(["-c", str(path), "channel", "discord", "--on"])
    assert main(["-c", str(path), "channel", "discord", "--off"]) == 0
    assert _states(path)["discord"] is False


def test_channel_command_rejects_unknown_channel(tmp_path, capsys):
    from fbmarket.cli import main

    path = _channel_config(tmp_path)
    assert main(["-c", str(path), "channel", "telegram", "--on"]) == 2
    assert "no 'telegram' channel" in capsys.readouterr().err


def test_channel_command_requires_a_direction(tmp_path):
    import pytest as _pytest

    from fbmarket.cli import main

    path = _channel_config(tmp_path)
    with _pytest.raises(SystemExit):
        main(["-c", str(path), "channel", "discord"])
