# voice-bridge Architecture

## Overview

**voice-bridge** is a FastAPI WebSocket server that enables bidirectional voice and text communication between a phone browser and Claude Code on a PC. It functions as a remote voice bridge, running all audio processing locally on the PC while serving a lightweight web UI to the phone.

```
┌─────────────────────┐                      ┌──────────────────────┐
│   Phone Browser     │   WebSocket (HTTPS)  │   voice-bridge PC    │
├─────────────────────┤◄────────────────────►├──────────────────────┤
│ • Audio capture     │                      │ • Audio processing   │
│ • Voice UI          │  Binary: PCM audio   │ • STT (Whisper)     │
│ • Chat interface    │  JSON: control msgs  │ • TTS (Kokoro)      │
│                     │                      │ • VAD (Silero)      │
│                     │                      │ • Claude Agent SDK   │
└─────────────────────┘                      └──────────────────────┘
```

## Key Design Principles

1. **Separation of concerns**: Audio processing, ML models, and Claude communication are in isolated modules
2. **Async-first**: FastAPI + asyncio for responsive WebSocket handling while background work runs in thread executors
3. **Half-duplex audio**: Microphone auto-mutes during TTS playback to prevent echo
4. **Multi-segment accumulation**: User can speak multiple segments before saying "over", then they're joined and sent to Claude
5. **Incremental TTS**: Claude's response is streamed sentence-by-sentence to TTS for faster time-to-first-audio

## Project Structure

```
voice_bridge/
├── __init__.py          # Package definition
├── __main__.py          # Entry point: argument parsing & server startup
├── server.py            # FastAPI app, BridgeSession class, WebSocket logic
├── claude.py            # Claude Agent SDK client
├── stt.py               # Whisper.cpp speech-to-text
├── tts.py               # Kokoro text-to-speech (not used in bridge)
├── audio.py             # Low-level audio utilities & VAD model loader
├── vad.py               # RemoteVADProcessor for speech boundary detection
└── static/
    ├── index.html       # Mobile web UI
    ├── app.js           # Client-side JavaScript
    └── ...
```

## Core Modules

### 1. `__main__.py` — Entry Point

Responsibilities:
- Parse command-line arguments (`--host`, `--port`, `--model`)
- Check `claude_agent_sdk` importability (fail fast if missing)
- Verify auth env vars are present (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`)
- Eagerly load ML models (VAD, Whisper, TTS) at startup
- Print access URLs (local IP + token)
- Start uvicorn server

Key functions:
- `run_bridge()`: Main entry point
- `_get_local_ip()`: Get machine's LAN IP for user-facing URL
- `_log()`: Debug logging to stderr

### 2. `server.py` — FastAPI & WebSocket Logic

The heart of the system. Implements:

#### `BridgeSession` Class
Manages a single phone-to-PC voice session. Key aspects:

**Three concurrent asyncio tasks:**
1. **`_reader_loop()`** — Continuously reads from WebSocket
   - Binary frames → audio queue
   - JSON messages → control queue (unless `stop_tts`, which is handled immediately)
   - Never blocked by STT/Claude/TTS work, ensuring low-latency stop

2. **`_processor_loop()`** — Processes audio through VAD → STT → accumulation
   - Feeds PCM to VAD (energy gate + Silero probability threshold)
   - When VAD fires (speech segment detected), transcribe with Whisper
   - Accumulate segments with continuation timeout
   - After timeout expires, flush accumulated text to Claude

3. **`_text_processor_loop()`** — Handles typed text input
   - Reads from control queue
   - Sends directly to Claude (bypasses VAD)

**Key methods:**
- `run()` — Spawns the three tasks and waits for graceful shutdown
- `_handle_audio(pcm_float)` — Entry point for audio processing
- `_handle_barge_in(pcm_float)` — Discards mic input during TTS (echo prevention)
- `_flush_accumulated_segments()` — Joins pending segments, sends to Claude
- `_stream_claude_response(user_text)` — Main Claude/TTS pipeline
- `_do_stream_claude_response()` — Implementation (runs under `_response_lock`)
- `_tts_consumer(queue)` — Consumes sentences from queue, synthesizes and streams audio

**State management:**
- `_pending_segments`: Multi-segment buffer
- `_continuation_deadline`: Timeout for "over" detection
- `_tts_active`: True while server is streaming TTS to client
- `_response_lock`: Serializes voice + text input (prevents concurrent Claude calls)

#### Key Routes:
- `GET /` — Serve mobile UI (`index.html`)
- `GET /health` — Health check (model loading status, SDK availability, auth method)
- `WebSocket /ws` — Voice communication endpoint
  - Auth: token query parameter validation
  - Origin: CORS-like validation (localhost, LAN IP, tunnel origins)
  - Single-session enforcement: New connection closes previous one

#### Globals:
- `_models`: Dict holding loaded VAD, Whisper, and TTS instances
- `_active_session`: Current BridgeSession (single session at a time)
- `AUTH_TOKEN`: 64-character hex token generated at startup
- `_BRIDGE_MODEL`: Claude model name (set by `set_bridge_model()` at startup)

### 3. `claude.py` — Claude Agent SDK Client

Wraps the `claude-agent-sdk` Python package for multi-turn conversations.
The SDK bundles the Claude Code CLI internally — no subprocess management
or JSON stdout parsing is needed.

**Authentication:**
- `CLAUDE_CODE_OAUTH_TOKEN` — Claude Max subscription (OAuth, long-lived token)
  - Generate with: `claude setup-token` (interactive prompt, saves to `~/.claude/auth.json`)
  - Only works if you have a Claude Max subscription
- `ANTHROPIC_API_KEY` — Anthropic API key (pay-per-use billing)
  - Get from: https://console.anthropic.com/account/keys
- At least one must be set; startup checks this before loading models.
- Priority: `CLAUDE_CODE_OAUTH_TOKEN` is checked first; falls back to `ANTHROPIC_API_KEY`

**ClaudeSession class:**
- Configures `ClaudeAgentOptions` with `allowed_tools=[]` (chat-only, no
  file/bash access), `permission_mode="acceptEdits"`, `max_turns=100`
- Accepts `model` parameter (default: `"sonnet"`) passed from `--model` flag
- Uses `async with ClaudeSDKClient(options)` as persistent session
- Calls `client.query(text)` then streams `client.receive_response()`
- Extracts text from `AssistantMessage` / `TextBlock` objects

**Key methods:**
- `send_message(text)` → AsyncGenerator[str] — Send text, yield response chunks
- `check_available()` — Static method: checks `claude_agent_sdk` is importable
- `cancel()` — Sets cancellation flag and calls `client.interrupt()`

### 4. `audio.py` — Low-Level Audio & VAD Model

Provides:
- `SileroVAD` class: Wraps onnxruntime Silero model
- `VadStateMachine`: State transitions (idle → speaking → trailing silence)
- `load_vad_model()` → SileroVAD instance
- Constants: `SAMPLE_RATE = 16000`, `CHUNK_SAMPLES = 512`

**Silero model details:**
- Runs on CPU via onnxruntime
- Input: 512-sample (32ms) frames at 16 kHz
- Output: 0–1 probability score (voice probability)
- Very low latency (~1ms per frame)

### 5. `vad.py` — Remote VAD Processor

Wraps the Silero model for streaming audio. Key design:

**RemoteVADProcessor class:**
- Accepts arbitrary-sized PCM chunks (rechunks internally to 512-sample VAD frames)
- Energy gate: Skip very quiet frames before Silero (optional)
- Silero threshold: Probability > 0.6 = speech
- Silence duration: 0.5s of trailing silence ends an utterance
- Min speech: Ignore segments < 0.5s
- No-speech timeout: Give up after 30s of silence

**Key methods:**
- `feed(pcm_float)` → (utterance_audio, is_speaking)
  - Returns complete utterance when VAD detects speech + silence
  - Returns (None, is_speaking) if still accumulating
- `reset()` — Flush internal buffer and state machine

### 6. `stt.py` — Whisper Speech-to-Text

Wraps pywhispercpp for STT. Key design:

**API:**
- `load_model(model_name="small.en")` → Model instance
- `transcribe(audio: np.ndarray, model)` → TranscribeResult

**TranscribeResult:**
- `text`: Recognized speech (stripped of hallucination artifacts)
- `no_speech_prob`: Average confidence that audio was silence (0–1)

**Artifact stripping:**
- Removes `[BLANK_AUDIO]`, `[MUSIC]`, `[NOISE]`, `[SILENCE]` tokens
- Cleans up extraneous whitespace

## Data Flow

### Voice Input (Microphone → Claude)

```
1. Phone sends PCM audio frames (16-bit int, 16 kHz) via WebSocket binary
2. server._reader_loop() → audio_queue
3. server._processor_loop() reads audio, feeds to RemoteVADProcessor
   a. VAD energy gate filters very quiet frames
   b. Silero model (0.5s chunks) detects speech probability
   c. When speech + silence detected → returns complete utterance
4. Whisper transcribes utterance → TranscribeResult (text + no_speech_prob)
5. Text accumulated in _pending_segments, waits for continuation timeout
6. After timeout or "over" → segments joined → send to Claude
7. ClaudeSDKClient.query() called, response streamed via receive_response()
8. Response text accumulates in sentence_buffer
9. Complete sentences → TTS queue
10. TTS consumer synthesizes 24 kHz audio → sends via WebSocket binary
11. Phone plays audio, sends playback_done → server clears _tts_active
```

### Text Input (Typed → Claude)

```
1. Phone sends JSON: {"type": "text_message", "text": "..."}
2. server._reader_loop() → control_queue
3. server._text_processor_loop() reads from control_queue
4. Bypasses VAD, sends directly to _stream_claude_response()
5. Same Claude/TTS flow as voice input
```

### WebSocket Message Protocol

**Control messages (JSON, text):**
- `{"type": "ready"}` — Server → Phone: session ready
- `{"type": "vad_state", "speaking": bool}` — Server → Phone: VAD activity
- `{"type": "transcript", "text": "..."}` — Server → Phone: recognized speech
- `{"type": "assistant_chunk", "text": "..."}` — Server → Phone: Claude response chunk
- `{"type": "assistant_done", "text": "..."}` — Server → Phone: full response (for replay)
- `{"type": "tts_start"}` — Server → Phone: TTS audio beginning
- `{"type": "tts_end"}` — Server → Phone: TTS audio finished
- `{"type": "text_message", "text": "..."}` — Phone → Server: typed text
- `{"type": "stop_tts"}` — Phone → Server: interrupt TTS
- `{"type": "vad_reset"}` — Phone → Server: flush VAD state (switch to push mode)
- `{"type": "playback_done"}` — Phone → Server: client finished playing TTS

**Audio frames (binary):**
- 16-bit signed integer PCM at 16 kHz, mono
- Phone → Server: raw mic audio
- Server → Phone: 24 kHz float32 TTS output (converted to 16-bit int on server)

## Audio Pipeline

### Microphone (Phone) → Whisper

```
Phone (16 kHz mic)
  ↓
WebSocket binary (16-bit int PCM)
  ↓
numpy.frombuffer() → float32 / 32768.0
  ↓
RemoteVADProcessor.feed()
  • Rechunk to 512-sample VAD frames
  • Energy gate: skip if RMS < 0.01
  • Silero VAD: probability > 0.6 = speech
  ↓
Complete utterance detected (speech + 0.5s silence)
  ↓
Whisper.cpp (small.en model)
  • Input: 16 kHz float32 audio
  • Output: text + no_speech_prob
  ↓
Result
```

### Claude → TTS (Kokoro) → Phone

```
Claude Agent SDK response stream (AssistantMessage TextBlocks)
  ↓
sentence_buffer accumulates chunks
  ↓
Complete sentence detected (ends with . ! ? or newline)
  ↓
TTS queue.put(sentence)
  ↓
_tts_consumer() task:
  • Kokoro.synthesize(sentence) → 24 kHz float32 chunks
  • Convert to 16-bit int: chunk * 32767
  • Send via WebSocket binary
  ↓
Phone receives, buffers, plays audio
  ↓
playback_done message → server clears _tts_active
```

## Session Management

### Concurrency Model

**Three independent asyncio tasks per session:**
1. Reader (I/O) — Never blocks on computation
2. Processor (audio/VAD/STT) — Blocking work in executor
3. Text processor (text input) — Waits on control queue

**Synchronization:**
- `_response_lock` (asyncio.Lock): Serializes voice and text input
  - Prevents concurrent `send_message()` to Claude
  - Ensures one response at a time
- `_stop_tts` (asyncio.Event): Signal TTS to stop
- Queues: audio_queue, control_queue, tts_queue

### Single-Session Enforcement

- `_active_session` global holds current BridgeSession
- New WebSocket connection → closes previous session
- Prevents multiple phones connecting simultaneously

### Graceful Shutdown

1. Reader task finishes (WebSocketDisconnect)
2. Processor/text tasks cancelled
3. Finally block: Claude SDK client cancelled, TTS task cancelled
4. Session marked as inactive

## Model Loading & Startup

Models load eagerly at startup (`load_models()` in `__main__.py`):

1. **VAD (Silero)** — ~100 MB, onnxruntime CPU
2. **Whisper** — ~140 MB for small.en, pywhispercpp
3. **TTS (Kokoro)** — ~150 MB, HuggingFace transformers + PyTorch

Total: ~400 MB on first download, ~10–15 seconds to load.

**Why eager loading?**
- Startup cost paid once, not per session
- Smooth UX: WebSocket connects immediately, ready to record
- Models stay in memory across sessions

## Environment & Configuration

### Environment Variables
- `BRIDGE_ALLOWED_ORIGIN` — Extra allowed WebSocket origin (set to `*` for tunnels)
- `PYTORCH_ENABLE_MPS_FALLBACK` — Fallback to CPU if MPS unavailable
- `CLAUDE_CODE_OAUTH_TOKEN` — Claude Max OAuth token (run `claude setup-token` to generate)
- `ANTHROPIC_API_KEY` — Anthropic API key for pay-per-use billing

### Command-Line Arguments
- `--host` — Bind address (default: `0.0.0.0`)
- `--port` — Port (default: `8787`)
- `--model` — Claude model alias (default: `sonnet`; options: `sonnet`, `opus`, `haiku`)

### Security
- **Auth token** — 64-character hex, validated on every WebSocket connection
- **Origin validation** — CORS-like check (localhost, LAN IP, extra origins)
- **Single session** — Prevents multiple simultaneous connections

## Error Handling & Logging

**Logging:**
- All debug output goes to stderr (stdout reserved for protocols)
- Prefixed with module name: `[bridge]`, `[bridge-vad]`, etc.

**Graceful degradation:**
- Reader loop errors don't kill processor
- Processor errors don't kill text loop
- TTS errors don't crash session (error logged, continue)
- JSON decode errors ignored

**Critical failures:**
- `claude-agent-sdk` not importable → exit at startup with install instructions
- No auth env vars (`CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY`) → exit at startup
- Model loading failures → exit at startup

## Performance Considerations

1. **VAD latency** — 32ms per frame (512 samples @ 16 kHz)
2. **Whisper latency** — ~500ms per utterance (depends on audio length)
3. **TTS latency** — ~100–500ms per sentence (depends on text length)
4. **Echo prevention** — Mic muted during TTS playback (barge-in disabled)
5. **Continuation timeout** — 1 second (mirrors MCP server behavior)

## Testing & Development

To test locally:

```bash
# Start server
voice-bridge --port 8787

# Open in browser (localhost)
curl http://localhost:8787/?token=<token>

# Test WebSocket
# Use static/index.html or write a client script
```

Key test scenarios:
- Single-segment input ("hello over")
- Multi-segment input ("hello" ... "world over")
- Text input (bypass voice)
- TTS interruption (Stop button)
- Rapid successive messages (serialize via _response_lock)
- Network disconnection (graceful cleanup)

## Troubleshooting

### Authentication Issues

**Error: "No authentication method available"**
- Check that `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` is set
- For OAuth: run `claude setup-token` and export the token
- For API key: get from https://console.anthropic.com/account/keys and export it

**OAuth token expired or invalid**
- Run `claude setup-token` again to refresh the token
- Export the new token: `export CLAUDE_CODE_OAUTH_TOKEN=<new_token>`

### WebSocket Connection Issues

**"WebSocket connection failed" on phone**
- Ensure HTTPS is used (not plain HTTP)
- Check that `BRIDGE_ALLOWED_ORIGIN` is set to `*` if using Cloudflare Tunnel or Tailscale
- Verify the token in the URL matches the one printed when server started

**"Microphone not working"**
- Ensure page is loaded over HTTPS (required by browsers)
- Check phone's microphone permissions for the browser
- Try Cloudflare Tunnel if LAN IP fails

### Model Loading Issues

**"Model loading timed out"**
- First startup takes 10–15 seconds to load VAD, Whisper, and TTS models
- Subsequent startups are faster (models cached locally)
- Check disk space for model downloads (~400 MB)

**"onnxruntime import failed"**
- Install onnxruntime: `pip install onnxruntime`
- May need to reinstall: `pip install -e . --force-reinstall`

### Performance Issues

**High latency or stuttering**
- Check CPU usage during TTS and Whisper processing
- VAD/Whisper use threading; TTS can be slow on CPU
- Try smaller model: `--model haiku` instead of `--model opus`
- Ensure no other heavy processes are running

**Echo or feedback during TTS**
- Mic auto-mutes during TTS playback (barge-in disabled by design)
- If hearing echo from speakers: reduce phone volume or move away from speaker

## Future Enhancements

Possible improvements:
- Audio compression (reduce WebSocket bandwidth)
- Barge-in detection (interrupt TTS on strong speech)
- Voice wake-word detection (always-listening mode)
- Faster models (Tiny Whisper, DiT for faster TTS)
- Session persistence (resume across restarts)
- Multi-user support (multiple concurrent sessions)
