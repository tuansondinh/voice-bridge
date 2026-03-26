# Plan #2: Chat UX Improvements — Tool Call Display + Full Polish

Plan ID: #2
Generated: 2026-03-26
Platform: web
Status: draft

## Context

The voice-bridge chat is currently text-only (`allowed_tools=[]` in claude.py). The agent can't use tools, and the UI has no concept of tool calls, thinking blocks, or rich rendering. The goal is to bring the chat UX to parity with Claude.ai — showing tool calls as collapsible cards, streaming markdown, thinking indicators, and general polish across `claude.py`, `server.py`, and `static/index.html`.

## Phases

1. [x] Phase 1: Backend — Structured Events + Tool Enablement — complexity: standard
   - In `voice_bridge/claude.py`: add imports for `ToolUseBlock`, `ToolResultBlock`, `ThinkingBlock`, `UserMessage` from `claude_agent_sdk` (with graceful `None` fallback like existing imports)
   - Remove `allowed_tools=[]` from `ClaudeAgentOptions` (line 91) so Claude has full tool access. Keep `permission_mode="bypassPermissions"` and `max_turns=100`
   - Update docstring on `ClaudeSession` class to reflect tools are now enabled
   - Change `send_message()` to yield `dict` events instead of `str`. The yield type becomes `AsyncGenerator[dict, None]`. Events: `{"type": "text", "text": "..."}`, `{"type": "thinking", "text": "..."}`, `{"type": "tool_use", "id": "...", "name": "...", "input": {...}}`, `{"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": bool}`
   - In the `receive_response()` loop: handle `TextBlock` (existing), `ThinkingBlock` (yield thinking event), `ToolUseBlock` (yield tool_use event). Also handle `UserMessage` objects — the SDK yields these for tool results; iterate `msg.content` for `ToolResultBlock` entries and yield tool_result events
   - In `voice_bridge/server.py` `_do_stream_claude_response()` (lines 440-504): change `async for chunk in self._claude.send_message(user_text)` to handle dict events. For `type=="text"`: existing behavior (send `assistant_chunk` to frontend, feed to `sentence_buffer` and TTS). For `type=="thinking"`: send `{"type": "thinking", "text": ...}` to frontend, skip TTS. For `type=="tool_use"`: send `{"type": "tool_use_start", "id": ..., "name": ..., "input": ...}` to frontend, skip TTS. For `type=="tool_result"`: send `{"type": "tool_result", "id": ..., "content": ..., "is_error": ...}` to frontend, skip TTS. Only `text` events go into `full_response` list and `sentence_buffer`
   - Update `tests/test_claude.py`: change `test_yields_text_from_assistant_message` — chunks are now dicts `{"type": "text", "text": "Hello "}` etc. Update assertion from `["Hello ", "world!"]` to `[{"type": "text", "text": "Hello "}, {"type": "text", "text": "world!"}]`. Similarly update `test_cancel_stops_iteration` assertion. Add new test `test_yields_tool_use_events` that creates a mock `ToolUseBlock` in an `AssistantMessage` and verifies `{"type": "tool_use", ...}` is yielded. Add `test_yields_thinking_events` for `ThinkingBlock`

2. [x] Phase 2: Frontend — Tool Call Cards + Thinking/Typing Indicators — complexity: standard
   - In `static/index.html` CSS: add `.tool-card` styles (rounded card, `--surface` bg, 3px solid left border in `--border` color, 8px 12px padding, 12px margin top/bottom, 12px border-radius). Add `.tool-card.loading` (left border `#f59e0b` amber, pulsing opacity animation). Add `.tool-card.success` (left border `var(--success)` green). Add `.tool-card.error` (left border `var(--accent)` red). Add `.tool-card-header` (flex row, cursor pointer, gap 8px, align-items center, 13px font). Add `.tool-card-body` (max-height 0, overflow hidden, transition max-height 0.3s). Add `.tool-card.expanded .tool-card-body` (max-height 400px, overflow-y auto). Add `.tool-card-body pre` (13px font, margin 4px 0, word-break break-all). Add `.tool-icon` (16px, opacity 0.7). Add `.tool-status` (12px, `--text-dim` color). Add `.tool-chevron` (margin-left auto, transition transform 0.2s). Add `.tool-card.expanded .tool-chevron` (transform rotate(90deg))
   - Add `.thinking-block` styles (similar to tool-card but dashed left border, opacity 0.8, `--text-dim` left border color). Add `.thinking-block-header` and `.thinking-block-body` (same expand/collapse pattern)
   - Add `.typing-indicator` styles (3 bouncing dots: `.typing-indicator .dots span` — 6px circles, `--text-dim` bg, inline-block, staggered `animation-delay` 0s/0.15s/0.3s, bounce keyframe 0.6s infinite)
   - Refactor assistant message structure: change `appendAssistantChunk()` to track a `currentTextSpan` inside the assistant message. Text chunks append to `currentTextSpan.textContent`. When `appendToolCard()` is called, if `currentTextSpan` exists close it, insert the tool card, set `currentTextSpan = null` so next text creates a new span
   - Add `showTypingIndicator()` — creates a typing indicator element in chat (3 bouncing dots with "Claude" label). Add `removeTypingIndicator()` — removes it. Call `showTypingIndicator()` in `sendTextMessage()` and in `handleMessage` for `transcript` case. Call `removeTypingIndicator()` when first `assistant_chunk`, `tool_use_start`, or `thinking` arrives
   - Add `appendToolCard({id, name, input})` — creates tool card DOM inside current assistant message (or creates assistant message if none). Card shows: wrench SVG icon, humanized tool name, "Running..." status with amber left border. Store card by `data-tool-id` attribute. Input shown in collapsed `<pre>` (JSON.stringify with 2-space indent, truncated to 500 chars)
   - Add `updateToolCard({id, content, is_error})` — finds card by `[data-tool-id="${id}"]`, changes class from `loading` to `success`/`error`, updates status text to tool name or "Error", populates `.tool-output` in body (truncated to 1000 chars)
   - Add `toggleToolCard(el)` — toggles `.expanded` class
   - Add `humanizeToolName(name)` — maps: `Read`→"Read File", `Edit`→"Edit File", `Write`→"Write File", `Bash`→"Run Command", `Grep`→"Search Code", `Glob`→"Find Files", `WebSearch`→"Web Search", `WebFetch`→"Fetch URL", default→name
   - Add `appendThinkingBlock({text})` — creates thinking block inside current assistant message. Brain icon (🧠 emoji or SVG), "Thinking" label, collapsed body with thinking text
   - Add new cases in `handleMessage()` switch: `case 'tool_use_start'`: call `removeTypingIndicator()` then `appendToolCard(msg)`. `case 'tool_result'`: call `updateToolCard(msg)`. `case 'thinking'`: call `removeTypingIndicator()` then `appendThinkingBlock(msg)`. Update `case 'assistant_chunk'`: call `removeTypingIndicator()` first
   - Update `finalizeAssistantMessage()` to handle new structure — apply `simpleMarkdown()` to each text span (not the whole container)

3. [x] Phase 3: Streaming Markdown + Code Blocks + Copy — complexity: standard
   - Implement progressive markdown rendering: instead of `content.textContent += text`, maintain `_rawText` string on the assistant message element. On each `assistant_chunk`, append to `_rawText` and call a debounced `renderMarkdown()` (debounce at 80ms using `requestAnimationFrame` or setTimeout). `renderMarkdown()` applies `simpleMarkdown(_rawText)` and sets `.innerHTML` on the current text span. On `finalizeAssistantMessage()`, do one final render. Handle partial code blocks: if unclosed triple backticks exist, render the partial content as plain text (don't open a `<pre>` that never closes)
   - Enhance `simpleMarkdown()`: extract language from fenced code blocks ` ```python\n...\n``` ` → `<pre class="code-block" data-lang="python"><code>...</code></pre>`. Add language label: `<span class="code-lang">python</span>` positioned absolute top-left of pre. Add copy button: `<button class="code-copy-btn" onclick="copyCodeBlock(this)">Copy</button>` positioned absolute top-right. Support unordered lists: `- item` → `<li>item</li>` wrapped in `<ul>`. Support links: `[text](url)` → `<a href="url" target="_blank" rel="noopener">text</a>`. Support headings: `### heading` → `<strong>heading</strong><br>`
   - Add CSS for code blocks: `.code-block` (position relative). `.code-lang` (position absolute, top 4px, left 8px, font-size 10px, color `--text-dim`, text-transform uppercase). `.code-copy-btn` (position absolute, top 4px, right 4px, background `--surface-hover`, border 1px solid `--border`, border-radius 4px, padding 2px 8px, font-size 11px, color `--text-dim`, cursor pointer). `.code-copy-btn:hover` (color `--text`, background `--surface`). `.code-copy-btn.copied` (color `--success`). Add `ul` inside message (list-style disc, padding-left 20px, margin 4px 0). Add `a` inside message (color `--accent`, text-decoration underline)
   - Add `copyCodeBlock(btn)` function — finds sibling `<code>`, copies `textContent` to clipboard, changes button text to "Copied!" for 1.5s
   - Add message-level copy button: in `finalizeAssistantMessage()`, append a copy button to the assistant message (visible on hover). CSS: `.message-copy-btn` (position absolute, top 6px, right 6px, opacity 0, transition opacity 0.2s). `.message:hover .message-copy-btn, .message-copy-btn:focus` (opacity 1). Make `.message.assistant` position relative. Add `copyMessage(btn)` that copies `_rawText` to clipboard
   - Update `.message.assistant` CSS to add `position: relative` for absolute-positioned copy button

4. [x] Phase 4: Smart Scroll, Timestamps, Errors, Reconnection, Grouping — complexity: standard
   - Smart auto-scroll: add `let userScrolledUp = false;` state. Add scroll listener on `chat`: `const atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 50; userScrolledUp = !atBottom;`. Replace ALL `chat.scrollTop = chat.scrollHeight` calls (in `addMessage`, `appendAssistantChunk`, `addSystemMessage`) with `scrollIfAtBottom()` function that only scrolls if `!userScrolledUp`. In `sendTextMessage()` and when transcript received, reset `userScrolledUp = false` and scroll. Add a floating "↓" pill button (`#scrollDownBtn`) at bottom-center of chat area, shown when `userScrolledUp && newContentArrived`. On click: scroll to bottom, hide pill. CSS: `.scroll-down-btn` (position sticky, bottom 8px, align-self center, background `--accent`, color white, border-radius 20px, padding 4px 16px, font-size 12px, cursor pointer, z-index 10, display none). `.scroll-down-btn.visible` (display block)
   - Message timestamps: in `addMessage()` and when creating assistant message in `appendAssistantChunk()`, add `<span class="timestamp">HH:MM</span>` using `new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})`. CSS: `.timestamp` (font-size 10px, color `--text-dim`, opacity 0.6, display block, margin-top 4px, text-align right for user, text-align left for assistant)
   - Better error display: replace the `case 'error'` handler's `addSystemMessage()` with a new `addErrorMessage(text)` function that creates a styled error card (red left border like tool-card.error, error icon ⚠, message text, and a "Retry" button). Store `lastUserMessage` in a variable (set in `sendTextMessage` and on `transcript`). Retry button calls `sendTextMessage` with the stored text (or re-sends via WebSocket). CSS: `.error-card` (background `--surface`, border-left 3px solid `var(--accent)`, border-radius 8px, padding 10px 14px, margin 8px 0). `.error-card .retry-btn` (margin-top 8px, padding 4px 12px, border-radius 12px, border 1px solid `--border`, background `--surface-hover`, color `--text`, cursor pointer, font-size 12px)
   - Reconnection UI: add a `#reconnectBanner` div in HTML (after header, before chat). On `ws.onclose`: show banner "Connection lost. Reconnecting...", increment `reconnectAttempts` counter, display count. After 5 failures show "Unable to connect" with manual "Retry" button. On successful `ws.onopen`: if `reconnectAttempts > 0`, show brief green toast "Reconnected" that auto-hides after 2s, reset counter, hide banner. CSS: `.reconnect-banner` (background `--accent`, color white, padding 8px 16px, text-align center, font-size 13px, display none). `.reconnect-banner.visible` (display block). `.reconnect-toast` (position fixed, top 60px, left 50%, transform translateX(-50%), background `--success`, color #1a1a2e, padding 8px 20px, border-radius 20px, font-size 13px, z-index 200, animation fadeIn 0.2s)
   - Message grouping: in `addMessage()`, check if the previous message in `#chat` has the same role and was created within 30s (store `data-time` on each message as `Date.now()`). If same role within 30s: reduce gap (margin-top 2px instead of inheriting the 12px gap), hide the `.label` element. CSS: `.message.grouped` (margin-top: -8px). `.message.grouped .label` (display: none). `.message.grouped .timestamp` (display: none)

## Acceptance Criteria
- Tool calls from Claude are visible in the chat as collapsible cards with tool name, loading state, and expandable input/output
- Thinking blocks appear as collapsed cards when the model uses extended thinking
- A typing indicator (bouncing dots) appears before Claude's first response chunk
- Markdown renders progressively during streaming (not just after full response)
- Code blocks show language label and copy button
- Assistant messages have a copy button on hover
- Chat does not force-scroll when user scrolls up to read history; a "scroll down" pill appears for new messages
- Each message shows a timestamp
- Errors display as styled cards with retry functionality
- Disconnects show a reconnection banner with attempt counter
- Consecutive same-role messages within 30s are visually grouped
- Tool call text, thinking text, tool names, and tool output are NOT spoken by TTS — only text content goes to TTS
- All existing voice + text functionality continues to work (no regressions)

## Verification
Tool: Playwright
Scenarios:
- Tool call display: Navigate to voice bridge → type "read the pyproject.toml file" → verify a tool card appears with "Read File" label and loading state → verify it transitions to success with file contents in expandable body
- Thinking indicator: Type a complex question → verify thinking block appears collapsed → click to expand → verify thinking text is visible
- Typing indicator: Type a message → verify bouncing dots appear → verify they disappear when first chunk arrives
- Streaming markdown: Type "write me a hello world in python with a code block" → verify markdown formatting appears during streaming, not just at the end
- Code block copy: Find a code block in response → verify language label visible → click copy button → verify "Copied!" feedback
- Message copy: Hover over assistant message → verify copy button appears → click → verify clipboard content
- Smart scroll: Send multiple messages to fill chat → scroll up → send another message → verify chat does NOT force-scroll → verify "scroll down" pill appears → click pill → verify scrolls to bottom
- Timestamps: Verify each message shows HH:MM timestamp
- Error handling: Trigger an error → verify styled error card with retry button → click retry → verify message re-sent
- Reconnection: Stop server → verify "Connection lost" banner → restart server → verify "Reconnected" toast
- TTS isolation: Send a tool-triggering message → verify TTS only speaks the text response, not tool names or thinking
