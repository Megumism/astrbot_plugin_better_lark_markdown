from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig

_original_send_message_chain: Callable[..., Any] | None = None
_patch_token = object()
_original_send_method: Callable[..., Any] | None = None

_card_send_mode = "reply"
_last_reply_msg_id_in_chat: dict[tuple[str, str], str] = {}


def _normalize_card_send_mode(mode: Any) -> str:
    normalized = str(mode).strip().lower()
    if normalized in {"direct", "reply", "auto"}:
        return normalized
    return "reply"


def _set_card_send_mode(config: AstrBotConfig | dict[str, Any] | None) -> None:
    global _card_send_mode

    if config is None:
        _card_send_mode = "reply"
        return

    if hasattr(config, "get"):
        raw_mode = config.get("card_send_mode", "reply")
    else:
        raw_mode = getattr(config, "card_send_mode", "reply")

    _card_send_mode = _normalize_card_send_mode(raw_mode)
    if _card_send_mode != raw_mode:
        logger.debug(
            "[card_send_mode] Requested mode=%s, normalized to=%s",
            raw_mode,
            _card_send_mode,
        )


def _resolve_send_targets(
    reply_message_id: str | None,
    receive_id: str | None,
    receive_id_type: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve send targets according to configured card_send_mode.
    Applies for both plain text and card sends."""
    if _card_send_mode == "reply":
        return reply_message_id, receive_id, receive_id_type

    if _card_send_mode == "auto":
        if reply_message_id and receive_id and receive_id_type:
            chat_key = (receive_id, receive_id_type)
            last_replied = _last_reply_msg_id_in_chat.get(chat_key)
            if last_replied != reply_message_id:
                _last_reply_msg_id_in_chat[chat_key] = reply_message_id
                # we SHOULD reply
                return reply_message_id, receive_id, receive_id_type
            else:
                # same as last time, so fallback to direct (no reply_message_id)
                return None, receive_id, receive_id_type
        return reply_message_id, receive_id, receive_id_type

    if _card_send_mode == "direct":
        if receive_id and receive_id_type:
            return None, receive_id, receive_id_type
        if reply_message_id:
            logger.debug(
                "[card_send_mode] Direct mode missing receive_id; falling back to reply"
            )
        return reply_message_id, receive_id, receive_id_type

    return reply_message_id, receive_id, receive_id_type


def _derive_receive_from_message_obj(message_obj: Any) -> tuple[str | None, str | None]:
    """Try to derive a receive_id and receive_id_type from an AstrBotMessage object."""
    # Group message -> chat_id
    try:
        group_id = getattr(message_obj, "group_id", None)
        if group_id:
            return group_id, "chat_id"
    except Exception:
        pass

    # Private message -> open_id (sender.user_id)
    try:
        sender = getattr(message_obj, "sender", None)
        if sender and getattr(sender, "user_id", None):
            return getattr(sender, "user_id"), "open_id"
    except Exception:
        pass

    return None, None


def _get_table_row_cells(line: str) -> list[str]:
    """Extract cells from a markdown table row, handling outer pipes."""
    stripped = line.strip()
    if not stripped or "|" not in stripped:
        return []

    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]

    return [cell.strip() for cell in stripped.split("|")]


def _is_markdown_table_separator(line: str) -> bool:
    """Return True when a line looks like a markdown table separator row."""
    cells = _get_table_row_cells(line)
    if len(cells) < 2:
        return False

    return all(re.fullmatch(r":?-+:?", cell) is not None for cell in cells)


def _is_markdown_table_segment(text: str) -> bool:
    """Check if a segment is purely a markdown table."""

    lines = text.strip().split("\n")
    if len(lines) < 3:
        return False

    # Second line should be separator
    if not _is_markdown_table_separator(lines[1]):
        return False

    header_cells = _get_table_row_cells(lines[0])
    sep_cells = _get_table_row_cells(lines[1])
    if len(header_cells) < 2 or len(header_cells) != len(sep_cells):
        return False

    # All lines should have pipes (table structure)
    return all("|" in line for line in lines)


def _build_table_card(table_markdown: str) -> dict:
    """Build a Lark card JSON 2.0 with markdown table as the only content.

    Args:
        table_markdown: Markdown table text

    Returns:
        Card JSON structure
    """

    logger.debug("[table_card] Building card for markdown table")

    card_json = {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": table_markdown,
                    "text_align": "left",
                }
            ],
        },
    }

    logger.debug(f"[table_card] Card structure built: {len(card_json)} keys")
    return card_json


async def _send_table_card(
    table_markdown: str,
    lark_client: Any,
    reply_message_id: str | None = None,
    receive_id: str | None = None,
    receive_id_type: str | None = None,
) -> bool:
    """Send a markdown table as a Lark interactive card.

    Args:
        table_markdown: Markdown table text
        lark_client: Lark client instance
        reply_message_id: Reply message ID (optional)
        receive_id: Receiver ID (optional)
        receive_id_type: Receiver ID type (optional)

    Returns:
        True if card sent successfully, False otherwise
    """

    try:
        from astrbot.core.platform.sources.lark.lark_event import (
            LarkMessageEvent,
        )
    except ImportError:
        logger.warning("[table_card] Failed to import LarkMessageEvent")
        return False

    card_json = _build_table_card(table_markdown)

    logger.debug(
        "[table_card] Sending table card with mode=%s, reply_message_id=%s, receive_id=%s, receive_id_type=%s",
        _card_send_mode,
        reply_message_id,
        receive_id,
        receive_id_type,
    )

    return await LarkMessageEvent._send_interactive_card(
        card_json,
        lark_client,
        reply_message_id=reply_message_id,
        receive_id=receive_id,
        receive_id_type=receive_id_type,
    )


def _split_text_by_markdown_table(text: str) -> list[str]:
    """Split text by ALL markdown tables, returning alternating prefix/table/suffix segments."""

    lines = text.splitlines()
    tables = []  # List of (start_line_index, end_line_index)

    logger.debug(f"[split_text] Processing {len(lines)} lines for markdown tables")

    # Find all markdown tables in the text
    for index in range(len(lines) - 1):
        if "|" not in lines[index]:
            continue
        if not _is_markdown_table_separator(lines[index + 1]):
            continue

        table_start = index
        table_end = index + 2

        while table_end < len(lines):
            current_line = lines[table_end]
            if not current_line.strip() or "|" not in current_line:
                break
            table_end += 1

        logger.debug(f"[split_text] Found table at lines {table_start}-{table_end - 1}")
        tables.append((table_start, table_end))

    if not tables:
        logger.debug("[split_text] No markdown tables found")
        return [text]

    logger.debug(f"[split_text] Found {len(tables)} table(s) total")

    # Build segments by walking through tables
    segments = []
    current_pos = 0

    for table_idx, (table_start, table_end) in enumerate(tables):
        # Add prefix segment (text before this table)
        if current_pos < table_start:
            prefix = "\n".join(lines[current_pos:table_start]).strip("\n")
            if prefix:
                logger.debug(
                    f"[split_text] Adding prefix before table {table_idx} (lines {current_pos}-{table_start - 1})"
                )
                segments.append(prefix)

        # Add table segment
        table = "\n".join(lines[table_start:table_end]).strip("\n")
        logger.debug(
            f"[split_text] Adding table {table_idx} (lines {table_start}-{table_end - 1})"
        )
        segments.append(table)

        current_pos = table_end

    # Add remaining text after last table (if any)
    if current_pos < len(lines):
        suffix = "\n".join(lines[current_pos:]).strip("\n")
        if suffix:
            logger.debug(
                f"[split_text] Adding suffix after last table (lines {current_pos}-{len(lines) - 1})"
            )
            segments.append(suffix)

    logger.debug(f"[split_text] Final result: {len(segments)} segments")
    return segments or [text]


def _should_split_message_chain(message_chain: MessageChain) -> bool:
    """Only split plain-text message chains that contain a markdown table."""

    if not message_chain.chain:
        logger.debug("[should_split] Empty message chain")
        return False

    if not all(isinstance(comp, Plain) for comp in message_chain.chain):
        logger.debug("[should_split] Message chain contains non-Plain components, skip")
        return False

    plain_text = "".join(
        comp.text for comp in message_chain.chain if isinstance(comp, Plain)
    )
    segments = _split_text_by_markdown_table(plain_text)
    should_split = len(segments) > 1
    logger.debug(
        f"[should_split] Text length={len(plain_text)}, segments={len(segments)}, should_split={should_split}"
    )
    return should_split


def _patch_send_message_chain(
    original_send_message_chain: Callable[..., Any],
):
    async def patched_send_message_chain(
        message_chain: MessageChain,
        lark_client: Any,
        reply_message_id: str | None = None,
        receive_id: str | None = None,
        receive_id_type: str | None = None,
    ):
        logger.debug("[send_patch] Intercepted send_message_chain call")

        if not _should_split_message_chain(message_chain):
            logger.debug("[send_patch] No table splitting needed, pass through")
            resolved_reply_id, resolved_receive_id, resolved_receive_type = (
                _resolve_send_targets(reply_message_id, receive_id, receive_id_type)
            )
            return await original_send_message_chain(
                message_chain,
                lark_client,
                reply_message_id=resolved_reply_id,
                receive_id=resolved_receive_id,
                receive_id_type=resolved_receive_type,
            )

        plain_text = "".join(
            comp.text for comp in message_chain.chain if isinstance(comp, Plain)
        )
        segments = _split_text_by_markdown_table(plain_text)

        logger.info(
            "[send_patch] Detected markdown table(s) in outgoing message, splitting into %d segments",
            len(segments),
        )

        for idx, segment in enumerate(segments, 1):
            is_table = _is_markdown_table_segment(segment)
            segment_type = "table" if is_table else "text"
            logger.debug(
                f"[send_patch] Sending segment {idx}/{len(segments)}: {len(segment)} chars ({segment_type})"
            )

            resolved_reply_id, resolved_receive_id, resolved_receive_type = (
                _resolve_send_targets(reply_message_id, receive_id, receive_id_type)
            )

            if is_table:
                logger.debug(f"[send_patch] Segment {idx} is a table, sending as card")
                await _send_table_card(
                    segment,
                    lark_client,
                    reply_message_id=resolved_reply_id,
                    receive_id=resolved_receive_id,
                    receive_id_type=resolved_receive_type,
                )
            else:
                logger.debug(
                    f"[send_patch] Segment {idx} is text, sending as plain message"
                )
                await original_send_message_chain(
                    MessageChain(chain=[Plain(segment)]),
                    lark_client,
                    reply_message_id=resolved_reply_id,
                    receive_id=resolved_receive_id,
                    receive_id_type=resolved_receive_type,
                )

    return patched_send_message_chain


def _install_patch() -> None:
    """Patch Lark send_message_chain so markdown tables are sent in segments."""

    global _original_send_message_chain

    try:
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
    except ImportError as exc:  # noqa: BLE001
        logger.warning("Failed to import LarkMessageEvent, skip patch: %s", exc)
        return

    current_id = getattr(LarkMessageEvent, "_markdown_table_patch_id", None)
    if current_id is _patch_token:
        logger.debug("Markdown table patch already installed.")
        return
    if current_id is not None and current_id is not _patch_token:
        logger.warning(
            "Another plugin seems to have patched LarkMessageEvent.send_message_chain; skip.",
        )
        return

    if _original_send_message_chain is None:
        _original_send_message_chain = LarkMessageEvent.send_message_chain

    setattr(LarkMessageEvent, "_markdown_table_patch_id", _patch_token)
    LarkMessageEvent.send_message_chain = staticmethod(
        _patch_send_message_chain(_original_send_message_chain)
    )
    # Also patch the instance `send` method so we can derive receive_id from
    # the message object for reply scenarios. This lets "direct" mode work
    # for normal reply flows by passing receive_id to send_message_chain.
    global _original_send_method

    if not hasattr(LarkMessageEvent, "_markdown_table_send_patch_id"):
        _original_send_method = LarkMessageEvent.send

        async def _patched_send(self, message: Any) -> None:
            # Try to derive receive target from message object
            derived_receive_id, derived_receive_type = _derive_receive_from_message_obj(
                getattr(self, "message_obj", None)
            )

            await LarkMessageEvent.send_message_chain(
                message,
                self.bot,
                reply_message_id=getattr(self, "message_obj", None)
                and getattr(self.message_obj, "message_id", None),
                receive_id=derived_receive_id,
                receive_id_type=derived_receive_type,
            )
            if _original_send_method is not None:
                await _original_send_method(self, message)

        setattr(LarkMessageEvent, "_markdown_table_send_patch_id", _patch_token)
        LarkMessageEvent.send = _patched_send
    logger.info("Markdown table split patch installed.")


def _remove_patch() -> None:
    """Restore the original send_message_chain implementation."""

    global _original_send_message_chain

    if _original_send_message_chain is None:
        return

    try:
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
    except ImportError:
        _original_send_message_chain = None
        return

    current_id = getattr(LarkMessageEvent, "_markdown_table_patch_id", None)
    if current_id is _patch_token:
        LarkMessageEvent.send_message_chain = staticmethod(_original_send_message_chain)
        delattr(LarkMessageEvent, "_markdown_table_patch_id")
        logger.info("Markdown table split patch removed.")

    # Restore patched instance send method if we replaced it
    global _original_send_method
    send_patch_id = getattr(LarkMessageEvent, "_markdown_table_send_patch_id", None)
    if send_patch_id is _patch_token and _original_send_method is not None:
        LarkMessageEvent.send = _original_send_method
        try:
            delattr(LarkMessageEvent, "_markdown_table_send_patch_id")
        except Exception:
            pass
        logger.info("Markdown table send-instance patch removed.")

    _original_send_message_chain = None
    _original_send_method = None


@register(
    "astrbot_better_lark_markdown",
    "megumism",
    "Split markdown table messages into separate segments and render as cards.",
    "1.1.0",
)
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        _set_card_send_mode(config)
        logger.info("Markdown table card send mode set to %s.", _card_send_mode)

    async def initialize(self):
        _install_patch()

    async def terminate(self):
        _remove_patch()
