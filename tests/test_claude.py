"""Tests for voice_bridge/claude.py — ClaudeSession using Agent SDK."""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _make_text_block(text: str):
    """Create a mock TextBlock."""
    from claude_agent_sdk import TextBlock

    block = MagicMock(spec=TextBlock)
    block.__class__ = TextBlock
    block.text = text
    return block


def _make_assistant_message(text_blocks: list[str]):
    """Create a mock AssistantMessage with text blocks."""
    from claude_agent_sdk import AssistantMessage

    msg = MagicMock(spec=AssistantMessage)
    msg.__class__ = AssistantMessage
    msg.content = [_make_text_block(t) for t in text_blocks]
    return msg


def _make_result_message():
    """Create a mock ResultMessage (end-of-stream marker)."""
    from claude_agent_sdk import ResultMessage

    msg = MagicMock(spec=ResultMessage)
    msg.__class__ = ResultMessage
    return msg


# ---------------------------------------------------------------------------
# Test: check_available
# ---------------------------------------------------------------------------


class TestCheckAvailable:
    def test_returns_true_when_sdk_importable(self):
        """check_available() returns True when claude_agent_sdk is importable."""
        from voice_bridge.claude import ClaudeSession

        # SDK is installed in our test environment
        assert ClaudeSession.check_available() is True

    def test_returns_false_when_sdk_not_importable(self):
        """check_available() returns False when claude_agent_sdk cannot be imported."""
        from voice_bridge.claude import ClaudeSession

        with patch.dict(sys.modules, {"claude_agent_sdk": None}):
            assert ClaudeSession.check_available() is False


# ---------------------------------------------------------------------------
# Test: constructor raises when no auth env vars
# ---------------------------------------------------------------------------


class TestConstructorAuth:
    def test_raises_if_no_auth_env_vars(self):
        """ClaudeSession() raises RuntimeError with clear message when no auth set."""
        env_without_auth = {
            k: v
            for k, v in os.environ.items()
            if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
        }
        with patch.dict(os.environ, env_without_auth, clear=True):
            with pytest.raises(RuntimeError, match="CLAUDE_CODE_OAUTH_TOKEN"):
                from voice_bridge.claude import ClaudeSession

                ClaudeSession()

    def test_succeeds_with_oauth_token(self):
        """ClaudeSession() constructs without error when CLAUDE_CODE_OAUTH_TOKEN is set."""
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "test-token"}
        with patch.dict(os.environ, env, clear=True):
            from voice_bridge.claude import ClaudeSession

            session = ClaudeSession()
            assert session is not None

    def test_succeeds_with_api_key(self):
        """ClaudeSession() constructs without error when ANTHROPIC_API_KEY is set."""
        env = {"ANTHROPIC_API_KEY": "sk-test-key"}
        with patch.dict(os.environ, env, clear=True):
            from voice_bridge.claude import ClaudeSession

            session = ClaudeSession()
            assert session is not None


# ---------------------------------------------------------------------------
# Test: send_message — yields text chunks via SDK
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_yields_text_from_assistant_message(self):
        """send_message() yields text chunks from AssistantMessage TextBlocks."""
        messages = [
            _make_assistant_message(["Hello ", "world!"]),
            _make_result_message(),
        ]

        async def _fake_receive_response():
            for msg in messages:
                yield msg

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.query = AsyncMock()
        mock_client.receive_response = _fake_receive_response

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            from voice_bridge.claude import ClaudeSession

            with patch("voice_bridge.claude.ClaudeSDKClient", return_value=mock_client):
                session = ClaudeSession()
                chunks = []
                async for chunk in session.send_message("hi"):
                    chunks.append(chunk)

        assert chunks == ["Hello ", "world!"]

    @pytest.mark.asyncio
    async def test_skips_empty_text(self):
        """send_message() with empty string yields nothing."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            from voice_bridge.claude import ClaudeSession

            session = ClaudeSession()
            chunks = []
            async for chunk in session.send_message("   "):
                chunks.append(chunk)

        assert chunks == []

    @pytest.mark.asyncio
    async def test_ignores_non_assistant_messages(self):
        """send_message() skips SystemMessage and other non-text types."""
        from claude_agent_sdk import SystemMessage

        sys_msg = MagicMock(spec=SystemMessage)
        sys_msg.__class__ = SystemMessage

        messages = [sys_msg, _make_result_message()]

        async def _fake_receive_response():
            for msg in messages:
                yield msg

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.query = AsyncMock()
        mock_client.receive_response = _fake_receive_response

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            from voice_bridge.claude import ClaudeSession

            with patch("voice_bridge.claude.ClaudeSDKClient", return_value=mock_client):
                session = ClaudeSession()
                chunks = []
                async for chunk in session.send_message("hello"):
                    chunks.append(chunk)

        assert chunks == []

    @pytest.mark.asyncio
    async def test_cancel_stops_iteration(self):
        """cancel() sets the cancelled flag so no further chunks are yielded."""
        import asyncio

        # stop_event simulates the SDK stopping the generator when interrupt() is called
        stop_event = asyncio.Event()

        async def _receive_with_pause():
            # First message comes through immediately
            yield _make_assistant_message(["first chunk"])
            # Then blocks — simulates waiting for next message from SDK
            await stop_event.wait()
            # After stop, generator ends (no more messages)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.query = AsyncMock()
        mock_client.receive_response = _receive_with_pause

        # When interrupt() is called, unblock the generator so it exits
        def _on_interrupt():
            stop_event.set()

        mock_client.interrupt = MagicMock(side_effect=_on_interrupt)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            from voice_bridge.claude import ClaudeSession

            with patch("voice_bridge.claude.ClaudeSDKClient", return_value=mock_client):
                session = ClaudeSession()

                chunks = []
                got_first = asyncio.Event()

                async def _consume():
                    async for chunk in session.send_message("hi"):
                        chunks.append(chunk)
                        got_first.set()

                task = asyncio.create_task(_consume())
                # Wait until we've received the first chunk
                await asyncio.wait_for(got_first.wait(), timeout=2.0)
                # Cancel: sets _cancelled=True and calls interrupt(), which sets stop_event
                session.cancel()
                await asyncio.wait_for(task, timeout=2.0)

        # First chunk was received; subsequent iterations stopped due to cancel
        assert "first chunk" in chunks
        mock_client.interrupt.assert_called_once()


# ---------------------------------------------------------------------------
# Test: public interface unchanged
# ---------------------------------------------------------------------------


class TestPublicInterface:
    def test_send_message_is_async_generator(self):
        """send_message() is an async generator function."""
        import inspect

        from voice_bridge.claude import ClaudeSession

        assert inspect.isasyncgenfunction(ClaudeSession.send_message)

    def test_cancel_is_sync(self):
        """cancel() is a regular synchronous method."""
        import inspect

        from voice_bridge.claude import ClaudeSession

        assert not inspect.iscoroutinefunction(ClaudeSession.cancel)

    def test_check_available_is_static(self):
        """check_available() is a static method."""
        from voice_bridge.claude import ClaudeSession

        assert isinstance(
            ClaudeSession.__dict__["check_available"], staticmethod
        )
