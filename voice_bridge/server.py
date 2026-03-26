"""bridge.py — FastAPI voice bridge server.

WebSocket-based server that connects a phone browser to the Claude Agent SDK
with voice I/O. Audio is processed on the PC (Whisper STT, Kokoro TTS),
text is sent to/from the Claude Agent SDK.

Run via: agent-voice-bridge (or python -m lazy_claude.bridge_main)
"""

from __future__ import annotations

import asyncio
import re
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

from voice_bridge.audio import load_vad_model
from voice_bridge.claude import ClaudeSession
from voice_bridge.tts import SAMPLE_RATE as TTS_SAMPLE_RATE
from voice_bridge.tts import BufferedTTSEngine
from voice_bridge.vad import RemoteVADProcessor
from voice_bridge.stt import load_model as load_whisper_model
from voice_bridge.stt import transcribe

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"

# Persist token across restarts so PWA home screen links keep working.
# Token is stored in a file next to the static dir and reused on restart.
_TOKEN_FILE = Path(__file__).parent / ".auth_token"

def _load_or_create_token() -> str:
    if _TOKEN_FILE.exists():
        token = _TOKEN_FILE.read_text().strip()
        if len(token) == 64:  # 32 bytes hex
            return token
    token = secrets.token_hex(32)
    _TOKEN_FILE.write_text(token)
    return token

AUTH_TOKEN = _load_or_create_token()

# Sentence boundary pattern for incremental TTS
_SENTENCE_RE = re.compile(r"(?<=[.!?\n])\s+")
_FENCED_CODE_BLOCK_RE = re.compile(r"```[A-Za-z0-9_+-]*\n?[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# Maximum chars to buffer before forcing a TTS chunk
_MAX_SENTENCE_CHARS = 150


def _log(msg: str) -> None:
    print(f"[bridge] {msg}", file=sys.stderr, flush=True)


def _prepare_tts_text(text: str) -> str:
    """Strip markdown/control syntax before sending text to TTS.

    The UI still receives the original streamed markdown. This only cleans the
    audio path so code fences, table separators, and formatting markers do not
    get spoken as punctuation/noise.
    """
    if not text or not text.strip():
        return ""

    cleaned = _FENCED_CODE_BLOCK_RE.sub(" ", text)
    cleaned = _MARKDOWN_LINK_RE.sub(r"\1", cleaned)
    cleaned = _INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r"^\s{0,3}#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*[-*+]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*\d+\.\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*\|?[:\- ]+\|[:\-| ]*$", " ", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*(.*?)\*", r"\1", cleaned)
    cleaned = cleaned.replace("|", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _make_png(size: int, r: int, g: int, b: int) -> bytes:
    """Generate a solid-color RGB PNG using stdlib only (struct + zlib)."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    row = bytes([0]) + bytes([r, g, b] * size)  # filter=0, RGB pixels
    idat = zlib.compress(row * size, 9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def _generate_pwa_assets() -> None:
    """Generate PWA icon PNGs at startup (idempotent — skips existing files)."""
    icons_dir = _STATIC_DIR / "icons"
    icons_dir.mkdir(exist_ok=True)
    R, G, B = 233, 69, 96  # #e94560 — app accent color
    for name, size in [
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("apple-touch-icon.png", 180),
    ]:
        path = icons_dir / name
        if not path.exists():
            path.write_bytes(_make_png(size, R, G, B))
            _log(f"Generated PWA icon: {name}")


def _get_local_ip() -> str:
    """Get the machine's local network IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _get_tailscale_hostname() -> str | None:
    """Detect the machine's Tailscale HTTPS hostname (e.g. my-pc.tail1234.ts.net).

    Tries the macOS app binary first, then falls back to ``tailscale`` in PATH
    (Linux / Homebrew CLI). Returns ``None`` if Tailscale is not installed,
    not connected, or HTTPS certificates are not enabled.
    """
    import json
    import subprocess

    candidates = [
        ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "status", "--json"],
        ["tailscale", "status", "--json"],
    ]
    for cmd in candidates:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=3,
            )
            if result.returncode != 0:
                continue
            data = json.loads(result.stdout)
            dns_name: str = data.get("Self", {}).get("DNSName", "")
            if dns_name:
                return dns_name.rstrip(".")
        except Exception:
            continue
    return None


# Resolved once at import time so startup is fast and all handlers share the value.
TAILSCALE_HOSTNAME: str | None = _get_tailscale_hostname()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Voice Bridge", docs_url=None, redoc_url=None)

# How long to wait for continuation speech after a VAD segment (seconds).
# Mirrors the MCP server's _CONTINUATION_RESPONSE_TIMEOUT.
_CONTINUATION_TIMEOUT = 1.0

# Shared models (loaded once at startup via lifespan or on first connect)
_models: dict[str, Any] = {}
_active_session: BridgeSession | None = None

# Model name to use for new ClaudeSession instances — set at startup via
# set_bridge_model() when the --model CLI flag is parsed.
_BRIDGE_MODEL: str = "sonnet"


def set_bridge_model(model: str) -> None:
    """Set the Claude model for new sessions (call before load_models)."""
    global _BRIDGE_MODEL
    _BRIDGE_MODEL = model
    _log(f"Claude model set to: {model}")


class BridgeSession:
    """Manages one phone-to-PC voice session over WebSocket.

    Uses two concurrent asyncio tasks:
    - A reader task that continuously reads WebSocket messages and dispatches
      them to queues. This ensures control messages (e.g. stop_tts) are
      processed immediately even while the processor is busy.
    - A processor task that reads from those queues and runs STT/Claude/TTS.
    """

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._vad_processor = RemoteVADProcessor(
            _models["vad"],
            silence_duration=0.5,       # seconds of trailing silence to end a segment
            min_speech_duration=0.5,    # require at least 500ms of speech
            no_speech_timeout=30.0,
            speech_threshold=0.6,       # Silero must be ≥60% confident
            energy_threshold=0.01,      # RMS gate: skip very quiet frames
        )

        # Multi-segment accumulation (mirrors MCP server behaviour).
        # After VAD fires, we transcribe the segment and wait up to
        # _CONTINUATION_TIMEOUT seconds for more speech before sending
        # the accumulated text to Claude. "Over" bypasses the wait.
        self._pending_segments: list[str] = []
        self._continuation_deadline: float | None = None
        self._tts = _models["tts"]
        self._whisper_model = _models["whisper"]
        # ClaudeSession is created here but not yet connected — connect() is
        # called in run() so that the SDK client stays alive for the full
        # session lifetime (preserving multi-turn conversation context).
        self._claude = ClaudeSession(model=_BRIDGE_MODEL)
        self._tts_task: asyncio.Task | None = None
        self._stop_tts = asyncio.Event()
        self._response_lock = asyncio.Lock()  # serializes voice + text → Claude

        # TTS active flag: True while server is sending TTS audio to client.
        # Used to gate VAD and enable barge-in detection.
        self._tts_active: bool = False

        # Accumulates mic audio received during TTS for barge-in detection.
        self._barge_in_buffer: list[np.ndarray] = []

        # Queues feeding the processor from the reader
        # audio_queue holds raw bytes; control_queue holds parsed dicts
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._control_queue: asyncio.Queue[dict] = asyncio.Queue()

    async def run(self) -> None:
        """Main session loop: run reader and processor tasks concurrently."""
        await self._send_json({"type": "ready"})
        _log("Session started")

        # Connect the SDK client once for the full session lifetime so that
        # multi-turn conversation context is preserved across all messages.
        await self._claude.connect()

        reader_task = asyncio.create_task(self._reader_loop())
        processor_task = asyncio.create_task(self._processor_loop())
        text_task = asyncio.create_task(self._text_processor_loop())

        try:
            # Wait for either task to finish (disconnect or fatal error)
            done, pending = await asyncio.wait(
                [reader_task, processor_task, text_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Cancel the other tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            self._claude.cancel()
            await self._claude.close()
            if self._tts_task and not self._tts_task.done():
                self._tts_task.cancel()
            _log("Session ended")

    async def _reader_loop(self) -> None:
        """Continuously read from WebSocket and dispatch to queues.

        This runs independently of the processor so that control messages
        (e.g. stop_tts) are never blocked by STT/Claude/TTS work.
        """
        import json as _json

        try:
            while True:
                message = await self.ws.receive()

                if message.get("type") == "websocket.disconnect":
                    break

                # Binary: PCM audio from phone
                if "bytes" in message and message["bytes"]:
                    await self._audio_queue.put(message["bytes"])

                # Text/JSON: control messages — handle immediately
                elif "text" in message and message["text"]:
                    try:
                        data = _json.loads(message["text"])
                        # Handle stop_tts here directly so it's never delayed
                        if data.get("type") == "stop_tts":
                            self._stop_tts.set()
                            self._tts.stop()
                            self._claude.cancel()
                            _log("TTS interrupted by client")
                        elif data.get("type") == "vad_reset":
                            # Client switched to push mode — flush all
                            # buffered VAD state and pending segments.
                            self._vad_processor.reset()
                            self._pending_segments.clear()
                            self._continuation_deadline = None
                            # Drain any queued audio frames
                            while not self._audio_queue.empty():
                                self._audio_queue.get_nowait()
                            _log("VAD reset — flushed buffers")
                        elif data.get("type") == "playback_done":
                            # Client finished playing all buffered TTS audio.
                            # Now safe to clear _tts_active so normal VAD resumes.
                            self._tts_active = False
                            self._barge_in_buffer = []
                            _log("Client playback complete — TTS active cleared")
                        elif data.get("type") == "ping":
                            await self._send_json({"type": "pong"})
                        else:
                            await self._control_queue.put(data)
                    except _json.JSONDecodeError:
                        pass

        except WebSocketDisconnect:
            _log("Client disconnected")
        except Exception as exc:
            _log(f"Reader error: {exc}")

    async def _processor_loop(self) -> None:
        """Read from audio queue and run VAD/STT/Claude/TTS pipeline."""
        try:
            while True:
                try:
                    data = await asyncio.wait_for(self._audio_queue.get(), timeout=0.5)
                    await self._handle_audio(data)
                except asyncio.TimeoutError:
                    # No audio arrived — check if continuation deadline expired.
                    if (
                        self._continuation_deadline is not None
                        and time.monotonic() > self._continuation_deadline
                        and self._pending_segments
                    ):
                        await self._flush_accumulated_segments()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _log(f"Processor error: {exc}")

    async def _text_processor_loop(self) -> None:
        """Read text messages from control queue and run Claude/TTS pipeline."""
        try:
            while True:
                data = await self._control_queue.get()
                if data.get("type") == "text_message":
                    text = data.get("text", "").strip()
                    if text:
                        _log(f"Text input: {text[:80]}...")
                        await self._stream_claude_response(text)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _log(f"Text processor error: {exc}")

    async def _handle_audio(self, data: bytes) -> None:
        """Process incoming PCM audio from the phone."""
        # Convert Int16LE bytes to float32
        pcm_int16 = np.frombuffer(data, dtype=np.int16)
        pcm_float = pcm_int16.astype(np.float32) / 32768.0

        # During TTS playback, route audio to barge-in detector instead of VAD.
        # We don't send vad_state updates in this mode to avoid UI flickering.
        if self._tts_active:
            await self._handle_barge_in(pcm_float)
            return

        # Check if continuation deadline has expired (user stopped talking).
        if (
            self._continuation_deadline is not None
            and time.monotonic() > self._continuation_deadline
            and self._pending_segments
        ):
            await self._flush_accumulated_segments()

        # Feed to VAD
        utterance, is_speaking = self._vad_processor.feed(pcm_float)

        # Send VAD state to phone for visual feedback
        await self._send_json({"type": "vad_state", "speaking": is_speaking})

        if utterance is not None:
            await self._transcribe_and_accumulate(utterance)

    async def _handle_barge_in(self, pcm_float: np.ndarray) -> None:
        """Discard mic audio during TTS playback.

        Voice barge-in is disabled because the phone's mic picks up its own
        speaker output (echo), causing false triggers.  Users can press the
        Stop button on the UI to interrupt TTS instead.
        """
        # Simply discard — no processing needed.
        pass

    async def _transcribe_and_accumulate(self, audio: np.ndarray) -> None:
        """Transcribe a VAD segment and accumulate for multi-segment input.

        After transcription, the segment is appended to ``_pending_segments``.
        If the user said "over", we flush immediately. Otherwise we reset the
        continuation deadline — the next audio frame that arrives after the
        deadline will trigger the flush (see ``_handle_audio``).
        """
        loop = asyncio.get_event_loop()

        # 1. Transcribe with Whisper (blocking, run in executor)
        result = await loop.run_in_executor(
            None, lambda: transcribe(audio, model=self._whisper_model)
        )

        if not result.text or result.no_speech_prob > 0.6:
            _log(f"Discarding noise (no_speech_prob={result.no_speech_prob:.2f})")
            return

        text = result.text

        if text:
            self._pending_segments.append(text)
            _log(f"Segment accumulated: {text}")

        if self._pending_segments:
            # Reset continuation deadline — wait for more speech
            self._continuation_deadline = time.monotonic() + _CONTINUATION_TIMEOUT
            _log(f"Waiting up to {_CONTINUATION_TIMEOUT}s for continuation…")

    async def _flush_accumulated_segments(self) -> None:
        """Join all accumulated segments and send to Claude."""
        text = " ".join(self._pending_segments).strip()
        self._pending_segments = []
        self._continuation_deadline = None

        if not text:
            return

        _log(f"User said: {text}")

        # Send full transcript to phone
        await self._send_json({"type": "transcript", "text": text})

        # Send to Claude and stream response with incremental TTS
        await self._stream_claude_response(text)

    async def _stream_claude_response(self, user_text: str) -> None:
        """Send to Claude, stream text to phone, and do incremental TTS."""
        async with self._response_lock:
            await self._do_stream_claude_response(user_text)

    async def _do_stream_claude_response(self, user_text: str) -> None:
        """Actual implementation — called under _response_lock."""
        # Cancel any previous TTS that might still be playing on the client
        if self._tts_active:
            self._stop_tts.set()
            self._tts.stop()
            self._tts_active = False
            self._barge_in_buffer = []
            await self._send_json({"type": "tts_stop"})
            _log("Cancelled previous TTS before new response")

        self._stop_tts.clear()
        full_response: list[str] = []
        sentence_buffer = ""
        tts_queue: asyncio.Queue[str | None] = asyncio.Queue()

        # Start TTS consumer task
        tts_task = asyncio.create_task(self._tts_consumer(tts_queue))

        try:
            async for event in self._claude.send_message(user_text):
                # If stop was requested, kill Claude and bail out
                if self._stop_tts.is_set():
                    _log("Stop requested — killing Claude process")
                    self._claude.cancel()
                    break

                event_type = event.get("type")

                if event_type == "text":
                    chunk = event["text"]
                    full_response.append(chunk)
                    await self._send_json({"type": "assistant_chunk", "text": chunk})

                    # Accumulate for sentence-level TTS
                    sentence_buffer += chunk
                    sentences = _SENTENCE_RE.split(sentence_buffer)

                    if len(sentences) > 1:
                        # All but last are complete sentences
                        for sentence in sentences[:-1]:
                            sentence = _prepare_tts_text(sentence.strip())
                            if sentence:
                                _log(f"Queueing sentence for TTS: {sentence[:60]}")
                                await tts_queue.put(sentence)
                        sentence_buffer = sentences[-1]
                    elif len(sentence_buffer) > _MAX_SENTENCE_CHARS:
                        # Force TTS on long chunks without sentence boundaries
                        sentence = _prepare_tts_text(sentence_buffer.strip())
                        if sentence:
                            await tts_queue.put(sentence)
                        sentence_buffer = ""
                elif event_type == "thinking":
                    await self._send_json(
                        {"type": "thinking", "text": event.get("text", "")}
                    )
                elif event_type == "tool_use":
                    await self._send_json(
                        {
                            "type": "tool_use_start",
                            "id": event.get("id"),
                            "name": event.get("name"),
                            "input": event.get("input", {}),
                        }
                    )
                elif event_type == "tool_result":
                    await self._send_json(
                        {
                            "type": "tool_result",
                            "id": event.get("tool_use_id"),
                            "content": event.get("content", ""),
                            "is_error": event.get("is_error", False),
                        }
                    )

            # Flush remaining text (unless stopped)
            if not self._stop_tts.is_set() and sentence_buffer.strip():
                sentence = _prepare_tts_text(sentence_buffer.strip())
                if sentence:
                    await tts_queue.put(sentence)

            # Signal TTS consumer to finish
            await tts_queue.put(None)

            response_text = "".join(full_response)
            await self._send_json(
                {"type": "assistant_done", "text": response_text}
            )
            _log(f"Claude responded: {response_text[:100]}...")

        except Exception as exc:
            _log(f"Error streaming Claude response: {exc}")
            await tts_queue.put(None)
        finally:
            await tts_task

    async def _tts_consumer(self, queue: asyncio.Queue[str | None]) -> None:
        """Consume sentences from queue and stream TTS audio to phone.

        Audio chunks are yielded one-by-one from the synthesize generator
        via a thread executor, so the stop event is checked between each
        chunk and interruption takes effect immediately.
        """
        loop = asyncio.get_event_loop()

        # Signal that TTS is active so _handle_audio routes to barge-in path.
        self._tts_active = True
        self._barge_in_buffer = []

        await self._send_json({"type": "tts_start"})
        _log("TTS consumer started")

        try:
            while True:
                sentence = await queue.get()
                _log(f"TTS consumer got: {repr(sentence)[:60]}")
                if sentence is None:
                    break

                if self._stop_tts.is_set():
                    # Drain remaining items
                    while not queue.empty():
                        queue.get_nowait()
                    break

                # Run the blocking synthesize generator in a thread executor,
                # but yield chunks back to the async loop one at a time so
                # the stop event can interrupt mid-sentence.
                chunk_queue: asyncio.Queue[np.ndarray | None] = asyncio.Queue()

                def _run_synthesis(s: str) -> None:
                    """Run synthesis in thread, push chunks to async queue."""
                    try:
                        _log(f"Synthesizing: {s[:60]}")
                        count = 0
                        for c in self._tts.synthesize(s):
                            count += 1
                            loop.call_soon_threadsafe(chunk_queue.put_nowait, c)
                        _log(f"Synthesis done: {count} chunks")
                    except Exception as exc:
                        _log(f"TTS synthesis error: {exc}")
                    finally:
                        loop.call_soon_threadsafe(chunk_queue.put_nowait, None)

                synth_future = loop.run_in_executor(
                    None, lambda s=sentence: _run_synthesis(s)
                )

                # Stream chunks as they arrive from the synthesis thread
                interrupted = False
                while True:
                    chunk = await chunk_queue.get()
                    if chunk is None:
                        break
                    if self._stop_tts.is_set():
                        interrupted = True
                        # Signal TTS engine to stop early
                        self._tts.stop()
                        break
                    # Convert float32 to Int16LE for phone
                    int16_data = (chunk * 32767).clip(-32768, 32767).astype(
                        np.int16
                    )
                    await self.ws.send_bytes(int16_data.tobytes())

                await synth_future  # ensure thread completes

                if interrupted or self._stop_tts.is_set():
                    # Drain remaining sentences from queue
                    while not queue.empty():
                        queue.get_nowait()
                    break

        except Exception as exc:
            _log(f"TTS consumer error: {exc}")
        finally:
            await self._send_json({"type": "tts_end"})
            self._barge_in_buffer = []
            # NOTE: _tts_active stays True until client sends playback_done.
            # This ensures "stop" barge-in works during client-side audio
            # playback (server finishes sending audio before client finishes
            # playing it).

    async def _send_json(self, data: dict) -> None:
        """Send a JSON message to the phone, ignoring errors."""
        try:
            import json
            await self.ws.send_text(json.dumps(data))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def serve_ui(token: str = Query("")):
    """Serve the mobile web UI."""
    index_path = _STATIC_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>Voice Bridge</h1><p>index.html not found</p>")
    html = index_path.read_text()
    # Inject tokenized manifest so PWA start_url includes the auth token.
    # iOS standalone apps have isolated storage — localStorage from Safari
    # is NOT shared, so the token must be in the manifest start_url.
    if token:
        manifest_href = f"/manifest.json?token={quote(token)}"
        html = html.replace(
            '<link rel="manifest" href="/manifest.json">',
            f'<link rel="manifest" href="{manifest_href}">',
        )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/manifest.json")
async def serve_manifest(token: str = Query("")):
    """Serve the PWA web app manifest with tokenized start_url."""
    manifest = {
        "name": "Voice Bridge",
        "short_name": "VoiceBridge",
        "description": "Voice-to-Claude AI assistant",
        "start_url": f"/?token={token}" if token else "/",
        "display": "standalone",
        "background_color": "#1a1a2e",
        "theme_color": "#e94560",
        "orientation": "portrait",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return JSONResponse(manifest, media_type="application/manifest+json", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/sw.js")
async def serve_sw():
    """Serve the PWA service worker (must be at root scope)."""
    return Response(
        (_STATIC_DIR / "sw.js").read_bytes(),
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@app.get("/icons/{filename}")
async def serve_icon(filename: str):
    """Serve PWA icons."""
    if not filename.endswith(".png") or "/" in filename:
        raise HTTPException(404)
    path = _STATIC_DIR / "icons" / filename
    if not path.exists():
        raise HTTPException(404)
    return Response(
        path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/health")
async def health():
    """Health check endpoint."""
    import os as _os

    from voice_bridge.claude import ClaudeSession

    # Determine which auth method is configured (if any)
    auth_method: str | None = None
    if _os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        auth_method = "CLAUDE_CODE_OAUTH_TOKEN"
    elif _os.environ.get("ANTHROPIC_API_KEY"):
        auth_method = "ANTHROPIC_API_KEY"

    return {
        "status": "ready" if _models.get("whisper") else "loading",
        "models_loaded": list(_models.keys()),
        "sdk_available": ClaudeSession.check_available(),
        "auth_method": auth_method,
        "model": _BRIDGE_MODEL,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query("")):
    """WebSocket endpoint for voice communication."""
    global _active_session

    # Auth check
    if token != AUTH_TOKEN:
        await ws.close(code=4001, reason="Invalid token")
        _log("Rejected connection: invalid token")
        return

    # Origin validation — allow localhost, the server's own LAN IP,
    # and any extra origins supplied via BRIDGE_ALLOWED_ORIGIN env var.
    # This prevents cross-site WebSocket hijacking from untrusted pages.
    # Set BRIDGE_ALLOWED_ORIGIN=* to disable the check (e.g. for tunnels).
    import os as _os
    origin = ws.headers.get("origin", "")
    if origin:
        extra_origin = _os.environ.get("BRIDGE_ALLOWED_ORIGIN", "")
        if extra_origin != "*":
            allowed_prefixes = [
                "http://localhost",
                "http://127.0.0.1",
            ]
            local_ip = _get_local_ip()
            if local_ip != "127.0.0.1":
                allowed_prefixes.append(f"http://{local_ip}")
            if TAILSCALE_HOSTNAME:
                allowed_prefixes.append(f"https://{TAILSCALE_HOSTNAME}")
            if extra_origin:
                allowed_prefixes.append(extra_origin)

            if not any(origin.startswith(p) for p in allowed_prefixes):
                await ws.close(code=1008, reason="Invalid origin")
                _log(f"Rejected connection: invalid origin '{origin}'")
                return

    # Single session enforcement
    if _active_session is not None:
        _log("Closing existing session for new connection")
        try:
            await _active_session.ws.close(code=4002, reason="Replaced by new connection")
        except Exception:
            pass

    await ws.accept()
    _log("WebSocket connected")

    session = BridgeSession(ws)
    _active_session = session

    try:
        await session.run()
    finally:
        # Only clear if we still own the reference; a newer connection may
        # have already replaced _active_session before our finally runs.
        if _active_session is session:
            _active_session = None


def load_models() -> None:
    """Eagerly load all ML models. Call at startup."""
    _log("Loading models (this may take 10-15 seconds on first run)...")

    _log("Loading VAD model...")
    _models["vad"] = load_vad_model()

    _log("Loading Whisper model...")
    _models["whisper"] = load_whisper_model()

    _log("Loading TTS engine...")
    _models["tts"] = BufferedTTSEngine()

    _log("All models loaded and ready.")
    _generate_pwa_assets()
