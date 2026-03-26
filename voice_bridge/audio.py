"""audio.py — Silero VAD ONNX wrapper and state machine.

Provides the VAD primitives needed by the voice bridge.
No microphone capture, no wakeword — bridge-only subset.

Public API
----------
load_vad_model() -> SileroVAD
    Download (first run) and load the Silero VAD ONNX model.

VadStateMachine
    State machine: WAITING → SPEAKING → TRAILING_SILENCE → DONE.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path
from typing import Literal

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16_000          # Hz — required by Silero VAD 16kHz model
CHUNK_SAMPLES = 512           # samples per VAD frame at 16 kHz (32 ms)
CONTEXT_SAMPLES = 64          # context prepended to each chunk per Silero spec

_MODELS_DIR = Path(__file__).parent / "models"
_VAD_MODEL_PATH = _MODELS_DIR / "silero_vad.onnx"
_VAD_MODEL_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/master"
    "/src/silero_vad/data/silero_vad.onnx"
)

_SPEECH_THRESHOLD = 0.5


def _log(msg: str) -> None:
    print(f"[voice-bridge audio] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Silero VAD ONNX wrapper
# ---------------------------------------------------------------------------

class SileroVAD:
    """Thin ONNX-based wrapper around the Silero VAD model."""

    def __init__(self, model_path: Path) -> None:
        import importlib
        from unittest.mock import Mock

        ort = importlib.import_module("onnxruntime")
        if isinstance(ort, Mock):
            import sys as _sys
            _sys.modules.pop("onnxruntime", None)
            ort = importlib.import_module("onnxruntime")

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3

        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def __call__(self, chunk: np.ndarray) -> float:
        if chunk.dtype != np.float32:
            chunk = chunk.astype(np.float32)
        if chunk.ndim != 1 or chunk.shape[0] != CHUNK_SAMPLES:
            raise ValueError(
                f"chunk must be 1-D with {CHUNK_SAMPLES} samples, got {chunk.shape}"
            )

        x = np.concatenate([self._context, chunk[np.newaxis, :]], axis=1)
        feed = {
            "input": x,
            "state": self._state,
            "sr": np.array(SAMPLE_RATE, dtype=np.int64),
        }
        output, new_state = self._session.run(None, feed)
        self._state = new_state
        self._context = x[:, -CONTEXT_SAMPLES:]
        return float(output[0, 0])


# ---------------------------------------------------------------------------
# Model loader
# ---------------------------------------------------------------------------

def load_vad_model() -> SileroVAD:
    """Download (once) and return a ready-to-use SileroVAD instance."""
    if not _VAD_MODEL_PATH.exists():
        _log(f"Downloading Silero VAD model → {_VAD_MODEL_PATH} …")
        _MODELS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            urllib.request.urlretrieve(_VAD_MODEL_URL, _VAD_MODEL_PATH)
            _log("Download complete.")
        except Exception as exc:
            _log(f"ERROR: failed to download VAD model: {exc}")
            raise

    return SileroVAD(_VAD_MODEL_PATH)


# ---------------------------------------------------------------------------
# VAD State Machine
# ---------------------------------------------------------------------------

State = Literal["WAITING", "SPEAKING", "TRAILING_SILENCE", "DONE"]


class VadStateMachine:
    """Drive audio-capture state based on per-chunk VAD probabilities.

    Transitions: WAITING → SPEAKING → TRAILING_SILENCE → DONE
    """

    def __init__(
        self,
        silence_duration: float = 0.5,
        min_speech_duration: float = 0.5,
        no_speech_timeout: float = 15.0,
        sample_rate: int = SAMPLE_RATE,
        chunk_size: int = CHUNK_SAMPLES,
        speech_threshold: float = _SPEECH_THRESHOLD,
    ) -> None:
        self.silence_duration = silence_duration
        self.min_speech_duration = min_speech_duration
        self.no_speech_timeout = no_speech_timeout
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.speech_threshold = speech_threshold

        self.state: State = "WAITING"
        self.timed_out: bool = False

        self._speech_started_at: float | None = None
        self._silence_started_at: float | None = None
        self._accumulated_speech: float = 0.0

    def update(self, speech_prob: float, timestamp: float) -> bool:
        """Process one VAD result. Returns True when recording should stop."""
        is_speech = speech_prob >= self.speech_threshold

        if self.state == "WAITING":
            if timestamp >= self.no_speech_timeout:
                self.state = "DONE"
                self.timed_out = True
                return True
            if is_speech:
                self.state = "SPEAKING"
                self._speech_started_at = timestamp
                self._accumulated_speech += self.chunk_size / self.sample_rate

        elif self.state == "SPEAKING":
            if is_speech:
                self._accumulated_speech += self.chunk_size / self.sample_rate
            else:
                self.state = "TRAILING_SILENCE"
                self._silence_started_at = timestamp

        elif self.state == "TRAILING_SILENCE":
            if is_speech:
                self.state = "SPEAKING"
                self._accumulated_speech += self.chunk_size / self.sample_rate
                self._silence_started_at = None
            else:
                silence_elapsed = timestamp - self._silence_started_at  # type: ignore[operator]
                if (
                    silence_elapsed >= self.silence_duration
                    and self._accumulated_speech >= self.min_speech_duration
                ):
                    self.state = "DONE"
                    return True

        return self.state == "DONE"
