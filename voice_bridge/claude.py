"""bridge_claude.py — Claude CLI subprocess wrapper for the voice bridge.

Spawns ``claude -p "text" --output-format stream-json`` per message,
parses the streaming JSON output, and yields text chunks.

Supports multi-turn conversations via --resume with a session ID
captured from the first call's ``init`` event.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from typing import AsyncGenerator


def _log(msg: str) -> None:
    print(f"[bridge-claude] {msg}", file=sys.stderr, flush=True)


def _find_claude_binary() -> str | None:
    """Find the claude CLI binary on PATH."""
    return shutil.which("claude")


class ClaudeSession:
    """Manages interaction with Claude CLI via subprocess.

    Each call to ``send_message`` spawns a new ``claude -p`` process.
    Multi-turn context is maintained by capturing the ``session_id``
    from the first call and passing ``--resume <session_id>`` to
    subsequent calls.
    """

    def __init__(self) -> None:
        self._claude_bin = _find_claude_binary()
        if not self._claude_bin:
            raise RuntimeError(
                "Claude CLI not found on PATH. "
                "Install it from https://docs.anthropic.com/en/docs/claude-code"
            )
        _log(f"Using Claude CLI: {self._claude_bin}")

        self._session_id: str | None = None
        self._active_process: asyncio.subprocess.Process | None = None

    async def send_message(self, text: str) -> AsyncGenerator[str, None]:
        """Send text to Claude CLI, yield text chunks as they stream back.

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

        cmd = [
            self._claude_bin,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
            "--model", "sonnet",
            "-p",
        ]

        # Resume conversation if we have a session ID from a previous call
        if self._session_id:
            cmd.extend(["--resume", self._session_id])

        # -- ends option parsing so text starting with '-' isn't treated as a flag
        cmd.extend(["--", text])

        _log(f"Sending to Claude: {text[:80]}...")
        if self._session_id:
            _log(f"Resuming session: {self._session_id}")

        try:
            self._active_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            assert self._active_process.stdout is not None

            async for line in self._active_process.stdout:
                decoded = line.decode("utf-8", errors="replace").strip()
                if not decoded:
                    continue

                try:
                    event = json.loads(decoded)
                except json.JSONDecodeError:
                    continue

                # Capture session ID from the init event (first call)
                if (
                    event.get("type") == "system"
                    and event.get("subtype") == "init"
                    and not self._session_id
                ):
                    self._session_id = event.get("session_id")
                    _log(f"Captured session ID: {self._session_id}")

                # Extract text from stream-json events
                text_chunk = self._extract_text(event)
                if text_chunk:
                    yield text_chunk

            await asyncio.wait_for(self._active_process.wait(), timeout=10)

            if self._active_process.returncode != 0:
                stderr_data = b""
                if self._active_process.stderr:
                    stderr_data = await self._active_process.stderr.read()
                _log(
                    f"Claude exited with code {self._active_process.returncode}: "
                    f"{stderr_data.decode('utf-8', errors='replace')[:200]}"
                )

        except asyncio.TimeoutError:
            _log("Claude CLI timed out")
            if self._active_process:
                self._active_process.kill()
        except Exception as exc:
            _log(f"Error communicating with Claude: {exc}")
        finally:
            self._active_process = None

    def cancel(self) -> None:
        """Kill the active Claude process if running.

        This does NOT destroy the session — the session ID is preserved
        and the next ``send_message`` call will resume from where the
        conversation left off (minus the interrupted response).
        """
        if self._active_process and self._active_process.returncode is None:
            _log("Cancelling active Claude process")
            self._active_process.kill()

    @staticmethod
    def _extract_text(event: dict) -> str | None:
        """Extract text content from a stream-json event.

        With ``--include-partial-messages``, the CLI emits ``stream_event``
        wrappers around the Anthropic API streaming protocol.  We extract
        incremental text from ``content_block_delta`` events for true
        token-by-token streaming.  The ``assistant`` and ``result`` events
        still contain the full text but are ignored to avoid duplication.
        """
        event_type = event.get("type")

        # stream_event wraps the raw API streaming events
        if event_type == "stream_event":
            inner = event.get("event", {})
            inner_type = inner.get("type")
            if inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    return delta.get("text", "")

        return None

    @staticmethod
    def check_available() -> bool:
        """Check if the claude CLI is available on PATH."""
        return _find_claude_binary() is not None
