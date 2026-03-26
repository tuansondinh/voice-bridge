# Review: Chat UX Improvements — Tool Call Display + Full Polish

## Blocking findings

1. Resolved. `voice_bridge/static/index.html:861`, `voice_bridge/static/index.html:984`, `voice_bridge/static/index.html:1384`, `voice_bridge/static/index.html:1437`
   The new assistant-message state is never cleared when a response aborts with `error` or when the websocket closes mid-stream. Both paths remove the typing indicator and update status, but neither calls `finalizeAssistantMessage()` nor resets `currentAssistantEl` / `currentTextSpan`. The next retry or post-reconnect response therefore reuses the partially rendered previous assistant bubble and appends new text/tool cards into it. This breaks retry/reconnect flows and can mix two different responses into one message.

2. Resolved. `voice_bridge/static/index.html:1499`, `voice_bridge/static/index.html:1507`
   The markdown renderer now turns model output into live `<a href="...">` tags without any protocol allowlist. Because the URL is inserted directly into `href`, a response like `[open this](javascript:alert(document.domain))` becomes a clickable script URL in the app. That is a user-triggered XSS/vector introduced by the new progressive markdown feature and should be blocked by restricting links to safe schemes such as `http:`, `https:`, and optionally `mailto:`.

## Non-blocking findings

None beyond the blocking issues above.

## Verification gaps

- I used a second-opinion CLI (`codex review --base 2ff235920192741f07d2bf4f407c6cec30508ee1`) during the review. It did not produce an additional completed finding set before timing out on environment/tooling checks, so the review below is based on direct inspection.
- I did not run the Playwright verification scenarios from the plan, so the frontend behaviors remain unverified end-to-end.
- `python -m pytest -q tests/test_claude.py` only runs partially in this shell after forcing `PYTHONPATH=.`, and it still fails because `claude_agent_sdk` is not importable here. Plain `pytest -q tests/test_claude.py` also fails earlier due local environment/plugin setup. So backend test status is not confirmed from this environment.

---

## Fixes Applied

**Fixes:** Finalized/reset assistant streaming state on websocket `onclose` and `onerror`, and added URL scheme sanitization so only `http:`, `https:`, and `mailto:` links render as anchors.
**Commit:** fix: address review findings (`ad0cb04`)
**Status:** All blocking findings resolved

---

## Fix Applied

**Bug:** Tool call display scenario failed because Claude SDK tools were still effectively disabled, so the model answered directly instead of emitting tool-use events.
**Fix:** Explicitly enabled SDK tools in `ClaudeAgentOptions` with `tools="all"` and added a regression test that asserts the session does not inherit the SDK's empty `allowed_tools` default.
**Commit:** fix: enable Claude SDK tools (`e91ab53`)
**Status:** Resolved
