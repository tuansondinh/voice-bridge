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
from typing import AsyncGenerator

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        TextBlock,
    )
except ImportError:
    # SDK not installed — names are None so check_available() can return False
    # and tests can patch them in.  The server startup check will catch this
    # before ClaudeSession is actually constructed.
    AssistantMessage = None  # type: ignore[assignment,misc]
    ClaudeAgentOptions = None  # type: ignore[assignment,misc]
    ClaudeSDKClient = None  # type: ignore[assignment,misc]
    TextBlock = None  # type: ignore[assignment,misc]


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
            async for chunk in session.send_message("Hello"):
                print(chunk, end="")
            async for chunk in session.send_message("How are you?"):
                print(chunk, end="")
        # SDK client is disconnected here

    The SDK is configured with an empty ``allowed_tools`` list so Claude is
    restricted to chat-only mode — no file access or shell execution is
    available through the voice bridge.

    Parameters
    ----------
    model:
        Model alias to pass to ``ClaudeAgentOptions``.  Defaults to
        ``"sonnet"`` (maps to the latest Claude Sonnet release).
        Other valid values: ``"opus"``, ``"haiku"``.
    """

    def __init__(self, model: str = "sonnet") -> None:
        auth_method = _check_auth()
        _log(f"Auth via {auth_method}")

        self._options = ClaudeAgentOptions(
            allowed_tools=[],           # chat-only — no file/bash access
            permission_mode="acceptEdits",
            max_turns=100,
            model=model,
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

    async def send_message(self, text: str) -> AsyncGenerator[str, None]:
        """Send text to Claude and yield response text chunks incrementally.

        The SDK client must be connected before calling this method (i.e., the
        session must be used inside an ``async with`` block).

        Parameters
        ----------
        text:
            User message to send to Claude.

        Yields
        ------
        str
            Text chunks from Claude's response as they arrive.
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

            async for msg in client.receive_response():
                if self._cancelled:
                    break

                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            if block.text:
                                yield block.text

        except Exception as exc:
            _log(f"Error communicating with Claude: {exc}")

    def cancel(self) -> None:
        """Interrupt an in-flight response.

        Sets the cancellation flag (checked in the streaming loop) and calls
        ``interrupt()`` on the active SDK client if one exists.
        """
        self._cancelled = True
        if self._client is not None:
            _log("Interrupting active Claude SDK client")
            try:
                self._client.interrupt()
            except Exception as exc:
                _log(f"SDK interrupt error (ignored): {exc}")

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
