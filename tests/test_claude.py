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


def _make_tool_use_block(tool_use_id: str, name: str, tool_input: dict):
    """Create a mock ToolUseBlock."""
    from claude_agent_sdk import ToolUseBlock

    block = MagicMock(spec=ToolUseBlock)
    block.__class__ = ToolUseBlock
    block.id = tool_use_id
    block.name = name
    block.input = tool_input
    return block


def _make_thinking_block(thinking: str, signature: str = "sig"):
    """Create a mock ThinkingBlock."""
    from claude_agent_sdk import ThinkingBlock

    block = MagicMock(spec=ThinkingBlock)
    block.__class__ = ThinkingBlock
    block.thinking = thinking
    block.signature = signature
    return block


def _make_tool_result_block(
    tool_use_id: str,
    content: str | list[dict] | None,
    is_error: bool | None = None,
):
    """Create a mock ToolResultBlock."""
    from claude_agent_sdk import ToolResultBlock

    block = MagicMock(spec=ToolResultBlock)
    block.__class__ = ToolResultBlock
    block.tool_use_id = tool_use_id
    block.content = content
    block.is_error = is_error
    return block


def _make_assistant_message_with_blocks(blocks: list):
    """Create a mock AssistantMessage with arbitrary SDK blocks."""
    from claude_agent_sdk import AssistantMessage

    msg = MagicMock(spec=AssistantMessage)
    msg.__class__ = AssistantMessage
    msg.content = blocks
    return msg


def _make_user_message_with_blocks(blocks: list):
    """Create a mock UserMessage with arbitrary SDK blocks."""
    from claude_agent_sdk import UserMessage

    msg = MagicMock(spec=UserMessage)
    msg.__class__ = UserMessage
    msg.content = blocks
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
                # Use async with so the SDK client is connected before send_message
                async with ClaudeSession() as session:
                    chunks = []
                    async for chunk in session.send_message("hi"):
                        chunks.append(chunk)

        assert chunks == [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world!"},
        ]

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
                async with ClaudeSession() as session:
                    chunks = []
                    async for chunk in session.send_message("hello"):
                        chunks.append(chunk)

        assert chunks == []

    @pytest.mark.asyncio
    async def test_yields_tool_use_events(self):
        """send_message() yields tool_use events from AssistantMessage blocks."""
        messages = [
            _make_assistant_message_with_blocks(
                [_make_tool_use_block("tool-1", "Read", {"path": "pyproject.toml"})]
            ),
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
                async with ClaudeSession() as session:
                    chunks = []
                    async for chunk in session.send_message("read the file"):
                        chunks.append(chunk)

        assert chunks == [
            {
                "type": "tool_use",
                "id": "tool-1",
                "name": "Read",
                "input": {"path": "pyproject.toml"},
            }
        ]

    @pytest.mark.asyncio
    async def test_yields_thinking_events(self):
        """send_message() yields thinking events from AssistantMessage blocks."""
        messages = [
            _make_assistant_message_with_blocks(
                [_make_thinking_block("Reasoning about the request")]
            ),
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
                async with ClaudeSession() as session:
                    chunks = []
                    async for chunk in session.send_message("think"):
                        chunks.append(chunk)

        assert chunks == [{"type": "thinking", "text": "Reasoning about the request"}]

    @pytest.mark.asyncio
    async def test_yields_tool_result_events(self):
        """send_message() yields tool_result events from UserMessage blocks."""
        messages = [
            _make_user_message_with_blocks(
                [_make_tool_result_block("tool-1", "file contents", False)]
            ),
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
                async with ClaudeSession() as session:
                    chunks = []
                    async for chunk in session.send_message("run a tool"):
                        chunks.append(chunk)

        assert chunks == [
            {
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": "file contents",
                "is_error": False,
            }
        ]

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
                async with ClaudeSession() as session:
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
        assert {"type": "text", "text": "first chunk"} in chunks
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

    def test_claude_session_is_async_context_manager(self):
        """ClaudeSession must support 'async with' (has __aenter__ and __aexit__)."""
        from voice_bridge.claude import ClaudeSession

        assert hasattr(ClaudeSession, "__aenter__"), (
            "ClaudeSession must implement __aenter__ for 'async with' support"
        )
        assert hasattr(ClaudeSession, "__aexit__"), (
            "ClaudeSession must implement __aexit__ for 'async with' support"
        )

    def test_close_method_exists(self):
        """ClaudeSession must expose a close() coroutine method."""
        import inspect

        from voice_bridge.claude import ClaudeSession

        assert hasattr(ClaudeSession, "close"), (
            "ClaudeSession must have a close() method to disconnect the SDK client"
        )
        assert inspect.iscoroutinefunction(ClaudeSession.close), (
            "ClaudeSession.close() must be a coroutine (async def)"
        )


# ---------------------------------------------------------------------------
# Test: multi-turn context preservation (new lifecycle)
# ---------------------------------------------------------------------------


class TestMultiTurnContext:
    @pytest.mark.asyncio
    async def test_same_sdk_client_reused_across_send_message_calls(self):
        """Calling send_message() twice reuses the same ClaudeSDKClient instance.

        The SDK client is stateful and preserves conversation context only when
        the same instance handles successive messages.  Creating a new client per
        message loses all prior context.
        """
        created_clients: list = []

        def _make_messages(text_blocks: list[str]):
            msgs = [_make_assistant_message(text_blocks), _make_result_message()]

            async def _gen():
                for m in msgs:
                    yield m

            return _gen

        class FakeClient:
            def __init__(self, *args, **kwargs):
                created_clients.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def query(self, text):
                pass

            def receive_response(self):
                return _make_messages(["chunk"])(

                )

            def interrupt(self):
                pass

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            from voice_bridge.claude import ClaudeSession

            with patch("voice_bridge.claude.ClaudeSDKClient", FakeClient):
                async with ClaudeSession() as session:
                    # First message
                    async for _ in session.send_message("message one"):
                        pass
                    # Second message
                    async for _ in session.send_message("message two"):
                        pass

        # Only one client should have been created for both messages
        assert len(created_clients) == 1, (
            f"Expected 1 ClaudeSDKClient instance across two send_message() calls, "
            f"got {len(created_clients)}.  Multi-turn context is lost when a new "
            f"client is created per message."
        )

    @pytest.mark.asyncio
    async def test_context_manager_connects_and_disconnects_client(self):
        """'async with ClaudeSession()' connects client on enter, closes on exit."""
        enter_calls = []
        exit_calls = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                enter_calls.append("enter")
                return self

            async def __aexit__(self, *args):
                exit_calls.append("exit")

            async def query(self, text):
                pass

            def receive_response(self):
                async def _gen():
                    yield _make_result_message()
                return _gen()

            def interrupt(self):
                pass

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            from voice_bridge.claude import ClaudeSession

            with patch("voice_bridge.claude.ClaudeSDKClient", FakeClient):
                async with ClaudeSession() as session:
                    assert len(enter_calls) == 1, "Client __aenter__ should be called on session entry"
                    assert len(exit_calls) == 0, "Client __aexit__ should NOT be called while session is open"

                # After exiting the context manager
                assert len(exit_calls) == 1, "Client __aexit__ must be called on session exit"
