"""Telegram CLI-lane commands on this bot: the shared casino-shared commands, bound to this
bot's ctl and vault, owner only, never reading a secret.

Reference: wolfbet/tests/test_tg_lanes.py, test_tg_sessions.py (the full behaviour).
"""

import asyncio
import json
import types
from pathlib import Path

import pytest

import shared.core.vault as vault
import tg.handlers as h

OWNER = 4242
CTL = "/home/bot/.local/bin/stakectl"
LANES = {"v": 1, "lanes": [
    {"lane": 1, "alive": True, "paused": False, "state": {"key_ref": "default", "session_id": 9,
                                                          "profit": 0.5, "currency": "usdt"}},
    {"lane": 2, "alive": True, "paused": False, "state": {"key_ref": "default", "session_id": 3,
                                                          "profit": 0.0, "currency": "usdt"}},
]}
_TG = Path(h.__file__).resolve().parent


@pytest.fixture
def ctl(monkeypatch):
    calls = []
    reply = {"rc": 0, "out": json.dumps(LANES)}

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = json.dumps(LANES) if cmd[1:] == ["lanes", "--json"] else reply["out"]
        return types.SimpleNamespace(returncode=reply["rc"] if cmd[1:] != ["lanes", "--json"] else 0,
                                     stdout=out, stderr="")

    monkeypatch.setenv("TG_OWNER_ID", str(OWNER))
    monkeypatch.setattr(h, "_ctl_path", lambda: CTL)
    monkeypatch.setattr(h, "_key_label", lambda ref: ref)
    monkeypatch.setattr("subprocess.run", fake_run)
    return types.SimpleNamespace(calls=calls, reply=reply)


def _update(user_id=OWNER, args=()):
    sent = []

    async def reply_text(text, **kw):
        sent.append(text)

    upd = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=user_id),
                                message=types.SimpleNamespace(reply_text=reply_text))
    return upd, types.SimpleNamespace(args=list(args)), sent


def _tap(data, user_id=OWNER):
    q = types.SimpleNamespace(data=data, from_user=types.SimpleNamespace(id=user_id),
                              answers=[], edits=[])

    async def answer(text=None, **kw):
        q.answers.append(text)

    async def edit_message_text(text, **kw):
        q.edits.append((text, kw))

    q.answer, q.edit_message_text = answer, edit_message_text
    asyncio.run(h.callback_handler(types.SimpleNamespace(callback_query=q), None))
    return q


def test_another_chat_is_refused_on_commands_and_buttons(ctl):
    upd, context, sent = _update(OWNER + 1)
    asyncio.run(h.cmd_lanes(upd, context))
    assert sent == ["Only the owner can manage lanes."]
    q = _tap("lanego:stop:2", OWNER + 1)
    assert q.answers == ["Only the owner can manage lanes."] and ctl.calls == []


def test_lanes_and_a_lane_button_run_this_bots_ctl(ctl):
    upd, context, sent = _update()
    asyncio.run(h.cmd_lanes(upd, context))
    assert ctl.calls == [[CTL, "lanes", "--json"]]
    assert sent[0].startswith("Lane 1: running, key default, session 9")
    _tap("lanego:pause:2")
    assert [CTL, "pause", "2"] in ctl.calls


class _Vault:
    services = []

    def __init__(self, service):
        self.services.append(service)

    def keys(self):
        return [types.SimpleNamespace(key_id="friend", label="friend", last4="a1b2", kind="key"),
                types.SimpleNamespace(key_id="login", label="friend login", last4="9f3c", kind="account")]

    def secret(self, *a, **k):
        raise AssertionError("the picker must never read a secret")


def test_the_key_picker_lists_this_bots_saved_keys_by_label_only(ctl, monkeypatch):
    monkeypatch.setattr(vault, "Vault", _Vault)
    q = _tap("lane:key:2")
    labels = [b.text for row in q.edits[-1][1]["reply_markup"].inline_keyboard for b in row]
    assert _Vault.services[-1] == "stake"
    assert labels == ["friend login (...9f3c)", "Cancel"]


def test_the_bots_own_buttons_still_reach_its_code():
    q = types.SimpleNamespace(data="stop_session:1", from_user=types.SimpleNamespace(id=OWNER))
    assert asyncio.run(h.LANES.handle_callback(q)) is False


def test_the_lane_commands_are_registered_listed_and_in_help():
    bot = (_TG / "bot.py").read_text()
    for name in ("lanes", "laneadd", "lanerename", "sessions"):
        assert f'CommandHandler("{name}", cmd_{name})' in bot
        assert f'BotCommand("{name}",' in bot
    handlers = (_TG / "handlers.py").read_text()
    assert "*CLI lanes* (owner)" in handlers and "/sessions [lane]" in handlers
