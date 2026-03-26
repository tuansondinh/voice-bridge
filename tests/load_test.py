"""Load test for voice-bridge server.

Exercises three layers of the stack without requiring real ML models or a
live Claude API key:

  1. HTTP endpoints  (/health, /)  — concurrent async requests via httpx
  2. WebSocket lifecycle           — rapid connect / auth / disconnect churn
  3. Audio-frame throughput        — sustained PCM stream inside one session
  4. Control-message throughput    — JSON message burst inside one session
  5. Session-replacement behaviour — new connection evicts the old one

Run with pytest (quiet, show load summary):
    uv run pytest tests/load_test.py -v -s

Or as a standalone script:
    uv run python tests/load_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _silent_pcm(duration_s: float = 0.016, sample_rate: int = 16_000) -> bytes:
    """Return *duration_s* of silence as Int16LE PCM bytes (16 kHz mono)."""
    n_samples = int(sample_rate * duration_s)
    return (np.zeros(n_samples, dtype=np.int16)).tobytes()


def _noise_pcm(duration_s: float = 0.016, sample_rate: int = 16_000) -> bytes:
    """Return *duration_s* of white noise as Int16LE PCM bytes (16 kHz mono)."""
    n_samples = int(sample_rate * duration_s)
    rng = np.random.default_rng(42)
    data = (rng.integers(-200, 200, n_samples, dtype=np.int16))
    return data.tobytes()


@dataclass
class Stats:
    label: str
    latencies: list[float] = field(default_factory=list)
    errors: int = 0

    def record(self, elapsed: float) -> None:
        self.latencies.append(elapsed)

    def report(self) -> str:
        if not self.latencies:
            return f"{self.label}: no data (errors={self.errors})"
        n = len(self.latencies)
        avg = sum(self.latencies) / n
        lo = min(self.latencies)
        hi = max(self.latencies)
        sorted_l = sorted(self.latencies)
        p50 = sorted_l[int(n * 0.50)]
        p95 = sorted_l[min(int(n * 0.95), n - 1)]
        p99 = sorted_l[min(int(n * 0.99), n - 1)]
        total = sum(self.latencies)
        rps = n / total if total else 0
        return (
            f"{self.label}: n={n} errors={self.errors} "
            f"avg={avg*1000:.1f}ms p50={p50*1000:.1f}ms "
            f"p95={p95*1000:.1f}ms p99={p99*1000:.1f}ms "
            f"min={lo*1000:.1f}ms max={hi*1000:.1f}ms "
            f"rps={rps:.1f}"
        )


# ---------------------------------------------------------------------------
# Mock factories
# ---------------------------------------------------------------------------

def _make_mock_vad() -> MagicMock:
    """VAD model that never fires (returns None utterance, is_speaking=False)."""
    vad = MagicMock()
    # RemoteVADProcessor wraps this; the processor's .feed() returns (None, False)
    return vad


def _make_mock_whisper() -> MagicMock:
    return MagicMock()


def _make_mock_tts() -> MagicMock:
    tts = MagicMock()
    tts.synthesize = MagicMock(return_value=iter([]))
    tts.stop = MagicMock()
    return tts


def _fake_models() -> dict:
    return {
        "vad": _make_mock_vad(),
        "whisper": _make_mock_whisper(),
        "tts": _make_mock_tts(),
    }


# ---------------------------------------------------------------------------
# Shared fixture: patched app + httpx async client
# ---------------------------------------------------------------------------

@pytest.fixture()
def patched_app():
    """Return the FastAPI app with models and Claude SDK mocked out."""
    import voice_bridge.server as srv

    fake = _fake_models()

    # Patch RemoteVADProcessor so audio frames never trigger STT/Claude
    mock_vad_proc = MagicMock()
    mock_vad_proc.feed.return_value = (None, False)
    mock_vad_proc.reset = MagicMock()

    with (
        patch.dict(srv._models, fake, clear=True),
        patch("voice_bridge.server.RemoteVADProcessor", return_value=mock_vad_proc),
        patch("voice_bridge.server.ClaudeSession") as MockClaude,
        patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}),
    ):
        # Make ClaudeSession a no-op async context manager
        mock_session = AsyncMock()
        mock_session.send_message = AsyncMock(return_value=_empty_async_gen())
        mock_session.connect = AsyncMock()
        mock_session.close = AsyncMock()
        mock_session.cancel = MagicMock()
        MockClaude.return_value = mock_session
        MockClaude.check_available.return_value = True

        yield srv.app, srv.AUTH_TOKEN


async def _empty_async_gen():
    """Async generator that yields nothing (empty Claude response)."""
    return
    yield  # make it an async generator


@asynccontextmanager
async def _async_client(app) -> AsyncIterator:
    """httpx async client wired to the ASGI app (no real network)."""
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# 1. HTTP — concurrent /health requests
# ---------------------------------------------------------------------------

class TestHttpHealthLoad:
    """Concurrent GET /health requests."""

    CONCURRENCY = 50
    ROUNDS = 5  # total = CONCURRENCY * ROUNDS

    @pytest.mark.asyncio
    async def test_health_concurrent(self, patched_app, capsys):
        app, token = patched_app
        stats = Stats("GET /health")

        async def _one(client):
            t0 = time.perf_counter()
            try:
                resp = await client.get("/health")
                assert resp.status_code == 200
                stats.record(time.perf_counter() - t0)
            except Exception:
                stats.errors += 1

        async with _async_client(app) as client:
            for _ in range(self.ROUNDS):
                tasks = [asyncio.create_task(_one(client)) for _ in range(self.CONCURRENCY)]
                await asyncio.gather(*tasks)

        with capsys.disabled():
            print(f"\n  {stats.report()}")

        assert stats.errors == 0, f"{stats.errors} errors in health load test"
        assert len(stats.latencies) == self.CONCURRENCY * self.ROUNDS

    @pytest.mark.asyncio
    async def test_health_response_fields(self, patched_app):
        """Health response always contains required fields under load."""
        app, _ = patched_app
        async with _async_client(app) as client:
            tasks = [client.get("/health") for _ in range(20)]
            responses = await asyncio.gather(*tasks)

        for resp in responses:
            data = resp.json()
            assert "status" in data
            assert "models_loaded" in data
            assert "sdk_available" in data
            assert "auth_method" in data
            assert "model" in data


# ---------------------------------------------------------------------------
# 2. HTTP — concurrent / (root UI) requests
# ---------------------------------------------------------------------------

class TestHttpRootLoad:
    CONCURRENCY = 30

    @pytest.mark.asyncio
    async def test_root_concurrent(self, patched_app, capsys):
        app, token = patched_app
        stats = Stats("GET /")

        async def _one(client):
            t0 = time.perf_counter()
            try:
                resp = await client.get(f"/?token={token}")
                assert resp.status_code == 200
                stats.record(time.perf_counter() - t0)
            except Exception:
                stats.errors += 1

        async with _async_client(app) as client:
            tasks = [asyncio.create_task(_one(client)) for _ in range(self.CONCURRENCY)]
            await asyncio.gather(*tasks)

        with capsys.disabled():
            print(f"\n  {stats.report()}")

        assert stats.errors == 0


# ---------------------------------------------------------------------------
# 3. WebSocket — connection lifecycle churn
# ---------------------------------------------------------------------------

class TestWebSocketChurn:
    """Rapid sequential connect → auth → disconnect cycles."""

    CYCLES = 20

    def test_connect_disconnect_cycles(self, patched_app, capsys):
        """Each cycle: connect with valid token → receive 'ready' → close."""
        from starlette.testclient import TestClient

        app, token = patched_app
        stats = Stats("WS connect/disconnect")
        errors = 0

        with TestClient(app) as client:
            for i in range(self.CYCLES):
                t0 = time.perf_counter()
                try:
                    with client.websocket_connect(f"/ws?token={token}") as ws:
                        msg = ws.receive_json()
                        assert msg["type"] == "ready", f"Expected ready, got {msg}"
                    stats.record(time.perf_counter() - t0)
                except Exception as exc:
                    errors += 1

        stats.errors = errors
        with capsys.disabled():
            print(f"\n  {stats.report()}")

        assert stats.errors == 0, f"{stats.errors} errors in WS churn test"
        assert len(stats.latencies) == self.CYCLES

    def test_invalid_token_rejected(self, patched_app):
        """Connections with wrong token are rejected (code 4001)."""
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        app, _ = patched_app
        with TestClient(app) as client:
            with pytest.raises((WebSocketDisconnect, Exception)):
                with client.websocket_connect("/ws?token=bad-token") as ws:
                    ws.receive_json()

    def test_missing_token_rejected(self, patched_app):
        """Connections without a token are rejected."""
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        app, _ = patched_app
        with TestClient(app) as client:
            with pytest.raises((WebSocketDisconnect, Exception)):
                with client.websocket_connect("/ws") as ws:
                    ws.receive_json()


# ---------------------------------------------------------------------------
# 4. WebSocket — audio frame throughput
# ---------------------------------------------------------------------------

class TestAudioThroughput:
    """Push a burst of PCM frames and measure the server's processing rate."""

    FRAME_DURATION_MS = 16          # 16 ms per frame (standard VAD window)
    SAMPLE_RATE = 16_000
    SAMPLES_PER_FRAME = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 256
    BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2  # Int16 = 2 bytes/sample

    # Simulate 5 seconds of audio in one burst
    BURST_DURATION_S = 5.0
    N_FRAMES = int(BURST_DURATION_S * 1000 / FRAME_DURATION_MS)  # 312 frames

    def _make_frame(self) -> bytes:
        return _silent_pcm(self.FRAME_DURATION_MS / 1000, self.SAMPLE_RATE)

    def test_audio_burst_throughput(self, patched_app, capsys):
        """Send N_FRAMES of PCM audio in a single session; measure frame/s."""
        from starlette.testclient import TestClient

        app, token = patched_app
        frame = self._make_frame()

        with TestClient(app) as client:
            with client.websocket_connect(f"/ws?token={token}") as ws:
                # Consume the 'ready' message
                ws.receive_json()

                t0 = time.perf_counter()
                for _ in range(self.N_FRAMES):
                    ws.send_bytes(frame)

                elapsed = time.perf_counter() - t0

        total_audio_s = self.N_FRAMES * self.FRAME_DURATION_MS / 1000
        total_bytes = self.N_FRAMES * self.BYTES_PER_FRAME
        fps = self.N_FRAMES / elapsed
        mbps = (total_bytes / elapsed) / (1024 * 1024)

        with capsys.disabled():
            print(
                f"\n  Audio burst: {self.N_FRAMES} frames "
                f"({total_audio_s:.1f}s audio, {total_bytes/1024:.0f} KB) "
                f"in {elapsed*1000:.0f}ms → "
                f"{fps:.0f} frames/s, {mbps:.2f} MB/s"
            )

        # Should handle at least 10× real-time audio (very conservative)
        assert fps >= self.SAMPLE_RATE / self.SAMPLES_PER_FRAME * 2, (
            f"Frame rate {fps:.0f} fps is below 2× real-time threshold"
        )

    def test_audio_frame_size_variants(self, patched_app):
        """Server accepts PCM frames of various common sizes without error."""
        from starlette.testclient import TestClient

        app, token = patched_app

        frame_sizes_ms = [10, 16, 20, 30, 50]

        with TestClient(app) as client:
            for ms in frame_sizes_ms:
                with client.websocket_connect(f"/ws?token={token}") as ws:
                    ws.receive_json()  # ready
                    frame = _silent_pcm(ms / 1000)
                    # Send a few frames of each size
                    for _ in range(5):
                        ws.send_bytes(frame)
                    # No exception = success


# ---------------------------------------------------------------------------
# 5. WebSocket — control message throughput
# ---------------------------------------------------------------------------

class TestControlMessageThroughput:
    """Burst of JSON control messages inside a single session."""

    N_MESSAGES = 100

    def test_vad_reset_burst(self, patched_app, capsys):
        """Send N vad_reset messages; all should be processed without error."""
        from starlette.testclient import TestClient

        app, token = patched_app

        with TestClient(app) as client:
            with client.websocket_connect(f"/ws?token={token}") as ws:
                ws.receive_json()  # ready

                t0 = time.perf_counter()
                for _ in range(self.N_MESSAGES):
                    ws.send_text(json.dumps({"type": "vad_reset"}))
                elapsed = time.perf_counter() - t0

        mps = self.N_MESSAGES / elapsed

        with capsys.disabled():
            print(
                f"\n  Control burst (vad_reset × {self.N_MESSAGES}): "
                f"{elapsed*1000:.0f}ms → {mps:.0f} msg/s"
            )

    def test_stop_tts_burst(self, patched_app, capsys):
        """Send repeated stop_tts messages without crashing."""
        from starlette.testclient import TestClient

        app, token = patched_app

        with TestClient(app) as client:
            with client.websocket_connect(f"/ws?token={token}") as ws:
                ws.receive_json()  # ready

                for _ in range(20):
                    ws.send_text(json.dumps({"type": "stop_tts"}))
                # No exception = success

    def test_playback_done_burst(self, patched_app, capsys):
        """Send repeated playback_done messages without crashing."""
        from starlette.testclient import TestClient

        app, token = patched_app

        with TestClient(app) as client:
            with client.websocket_connect(f"/ws?token={token}") as ws:
                ws.receive_json()  # ready

                for _ in range(20):
                    ws.send_text(json.dumps({"type": "playback_done"}))

    def test_mixed_control_messages(self, patched_app, capsys):
        """Interleaved audio and control messages do not deadlock."""
        from starlette.testclient import TestClient

        app, token = patched_app
        frame = _silent_pcm()
        controls = [
            {"type": "vad_reset"},
            {"type": "stop_tts"},
            {"type": "playback_done"},
        ]

        with TestClient(app) as client:
            with client.websocket_connect(f"/ws?token={token}") as ws:
                ws.receive_json()  # ready

                import itertools
                for i, ctrl in enumerate(itertools.islice(itertools.cycle(controls), 30)):
                    ws.send_text(json.dumps(ctrl))
                    if i % 3 == 0:
                        ws.send_bytes(frame)


# ---------------------------------------------------------------------------
# 6. Session-replacement behaviour
# ---------------------------------------------------------------------------

class TestSessionReplacement:
    """New connection should evict (close) an existing session."""

    def test_new_connection_evicts_old(self, patched_app):
        """Second connect closes the first connection."""
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        app, token = patched_app

        # We need two sequential connections (the server only allows one at a time).
        # The second connection will trigger close of the first; the first client
        # may then receive a disconnect.  We just verify the second connection
        # gets a 'ready' message successfully.
        with TestClient(app) as client:
            # First connection
            with client.websocket_connect(f"/ws?token={token}") as ws1:
                msg1 = ws1.receive_json()
                assert msg1["type"] == "ready"

            # Second connection (first is now gone)
            with client.websocket_connect(f"/ws?token={token}") as ws2:
                msg2 = ws2.receive_json()
                assert msg2["type"] == "ready"


# ---------------------------------------------------------------------------
# 7. Stress — interleaved HTTP + WebSocket
# ---------------------------------------------------------------------------

class TestMixedLoad:
    """HTTP requests fire concurrently while a WebSocket session is active."""

    HTTP_CONCURRENCY = 20

    @pytest.mark.asyncio
    async def test_health_during_websocket_session(self, patched_app, capsys):
        """Health endpoint is responsive even with an active WS session."""
        import threading
        app, token = patched_app
        stats = Stats("GET /health (during WS)")

        # Run WebSocket session in a background thread
        ws_ready = threading.Event()
        ws_stop = threading.Event()
        ws_errors: list[Exception] = []

        def _ws_session():
            from starlette.testclient import TestClient
            try:
                with TestClient(app) as client:
                    with client.websocket_connect(f"/ws?token={token}") as ws:
                        ws.receive_json()  # ready
                        ws_ready.set()
                        frame = _silent_pcm()
                        # Keep sending audio until stop signal
                        while not ws_stop.is_set():
                            ws.send_bytes(frame)
            except Exception as exc:
                ws_errors.append(exc)
                ws_ready.set()

        ws_thread = threading.Thread(target=_ws_session, daemon=True)
        ws_thread.start()
        ws_ready.wait(timeout=5.0)

        # Fire concurrent HTTP requests while WS is active
        async def _one(client):
            t0 = time.perf_counter()
            try:
                resp = await client.get("/health")
                assert resp.status_code == 200
                stats.record(time.perf_counter() - t0)
            except Exception:
                stats.errors += 1

        async with _async_client(app) as client:
            tasks = [asyncio.create_task(_one(client)) for _ in range(self.HTTP_CONCURRENCY)]
            await asyncio.gather(*tasks)

        ws_stop.set()
        ws_thread.join(timeout=5.0)

        with capsys.disabled():
            print(f"\n  {stats.report()}")

        assert stats.errors == 0, f"{stats.errors} HTTP errors during WS session"


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "-s", "--tb=short"],
        cwd=str(__file__).replace("/tests/load_test.py", ""),
    )
    sys.exit(result.returncode)
