# Plan #1: Migrate from Claude CLI Subprocess to Agent SDK

Plan ID: #1
Generated: 2026-03-26
Platform: web
Status: approved

## Context

Currently, `voice_bridge/claude.py` spawns a new `claude -p` subprocess for every message, parses streaming JSON stdout, and tracks multi-turn conversations via `--resume <session_id>`. This works but has process startup overhead, fragile stdout parsing, and tight coupling to CLI flags.

The migration replaces this with `claude-agent-sdk` (official Anthropic Python package, v0.1.50+), which:
- Wraps the Claude Code CLI internally (bundles it — not a direct API client)
- Provides `ClaudeSDKClient` for persistent multi-turn sessions
- Native async streaming (`async for message in client.receive_response()`)
- Built-in session management (no manual session_id capture/resume)
- Proper error types (`CLINotFoundError`, `ProcessError`, etc.) instead of stderr parsing

**Authentication — Claude Max via OAuth:**
The SDK bundles the Claude Code CLI, which supports `CLAUDE_CODE_OAUTH_TOKEN`. This allows Claude Max subscribers to use their subscription quota:
1. Run `claude setup-token` → generates a long-lived OAuth token
2. Set `CLAUDE_CODE_OAUTH_TOKEN` env var
3. The SDK's bundled CLI picks it up automatically

Alternatively, `ANTHROPIC_API_KEY` works for pay-per-use API billing. Both paths are supported — OAuth is the primary target for this migration.

**Key dependency:** `claude-agent-sdk>=0.1.0` (official, by Anthropic PBC, MIT licensed)

## Phases

1. [x] Phase 1: Replace `claude.py` with Agent SDK client — complexity: standard
   - Add `claude-agent-sdk>=0.1.0` dependency in `pyproject.toml`
   - Rewrite `ClaudeSession` class to use `ClaudeSDKClient` instead of subprocess
   - Use `async with ClaudeSDKClient(options)` as persistent session context manager
   - Configure `ClaudeAgentOptions`: `max_turns`, `allowed_tools` (restrict to chat-only — no file/bash tools), `permission_mode`
   - Replace `send_message()` to use `client.query()` + `client.receive_response()` streaming
   - Extract text from `AssistantMessage` / `TextBlock` objects instead of parsing stream-json events
   - Replace `cancel()` — cancel the async iterator or use SDK interruption mechanism
   - Update `check_available()` — try importing `claude_agent_sdk` instead of checking binary on PATH
   - Maintain the same public interface: `send_message(text) -> AsyncGenerator[str]`, `cancel()`, `check_available()`
   - Handle auth: check for `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`, clear error message if neither set
   - Remove all subprocess spawning, JSON stdout parsing, and session_id/--resume logic

2. [x] Phase 2: Update server integration & startup flow — complexity: standard
   - Update `__main__.py`: replace Claude CLI binary check with SDK import check
   - Update `__main__.py`: remove `shutil.which("claude")` path — SDK bundles its own CLI
   - Update `server.py` `BridgeSession.__init__`: adapt to new `ClaudeSession` constructor
   - Verify `_stream_claude_response()` works with the new async generator (should be drop-in since interface is preserved)
   - Add `--model` CLI flag (default: `sonnet`) to pass through to `ClaudeAgentOptions`
   - Update health endpoint to reflect SDK status and auth method in use
   - Update ARCHITECTURE.md to document SDK-based approach and OAuth flow
   - Update README.md: replace "Requires Claude Code CLI on PATH" with SDK install + `claude setup-token` instructions
   - Test full flow: voice input -> VAD -> STT -> SDK -> TTS -> audio output

## Acceptance Criteria
- `voice-bridge` starts successfully with `CLAUDE_CODE_OAUTH_TOKEN` set (Claude Max)
- `voice-bridge` also works with `ANTHROPIC_API_KEY` (API billing fallback)
- Clear error message if neither auth env var is set
- Multi-turn conversations work (context preserved across messages via `ClaudeSDKClient`)
- Streaming text chunks arrive incrementally (not buffered until complete)
- TTS receives sentence chunks during streaming (same incremental behavior as before)
- `cancel()` interrupts an in-flight response
- `stop_tts` from client still kills the Claude response mid-stream
- `allowed_tools` restricts Claude to chat-only (no file/bash access through voice bridge)
- Existing voice/text input flows work unchanged
- No regressions in VAD, STT, or TTS pipelines (untouched)

## Verification
Tool: Playwright
Scenarios:
- Start server with `CLAUDE_CODE_OAUTH_TOKEN` set, verify health endpoint returns ready
- Start server with neither auth var, verify clear error message
- Connect via WebSocket, send text message, verify streaming response arrives
- Send second message, verify conversation context is maintained
- Send stop_tts during response, verify stream is interrupted
- Verify TTS audio chunks are received during streaming response

---

## Review

Date: 2026-03-26
Reviewer: Opus
Base commit: 8fe949ccf6ff2a4bde64576632d4a6c9606fa1f0
Verdict: NEEDS FIXES

### Findings

**Blocking** (escalate to fix-pass builder)
- [x] Multi-turn conversation context is NOT preserved. In `voice_bridge/claude.py:110`, `ClaudeSDKClient` is created inside `async with` within each `send_message()` call. The client is connected on entry and disconnected on exit of the context manager, so every message starts a fresh session. The SDK docs confirm the client is "Stateful: Maintains conversation context across messages" but only when the same client instance is reused. The fix requires keeping the `ClaudeSDKClient` alive across multiple `send_message()` calls -- e.g., connecting once in an `async_init()`/`connect()` method and disconnecting in a `close()` method, or making the `ClaudeSession` itself an async context manager that `BridgeSession` holds open for its lifetime. This is a structural change touching `claude.py`, `server.py` (BridgeSession lifecycle), and tests.

**Fixed by reviewer** (already applied)
- [x] `ARCHITECTURE.md:34` still said `claude.py # Claude CLI subprocess wrapper` -- updated to `# Claude Agent SDK client`
- [x] `ARCHITECTURE.md:310` still said `Claude subprocess cancelled` -- updated to `Claude SDK client cancelled`

**Non-blocking**
- [x] `server.py:1` module docstring still mentions "Claude Code CLI" and "piped to/from Claude CLI" -- fixed: updated to "Claude Agent SDK"
- [ ] `test_phase2.py:TestModelFlag::test_model_default_is_sonnet` creates a fresh `argparse.ArgumentParser` instead of actually running the `run_bridge` parser logic, so it tests itself rather than the real code. Consider importing and invoking the actual parser.
- [x] No test verifies multi-turn context preservation -- fixed: added `TestMultiTurnContext` tests

### Build / Test Status
- Tests: PASS -- 23/23 passed (3.71s)
- Lint: not configured (no linter in pyproject.toml)

### Acceptance Criteria
- [x] `voice-bridge` starts successfully with `CLAUDE_CODE_OAUTH_TOKEN` set: met (startup checks env var, constructs ClaudeSession)
- [x] `voice-bridge` also works with `ANTHROPIC_API_KEY`: met (fallback path tested)
- [x] Clear error message if neither auth env var is set: met (RuntimeError with instructions)
- [x] Multi-turn conversations work (context preserved across messages): Resolved — ClaudeSession is now an async context manager; ClaudeSDKClient is connected once and reused across all send_message() calls
- [x] Streaming text chunks arrive incrementally: met (async generator yields TextBlock.text as received)
- [x] TTS receives sentence chunks during streaming: met (sentence buffer + queue unchanged)
- [x] `cancel()` interrupts an in-flight response: met (sets flag + calls client.interrupt())
- [x] `stop_tts` from client still kills the Claude response mid-stream: met (unchanged stop_tts handling in server.py)
- [x] `allowed_tools` restricts Claude to chat-only: met (allowed_tools=[] in ClaudeAgentOptions)
- [x] Existing voice/text input flows work unchanged: met (public interface preserved, server.py integration intact)
- [x] No regressions in VAD, STT, or TTS pipelines: met (those modules were not touched)

---

## Review Fixes Applied

Fixes: Restructured `ClaudeSession` as async context manager so `ClaudeSDKClient` connects once and persists across all `send_message()` calls. Updated `server.py` `BridgeSession.run()` to call `connect()`/`close()` around session lifetime. Fixed `server.py` module docstring. Added `TestMultiTurnContext` tests (client reuse + context manager lifecycle). 27/27 tests pass.
Commit: fix: address review findings (5c5f123)
Status: All blocking findings resolved
