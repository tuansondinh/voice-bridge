"""claude.py — Claude Agent SDK client for the voice bridge.

Uses ``ClaudeSDKClient`` for persistent multi-turn conversations.
The SDK bundles the Claude Code CLI internally — no subprocess management
or JSON stdout parsing needed.

Authentication is via environment variables:
  - ``CLAUDE_CODE_OAUTH_TOKEN`` — Claude Max subscription (OAuth token)
  - ``ANTHROPIC_API_KEY`` — Anthropic API key (pay-per-use billing)

At least one of the above must be set before constructing ``ClaudeSession``.
"""

from __future__ import annotations

import os
import sys
import json
from typing import Any, AsyncGenerator

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        StreamEvent,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )
except ImportError:
    # SDK not installed — names are None so check_available() can return False
    # and tests can patch them in.  The server startup check will catch this
    # before ClaudeSession is actually constructed.
    AssistantMessage = None  # type: ignore[assignment,misc]
    ClaudeAgentOptions = None  # type: ignore[assignment,misc]
    ClaudeSDKClient = None  # type: ignore[assignment,misc]
    ResultMessage = None  # type: ignore[assignment,misc]
    StreamEvent = None  # type: ignore[assignment,misc]
    TextBlock = None  # type: ignore[assignment,misc]
    ThinkingBlock = None  # type: ignore[assignment,misc]
    ToolResultBlock = None  # type: ignore[assignment,misc]
    ToolUseBlock = None  # type: ignore[assignment,misc]
    UserMessage = None  # type: ignore[assignment,misc]


def _log(msg: str) -> None:
    print(f"[bridge-claude] {msg}", file=sys.stderr, flush=True)


def _check_auth() -> str:
    """Return the auth method in use, or raise RuntimeError if none set."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "CLAUDE_CODE_OAUTH_TOKEN"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY"
    raise RuntimeError(
        "No authentication credentials found. "
        "Set CLAUDE_CODE_OAUTH_TOKEN (Claude Max subscription) "
        "or ANTHROPIC_API_KEY (API billing) before starting voice-bridge. "
        "For Claude Max: run `claude setup-token` and export the token."
    )


class ClaudeSession:
    """Manages a multi-turn conversation with Claude via the Agent SDK.

    ``ClaudeSDKClient`` is stateful — it preserves conversation context across
    messages only when the same client instance is reused.  ``ClaudeSession``
    is therefore an **async context manager** that connects the SDK client once
    on entry and disconnects it on exit.  All ``send_message()`` calls within
    the same ``async with`` block share the same client and thus the same
    conversation context.

    Usage::

        async with ClaudeSession(model="sonnet") as session:
            async for event in session.send_message("Hello"):
                print(event)
            async for event in session.send_message("How are you?"):
                print(event)
        # SDK client is disconnected here

    The SDK is configured with bypassed permissions and full tool access, so
    Claude can emit tool-use and tool-result events in addition to text.

    Parameters
    ----------
    model:
        Model alias to pass to ``ClaudeAgentOptions``.  Defaults to
        ``"sonnet"`` (maps to the latest Claude Sonnet release).
        Other valid values: ``"opus"``, ``"haiku"``.
    """

    def __init__(self, model: str = "sonnet", resume: str | None = None) -> None:
        auth_method = _check_auth()
        _log(f"Auth via {auth_method}")
        if resume:
            _log(f"Resuming session: {resume}")

        self._options = ClaudeAgentOptions(
            # `allowed_tools` defaults to [] in the SDK, which silently disables
            # tools unless we opt in explicitly.
            tools="all",
            permission_mode="bypassPermissions",
            max_turns=100,
            model=model,
            resume=resume,
        )

        # The SDK client — created once and kept alive for the session lifetime.
        # It is entered (connected) via __aenter__ / connect() and exited via
        # __aexit__ / close().  Access only after connect() has been called.
        self._sdk_client: ClaudeSDKClient | None = None
        # Reference to the currently active client exposed for cancel()
        self._client: ClaudeSDKClient | None = None
        # Flag to signal cancellation to the streaming loop
        self._cancelled: bool = False

    # ------------------------------------------------------------------
    # Async context manager — connects/disconnects the SDK client once
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "ClaudeSession":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def connect(self) -> None:
        """Enter the SDK client context manager (connect once).

        Called automatically by ``async with ClaudeSession(...) as s``.
        """
        client = ClaudeSDKClient(options=self._options)
        self._sdk_client = await client.__aenter__()
        self._client = self._sdk_client
        _log("SDK client connected")

    async def close(self) -> None:
        """Exit the SDK client context manager (disconnect).

        Called automatically when exiting the ``async with`` block, or can be
        called explicitly to shut down the session.
        """
        if self._sdk_client is not None:
            try:
                await self._sdk_client.__aexit__(None, None, None)
                _log("SDK client disconnected")
            except Exception as exc:
                _log(f"SDK client close error (ignored): {exc}")
            finally:
                self._sdk_client = None
                self._client = None

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    async def send_message(self, text: str) -> AsyncGenerator[dict[str, Any], None]:
        """Send text to Claude and yield structured response events incrementally.

        The SDK client must be connected before calling this method (i.e., the
        session must be used inside an ``async with`` block).

        Parameters
        ----------
        text:
            User message to send to Claude.

        Yields
        ------
        dict[str, Any]
            Structured response events as they arrive.
        """
        if not text.strip():
            return

        if self._sdk_client is None:
            _log("send_message() called but SDK client is not connected — skipping")
            return

        self._cancelled = False
        _log(f"Sending to Claude: {text[:80]}...")

        try:
            client = self._sdk_client
            await client.query(text)
            # Signal that the query has been sent so the server can tag/discover
            # the session immediately (before any response chunks arrive).
            yield {"type": "_query_sent"}
            async for event in self._iter_response():
                yield event

        except Exception as exc:
            _log(f"Error communicating with Claude: {exc}")

    async def _iter_response(self) -> AsyncGenerator[dict[str, Any], None]:
        """Yield structured events from the SDK client's current response stream.

        Shared by ``send_message`` and ``send_message_with_images``.
        Assumes ``client.query()`` (or equivalent) has already been called.
        """
        client = self._sdk_client
        if client is None:
            return

        session_id_emitted = False
        async for msg in client.receive_response():
            if self._cancelled:
                break

            # Extract session_id from the first StreamEvent — authoritative,
            # comes directly from the SDK rather than a list_sessions heuristic.
            if StreamEvent is not None and isinstance(msg, StreamEvent):
                if not session_id_emitted and msg.session_id:
                    session_id_emitted = True
                    yield {"type": "_session_id", "session_id": msg.session_id}
                continue

            # ResultMessage: end-of-turn confirmation, confirms session_id.
            if ResultMessage is not None and isinstance(msg, ResultMessage):
                if not session_id_emitted and msg.session_id:
                    session_id_emitted = True
                    yield {"type": "_session_id", "session_id": msg.session_id}
                yield {
                    "type": "_result",
                    "session_id": msg.session_id,
                    "stop_reason": msg.stop_reason,
                    "num_turns": msg.num_turns,
                    "is_error": msg.is_error,
                }
                continue

            if AssistantMessage is not None and isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if TextBlock is not None and isinstance(block, TextBlock):
                        if block.text:
                            yield {"type": "text", "text": block.text}
                    elif ThinkingBlock is not None and isinstance(block, ThinkingBlock):
                        if block.thinking:
                            yield {"type": "thinking", "text": block.thinking}
                    elif ToolUseBlock is not None and isinstance(block, ToolUseBlock):
                        yield {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
            elif UserMessage is not None and isinstance(msg, UserMessage):
                content = msg.content if isinstance(msg.content, list) else []
                for block in content:
                    if ToolResultBlock is not None and isinstance(block, ToolResultBlock):
                        yield {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": _stringify_tool_result_content(block.content),
                            "is_error": bool(block.is_error),
                        }
                if getattr(msg, "tool_use_result", None):
                    tool_result = msg.tool_use_result
                    # Only process if tool_result is a dict (not a string or other type)
                    if isinstance(tool_result, dict):
                        yield {
                            "type": "tool_result",
                            "tool_use_id": tool_result.get("tool_use_id") or msg.parent_tool_use_id,
                            "content": _stringify_tool_result_content(tool_result.get("content")),
                            "is_error": bool(tool_result.get("is_error")),
                        }

    async def send_message_with_images(
        self, text: str, images: list[dict[str, Any]]
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Send a message that includes one or more images to Claude.

        Parameters
        ----------
        text:
            Optional text to accompany the images.
        images:
            List of dicts with ``data`` (base64 string) and ``media_type``
            (e.g. ``"image/jpeg"``).

        Yields
        ------
        dict[str, Any]
            Structured response events as they arrive.
        """
        if self._sdk_client is None:
            _log("send_message_with_images() called but SDK client is not connected")
            return

        self._cancelled = False
        _log(f"Sending to Claude: {len(images)} image(s) + text: {text[:80]!r}")

        # Build a content block list: image blocks first, then optional text
        content: list[dict[str, Any]] = []
        for img in images:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"],
                },
            })
        if text.strip():
            content.append({"type": "text", "text": text})

        async def _single_msg():
            yield {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
            }

        try:
            client = self._sdk_client
            await client.query(_single_msg())
            yield {"type": "_query_sent"}
            async for event in self._iter_response():
                yield event

        except Exception as exc:
            _log(f"Error sending image message to Claude: {exc}")

    def cancel(self) -> None:
        """Interrupt an in-flight response.

        Sets the cancellation flag (checked in the streaming loop) and calls
        ``interrupt()`` on the active SDK client if one exists.
        """
        self._cancelled = True
        if self._client is not None:
            _log("Interrupting active Claude SDK client")
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self._client.interrupt())
            except Exception as exc:
                _log(f"SDK interrupt error (ignored): {exc}")

    async def set_model(self, model: str) -> None:
        """Change the active model on the live SDK client connection.

        Parameters
        ----------
        model:
            Model alias (e.g. ``"sonnet"``, ``"opus"``, ``"haiku"``).
        """
        if self._sdk_client is not None:
            await self._sdk_client.set_model(model)
            _log(f"Model changed to: {model}")

    @staticmethod
    def check_available() -> bool:
        """Check if the claude_agent_sdk package is importable.

        The SDK bundles the Claude Code CLI, so this is the only check needed —
        no PATH lookup required.
        """
        try:
            import importlib

            loader = importlib.util.find_spec("claude_agent_sdk")
            return loader is not None
        except (ImportError, ValueError):
            return False


def _stringify_tool_result_content(content: Any) -> str:
    """Normalize tool result content into a frontend-safe string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content)
