# Manual Verification Report: Chat UX Improvements — Tool Call Display + Full Polish

Plan ID: #2
Date: 2026-03-26
Verified by: User

## Summary
- Total scenarios: 11
- Passed: 11
- Failed: 0
- Skipped: 0

## Results

### ✓ Scenario 1: Tool call display
Status: PASS
Fix applied during verification: `fix: enable Claude SDK tools` (`e91ab53`)

### ✓ Scenario 2: Thinking indicator
Status: PASS

### ✓ Scenario 3: Typing indicator
Status: PASS

### ✓ Scenario 4: Streaming markdown
Status: PASS
User note: TTS produced weird noise at the start/end around markdown or code-block responses.

### ✓ Scenario 5: Code block copy
Status: PASS

### ✓ Scenario 6: Message copy
Status: PASS

### ✓ Scenario 7: Smart scroll
Status: PASS

### ✓ Scenario 8: Timestamps
Status: PASS

### ✓ Scenario 9: Error handling
Status: PASS

### ✓ Scenario 10: Reconnection
Status: PASS

### ✓ Scenario 11: TTS isolation
Status: PASS

## Acceptance Criteria
- [x] Tool calls from Claude are visible in the chat as collapsible cards with tool name, loading state, and expandable input/output
- [x] Thinking blocks appear as collapsed cards when the model uses extended thinking
- [x] A typing indicator (bouncing dots) appears before Claude's first response chunk
- [x] Markdown renders progressively during streaming (not just after full response)
- [x] Code blocks show language label and copy button
- [x] Assistant messages have a copy button on hover
- [x] Chat does not force-scroll when user scrolls up to read history; a "scroll down" pill appears for new messages
- [x] Each message shows a timestamp
- [x] Errors display as styled cards with retry functionality
- [x] Disconnects show a reconnection banner with attempt counter
- [x] Consecutive same-role messages within 30s are visually grouped
- [x] Tool call text, thinking text, tool names, and tool output are NOT spoken by TTS — only text content goes to TTS
- [x] All existing voice + text functionality continues to work (no regressions)

## Open Notes
- TTS can produce weird noise at the start/end of markdown or code-block responses. This did not block scenario acceptance, but it should be tracked as a follow-up bug.
