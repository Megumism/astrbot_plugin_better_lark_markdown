"""Tests for astrbot_better_lark_markdown."""

# ruff: noqa: E402, I001

import os
import sys
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ASTRBOT_ROOT = os.path.abspath(r"d:\Code\AstrBot")
for path in (ASTRBOT_ROOT, PLUGIN_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)


from astrbot.api.event import MessageChain  # noqa: E402
from astrbot.api.message_components import Plain  # noqa: E402
from astrbot.core.platform.sources.lark import lark_event as lark_event_mod  # noqa: E402
from main import (  # noqa: E402
    Main,
    _install_patch,
    _remove_patch,
    _split_text_by_markdown_table,
)


def _make_plugin(mode: str = "direct"):
    return Main(SimpleNamespace(), {"card_send_mode": mode})


@pytest.mark.asyncio
async def test_split_text_by_markdown_table_returns_three_segments():
    text = "A\n| col1 | col2 |\n| --- | --- |\n| 1 | 2 |\nB"

    assert _split_text_by_markdown_table(text) == [
        "A",
        "| col1 | col2 |\n| --- | --- |\n| 1 | 2 |",
        "B",
    ]


@pytest.mark.asyncio
async def test_split_text_by_markdown_table_handles_multiple_tables():
    """Test that multiple tables in one message are split correctly."""
    text = (
        "First part\n"
        "| col1 | col2 |\n"
        "| --- | --- |\n"
        "| a | b |\n"
        "Middle part\n"
        "| col3 | col4 |\n"
        "| --- | --- |\n"
        "| c | d |\n"
        "Last part"
    )

    result = _split_text_by_markdown_table(text)
    assert len(result) == 5  # First, Table1, Middle, Table2, Last

    assert result[0] == "First part"
    assert "| col1 | col2 |" in result[1]
    assert result[2] == "Middle part"
    assert "| col3 | col4 |" in result[3]
    assert result[4] == "Last part"


@pytest.mark.asyncio
async def test_install_patch_splits_plain_text_table_messages(monkeypatch):
    calls: list[str] = []
    card_calls: list[tuple[str, str | None, str | None]] = []

    async def fake_send_message_chain(
        message_chain,
        lark_client,
        reply_message_id=None,
        receive_id=None,
        receive_id_type=None,
    ):
        del lark_client, reply_message_id, receive_id, receive_id_type
        calls.append(
            "".join(
                comp.text for comp in message_chain.chain if isinstance(comp, Plain)
            )
        )

    async def fake_send_interactive_card(
        card_json,
        lark_client,
        reply_message_id=None,
        receive_id=None,
        receive_id_type=None,
    ):
        del lark_client
        # Extract markdown content from card
        for element in card_json.get("body", {}).get("elements", []):
            if element.get("tag") == "markdown":
                card_calls.append(
                    (element.get("content", ""), reply_message_id, receive_id)
                )
        return True

    monkeypatch.setattr(
        lark_event_mod.LarkMessageEvent,
        "send_message_chain",
        fake_send_message_chain,
        raising=False,
    )
    monkeypatch.setattr(
        lark_event_mod.LarkMessageEvent,
        "_send_interactive_card",
        fake_send_interactive_card,
        raising=False,
    )
    monkeypatch.setattr(
        "main._original_send_message_chain",
        None,
        raising=False,
    )

    try:
        _make_plugin("direct")
        _install_patch()

        await lark_event_mod.LarkMessageEvent.send_message_chain(
            MessageChain(
                chain=[Plain("A\n| col1 | col2 |\n| --- | --- |\n| 1 | 2 |\nB")]
            ),
            SimpleNamespace(im=SimpleNamespace()),
            reply_message_id="mid",
            receive_id="chat-1",
            receive_id_type="chat_id",
        )

        # Check that text segments were sent as plain messages
        assert "A" in calls
        assert "B" in calls
        # Check that table was sent as a card
        assert len(card_calls) == 1
        assert "| col1 | col2 |" in card_calls[0][0]
        assert card_calls[0][1] is None
        assert card_calls[0][2] == "chat-1"
    finally:
        _remove_patch()


@pytest.mark.asyncio
async def test_install_patch_keeps_plain_text_without_table_as_is(monkeypatch):
    calls: list[str] = []

    async def fake_send_message_chain(
        message_chain,
        lark_client,
        reply_message_id=None,
        receive_id=None,
        receive_id_type=None,
    ):
        del lark_client, reply_message_id, receive_id, receive_id_type
        calls.append(
            "".join(
                comp.text for comp in message_chain.chain if isinstance(comp, Plain)
            )
        )

    monkeypatch.setattr(
        lark_event_mod.LarkMessageEvent,
        "send_message_chain",
        fake_send_message_chain,
        raising=False,
    )
    monkeypatch.setattr(
        "main._original_send_message_chain",
        None,
        raising=False,
    )

    try:
        _make_plugin("direct")
        _install_patch()

        await lark_event_mod.LarkMessageEvent.send_message_chain(
            MessageChain(chain=[Plain("hello world")]),
            SimpleNamespace(im=SimpleNamespace()),
            reply_message_id="mid",
        )

        assert calls == ["hello world"]
    finally:
        _remove_patch()


@pytest.mark.asyncio
async def test_install_patch_uses_reply_mode_when_configured(monkeypatch):
    card_calls: list[tuple[str, str | None, str | None, str | None]] = []

    async def fake_send_message_chain(
        message_chain,
        lark_client,
        reply_message_id=None,
        receive_id=None,
        receive_id_type=None,
    ):
        del lark_client, reply_message_id, receive_id, receive_id_type

    async def fake_send_interactive_card(
        card_json,
        lark_client,
        reply_message_id=None,
        receive_id=None,
        receive_id_type=None,
    ):
        del lark_client
        for element in card_json.get("body", {}).get("elements", []):
            if element.get("tag") == "markdown":
                card_calls.append(
                    (
                        element.get("content", ""),
                        reply_message_id,
                        receive_id,
                        receive_id_type,
                    )
                )
        return True

    monkeypatch.setattr(
        lark_event_mod.LarkMessageEvent,
        "send_message_chain",
        fake_send_message_chain,
        raising=False,
    )
    monkeypatch.setattr(
        lark_event_mod.LarkMessageEvent,
        "_send_interactive_card",
        fake_send_interactive_card,
        raising=False,
    )
    monkeypatch.setattr(
        "main._original_send_message_chain",
        None,
        raising=False,
    )

    try:
        _make_plugin("reply")
        _install_patch()

        await lark_event_mod.LarkMessageEvent.send_message_chain(
            MessageChain(
                chain=[Plain("A\n| col1 | col2 |\n| --- | --- |\n| 1 | 2 |\nB")]
            ),
            SimpleNamespace(im=SimpleNamespace()),
            reply_message_id="mid",
            receive_id="chat-1",
            receive_id_type="chat_id",
        )

        assert len(card_calls) == 1
        assert "| col1 | col2 |" in card_calls[0][0]
        assert card_calls[0][1] == "mid"
        assert card_calls[0][2] == "chat-1"
        assert card_calls[0][3] == "chat_id"
    finally:
        _remove_patch()
        _make_plugin("direct")
