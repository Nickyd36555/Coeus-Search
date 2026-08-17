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
