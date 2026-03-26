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

    Uses ``ClaudeSDKClient`` as a persistent session context manager so that
    conversation context is preserved across calls without manual session_id
    tracking.

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

        # Active SDK client (set inside send_message context)
        self._client: ClaudeSDKClient | None = None
        # Flag to signal cancellation to the streaming loop
        self._cancelled: bool = False

    async def send_message(self, text: str) -> AsyncGenerator[str, None]:
        """Send text to Claude and yield response text chunks incrementally.

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

        self._cancelled = False
        _log(f"Sending to Claude: {text[:80]}...")

        try:
            async with ClaudeSDKClient(options=self._options) as client:
                self._client = client
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
        finally:
            self._client = None

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
