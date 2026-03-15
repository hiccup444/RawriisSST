from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

import numpy as np

from .base import ResultCallback, STTEngine, STTResult
from .whisper_models import get_model_path

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000          # Whisper expects 16 kHz mono
BLOCK_DURATION_MS = 30       # VAD frame size (10 / 20 / 30 ms)
BLOCK_SIZE = int(SAMPLE_RATE * BLOCK_DURATION_MS / 1000)
PTT_SPEECH_MARGIN_FRAMES = 5 # Frames kept before first / after last speech frame (~150 ms)
MIN_SPEECH_FRAMES = 3        # Minimum VAD-positive frames required to attempt transcription (~90 ms)


def _find_python() -> str:
    """Return an absolute path to a real Python interpreter.

    sys.executable may point to a PyInstaller bundle or launcher.
    Search order:
      1. pyvenv.cfg home (set by venv, points at the base Python directory)
      2. sys.base_exec_prefix  (base Python install, not the venv)
      3. sys.exec_prefix       (venv dir — contains Scripts/python.exe on Windows)
      4. PATH — but skip Windows Store stubs (WindowsApps\\python*.exe)
      5. sys.executable as last resort
    """
    prefixes = [sys.base_exec_prefix, sys.exec_prefix]

    # 1. Read pyvenv.cfg to get the original Python home directory
    for prefix in prefixes:
        cfg = os.path.join(prefix, "pyvenv.cfg")
        if os.path.isfile(cfg):
            try:
                with open(cfg) as f:
                    for line in f:
                        if line.lower().startswith("home"):
                            home = line.split("=", 1)[1].strip()
                            for name in ("python.exe", "python3.exe", "python"):
                                p = os.path.join(home, name)
                                if os.path.isfile(p):
                                    return p
            except Exception:
                pass

    # 2. Check well-known locations under base/exec prefix
    candidates = []
    for pfx in prefixes:
        candidates += [
            os.path.join(pfx, "python.exe"),              # conda / base on Windows
            os.path.join(pfx, "Scripts", "python.exe"),   # venv Scripts on Windows
            os.path.join(pfx, "bin", "python3"),           # Linux / macOS
            os.path.join(pfx, "bin", "python"),
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    # 3. PATH search — skip the Windows Store app-installer stubs
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found and "WindowsApps" not in found:
            return found

    return sys.executable   # last resort


# Inline worker code passed via -c to avoid any file-path / launcher issues.
# When run as  python -c <_WORKER_CODE> <model_path> <device>
# sys.argv is   ["-c", model_path, device]
_WORKER_CODE = """\
import sys, json
import numpy as np
from faster_whisper import WhisperModel

mp, dev = sys.argv[1], sys.argv[2]

def _out(obj):
    sys.stdout.buffer.write(json.dumps(obj).encode() + b"\\n")
    sys.stdout.buffer.flush()

try:
    compute_type = "float16" if dev == "cuda" else "int8"
    model = WhisperModel(mp, device=dev, compute_type=compute_type)
    _out({"status": "loaded"})
except Exception as exc:
    _out({"status": "error", "message": str(exc)})
    raise SystemExit(1)

buf = sys.stdin.buffer
while True:
    line = buf.readline()
    if not line:
        break
    try:
        hdr = json.loads(line.strip())
    except Exception:
        continue
    t = hdr.get("type")
    if t == "transcribe":
        audio = np.frombuffer(buf.read(int(hdr["size"])), dtype="float32")
        try:
            segs, _ = model.transcribe(
                audio,
                language=hdr.get("language") if hdr.get("language") not in (None, "", "auto") else None,
                beam_size=5,
                vad_filter=False,
                suppress_tokens=[-1],
            )
            text = " ".join(s.text.strip() for s in segs).strip()
            _out({"type": "result", "text": text})
        except Exception:
            _out({"type": "result", "text": ""})
    elif t == "quit":
        break
"""


class WhisperSTT(STTEngine):
    """Speech-to-text using faster-whisper (local, offline).

    The ctranslate2 model runs in a child process launched via subprocess.Popen
    so that a C-level crash cannot take down the main application.

    Supports three input modes:
      - "vad":        Continuous VAD-gated transcription.
      - "ptt_hold":   Buffer audio while PTT key held; transcribe on release.
      - "ptt_toggle": First PTT press starts recording; second press transcribes.
    """

    def __init__(
        self,
        model_size: str = "base",
        device: str = "cpu",
        vad_enabled: bool = True,
        vad_aggressiveness: int = 2,
        silence_threshold_ms: int = 700,
        input_mode: str = "vad",
        max_record_seconds: int = 10,
        live_transcribe: bool = False,
    ) -> None:
        super().__init__()
        self.model_size = model_size
        self.device = device
        self.vad_enabled = vad_enabled
        self.vad_aggressiveness = vad_aggressiveness
        self.silence_threshold_ms = silence_threshold_ms
        self.input_mode = input_mode
        self.max_record_seconds = max_record_seconds
        self.live_transcribe = live_transcribe

        self._is_loaded: bool = False
        self._proc: Optional[subprocess.Popen] = None
        self._stdout_queue: queue.Queue = queue.Queue()
        self._stdout_thread: Optional[threading.Thread] = None

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._audio_queue: queue.Queue[Optional[np.ndarray]] = queue.Queue(maxsize=100)

        # PTT state flags — written by PTT handler thread, read by capture thread
        self._ptt_record_active: bool = False
        self._ptt_flush_requested: bool = False

        # VAD instance — created once at load_model time, reused across captures
        self._vad = None

    @property
    def name(self) -> str:
        return f"Whisper ({self.model_size})"

    @property
    def requires_model_download(self) -> bool:
        return True

    @property
    def is_model_loaded(self) -> bool:
        return self._is_loaded

    # ------------------------------------------------------------------ Model management

    def load_model(self) -> None:
        """Launch the worker subprocess and wait for the model to load.
        Blocking — call from a background thread.
        """
        if self._is_loaded:
            return
        model_path = get_model_path(self.model_size)
        if model_path is None:
            raise RuntimeError(
                f"Whisper model '{self.model_size}' is not downloaded.\n"
                "Go to Settings → Speech-to-Text and click Download next to the model."
            )
        logger.info(
            "Loading Whisper model '%s' on %s from %s…",
            self.model_size, self.device, model_path,
        )

        kwargs: dict = {}
        if sys.platform == "win32":
            # Prevent Python.exe from opening a visible CMD console window
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        python_exe = _find_python()
        logger.info("Whisper subprocess python: %s", python_exe)
        self._proc = subprocess.Popen(
            [python_exe, "-c", _WORKER_CODE, model_path, self.device],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )

        # Background thread drains stdout so readline never blocks indefinitely
        self._stdout_queue = queue.Queue()
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout,
            args=(self._proc.stdout, self._stdout_queue),
            daemon=True,
            name="WhisperStdout",
        )
        self._stdout_thread.start()

        # Wait up to 120 s for the "loaded" / "error" status line
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                stderr_out = self._proc.stderr.read().decode(errors="replace").strip()
                code = self._proc.returncode
                self._proc = None
                raise RuntimeError(
                    f"Whisper subprocess crashed during model loading (exit code {code}).\n"
                    f"{stderr_out}\n"
                    "This is usually caused by a missing DLL or incompatible hardware.\n"
                    "Try reinstalling: pip install --upgrade faster-whisper"
                )
            try:
                line = self._stdout_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if line is None:   # EOF with no status line
                code = self._proc.poll() if self._proc else "?"
                stderr_out = self._proc.stderr.read().decode(errors="replace").strip() if self._proc else ""
                self._proc = None
                raise RuntimeError(
                    f"Whisper subprocess exited unexpectedly (exit code {code}).\n{stderr_out}"
                )

            try:
                msg = json.loads(line)
            except Exception:
                continue

            if msg.get("status") == "loaded":
                self._is_loaded = True
                logger.info("Whisper model loaded (subprocess pid=%d).", self._proc.pid)
                if self.vad_enabled:
                    try:
                        import webrtcvad
                        self._vad = webrtcvad.Vad(self.vad_aggressiveness)
                    except ImportError:
                        logger.warning("webrtcvad not available — VAD disabled")
                return
            elif msg.get("status") == "error":
                err = msg.get("message", "unknown error")
                self._proc.terminate()
                self._proc = None
                raise RuntimeError(f"Whisper model failed to load:\n{err}")
        else:
            if self._proc:
                self._proc.terminate()
                self._proc = None
            raise RuntimeError("Whisper model loading timed out (>120 s).")

    def unload_model(self) -> None:
        """Terminate the worker subprocess and release resources."""
        self._is_loaded = False
        if self._proc is not None:
            try:
                self._proc.stdin.write(
                    json.dumps({"type": "quit"}).encode() + b"\n"
                )
                self._proc.stdin.flush()
            except Exception:
                pass
            self._proc.terminate()
            self._proc.wait(timeout=5)
            self._proc = None
        logger.info("Whisper model unloaded.")

    # ------------------------------------------------------------------ PTT controls

    def ptt_press(self) -> None:
        """Called by PTTHandler when the PTT key is pressed."""
        if self.input_mode == "ptt_hold":
            self._drain_audio_queue()
            self._ptt_record_active = True
            logger.debug("PTT hold: recording started.")
        elif self.input_mode == "ptt_toggle":
            if not self._ptt_record_active:
                self._drain_audio_queue()
                self._ptt_record_active = True
                logger.debug("PTT toggle: recording started.")
            else:
                self._ptt_record_active = False
                self._ptt_flush_requested = True
                logger.debug("PTT toggle: recording stopped, flush requested.")

    def ptt_release(self) -> None:
        """Called by PTTHandler when the PTT key is released (hold) or toggled off (toggle)."""
        if self._ptt_record_active:
            self._ptt_record_active = False
            self._ptt_flush_requested = True
            logger.debug("PTT release: flush requested.")

    # ------------------------------------------------------------------ Listening

    def start_listening(
        self,
        callback: ResultCallback,
        device_index: Optional[int] = None,
        language: str = "en",
    ) -> None:
        if self._listening:
            return
        if not self._is_loaded:
            raise RuntimeError(
                "Whisper model is not loaded.\n"
                "Click 'Launch Whisper' to load the model first."
            )
        self._callback = callback
        self._stop_event.clear()
        self._ptt_record_active = False
        self._ptt_flush_requested = False
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(device_index, language),
            daemon=True,
            name="WhisperSTT",
        )
        self._listening = True
        self._thread.start()
        # Block the calling QThread so Qt signal delivery stays reliable.
        self._thread.join()

    def stop_listening(self) -> None:
        self._stop_event.set()
        self._audio_queue.put(None)  # unblock the queue consumer
        if self._thread:
            self._thread.join(timeout=5)
        self._listening = False
        self._ptt_record_active = False
        self._ptt_flush_requested = False

    # ------------------------------------------------------------------ Capture

    def _capture_loop(self, device_index: Optional[int], language: str) -> None:
        try:
            self._run_capture(device_index, language)
        except Exception as exc:
            logger.exception("WhisperSTT capture error: %s", exc)
        finally:
            self._listening = False

    def _run_capture(self, device_index: Optional[int], language: str) -> None:
        import sounddevice as sd

        vad = self._vad  # created once at load_model time

        def audio_callback(indata: np.ndarray, frames: int, time_info, status) -> None:
            if status:
                logger.debug("sounddevice status: %s", status)
            try:
                self._audio_queue.put_nowait(indata.copy())
            except queue.Full:
                pass  # drop frame; transcription is falling behind

        kwargs = dict(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=BLOCK_SIZE,
            callback=audio_callback,
        )
        if device_index is not None:
            kwargs["device"] = device_index

        with sd.InputStream(**kwargs):
            if self.input_mode == "vad":
                self._loop_vad(vad, language)
            elif self.live_transcribe:
                self._loop_ptt_live(vad, language)
            else:
                self._loop_ptt_standard(vad, language)

    def _loop_vad(self, vad, language: str) -> None:
        """Continuous VAD-gated transcription loop."""
        silence_frames_needed = max(1, self.silence_threshold_ms // BLOCK_DURATION_MS)
        max_frames = int(self.max_record_seconds * 1000 / BLOCK_DURATION_MS)
        audio_buffer: list[np.ndarray] = []
        silent_frames = 0
        speech_started = False
        speech_frame_count = 0

        while not self._stop_event.is_set():
            try:
                chunk = self._audio_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if chunk is None:
                break

            is_speech = self._vad_check(vad, chunk.tobytes())

            if is_speech:
                speech_started = True
                silent_frames = 0
                speech_frame_count += 1
                audio_buffer.append(chunk)
            elif speech_started:
                silent_frames += 1
                audio_buffer.append(chunk)
                if silent_frames >= silence_frames_needed or len(audio_buffer) >= max_frames:
                    if speech_frame_count >= MIN_SPEECH_FRAMES:
                        self._transcribe(audio_buffer, language)
                    audio_buffer = []
                    silent_frames = 0
                    speech_started = False
                    speech_frame_count = 0

    def _loop_ptt_standard(self, vad, language: str) -> None:
        """PTT-gated transcription loop (hold and toggle modes)."""
        max_frames = int(self.max_record_seconds * 1000 / BLOCK_DURATION_MS)
        audio_buffer: list[np.ndarray] = []
        first_speech: Optional[int] = None
        last_speech: Optional[int] = None
        speech_frame_count = 0

        def _flush() -> None:
            nonlocal audio_buffer, first_speech, last_speech, speech_frame_count
            if first_speech is not None and speech_frame_count >= MIN_SPEECH_FRAMES:
                start = max(0, first_speech - PTT_SPEECH_MARGIN_FRAMES)
                end = min(len(audio_buffer), last_speech + PTT_SPEECH_MARGIN_FRAMES + 1)
                self._transcribe(audio_buffer[start:end], language)
            audio_buffer = []
            first_speech = None
            last_speech = None
            speech_frame_count = 0

        while not self._stop_event.is_set():
            try:
                chunk = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                if self._ptt_flush_requested and not self._ptt_record_active:
                    self._ptt_flush_requested = False
                    _flush()
                continue

            if chunk is None:
                break

            if self._ptt_record_active:
                idx = len(audio_buffer)
                audio_buffer.append(chunk)
                if self._vad_check(vad, chunk.tobytes()):
                    if first_speech is None:
                        first_speech = idx
                    last_speech = idx
                    speech_frame_count += 1
                if len(audio_buffer) >= max_frames:
                    self._ptt_record_active = False
                    self._ptt_flush_requested = False
                    _flush()

            elif self._ptt_flush_requested:
                self._ptt_flush_requested = False
                _flush()

    def _loop_ptt_live(self, vad, language: str) -> None:
        """PTT live-transcribe loop."""
        silence_frames_needed = max(1, self.silence_threshold_ms // BLOCK_DURATION_MS)
        max_frames = int(self.max_record_seconds * 1000 / BLOCK_DURATION_MS)

        audio_buffer: list[np.ndarray] = []
        silent_frames = 0
        speech_started = False
        first_speech: Optional[int] = None
        last_speech: Optional[int] = None
        speech_frame_count = 0

        def _flush_segment() -> None:
            nonlocal audio_buffer, silent_frames, speech_started, first_speech, last_speech, speech_frame_count
            if first_speech is not None and speech_frame_count >= MIN_SPEECH_FRAMES:
                start = max(0, first_speech - PTT_SPEECH_MARGIN_FRAMES)
                end = min(len(audio_buffer), last_speech + PTT_SPEECH_MARGIN_FRAMES + 1)
                self._transcribe(audio_buffer[start:end], language)
            audio_buffer = []
            silent_frames = 0
            speech_started = False
            first_speech = None
            last_speech = None
            speech_frame_count = 0

        while not self._stop_event.is_set():
            try:
                chunk = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                if self._ptt_flush_requested and not self._ptt_record_active:
                    self._ptt_flush_requested = False
                    _flush_segment()
                continue

            if chunk is None:
                break

            if self._ptt_record_active:
                idx = len(audio_buffer)
                audio_buffer.append(chunk)
                is_speech = self._vad_check(vad, chunk.tobytes())

                if is_speech:
                    if first_speech is None:
                        first_speech = idx
                    last_speech = idx
                    speech_started = True
                    silent_frames = 0
                    speech_frame_count += 1
                elif speech_started:
                    silent_frames += 1
                    if silent_frames >= silence_frames_needed:
                        _flush_segment()

                if len(audio_buffer) >= max_frames:
                    _flush_segment()

            elif self._ptt_flush_requested:
                self._ptt_flush_requested = False
                _flush_segment()

    # ------------------------------------------------------------------ Helpers

    @staticmethod
    def _drain_stdout(pipe, out_queue: queue.Queue) -> None:
        """Background thread: reads stdout lines and puts them on a queue."""
        try:
            for line in pipe:
                out_queue.put(line)
        finally:
            out_queue.put(None)   # signal EOF

    def _drain_audio_queue(self) -> None:
        """Discard all audio frames queued before PTT press."""
        while True:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break

    def _vad_check(self, vad, pcm_bytes: bytes) -> bool:
        if vad is None:
            return True
        try:
            return vad.is_speech(pcm_bytes, SAMPLE_RATE)
        except Exception:
            return True

    def _transcribe(self, frames: list[np.ndarray], language: str) -> None:
        if not frames or self._proc is None:
            return
        audio = np.concatenate(frames, axis=0).flatten().astype(np.float32) / 32768.0
        audio_bytes = audio.tobytes()
        header = json.dumps({"type": "transcribe", "language": language, "size": len(audio_bytes)})
        try:
            self._proc.stdin.write(header.encode() + b"\n")
            self._proc.stdin.write(audio_bytes)
            self._proc.stdin.flush()
        except Exception as exc:
            logger.warning("Failed to send audio to Whisper subprocess: %s", exc)
            return

        # Poll for result, with stop-event and subprocess-death bail-outs
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return
            if self._proc.poll() is not None:
                logger.error(
                    "Whisper subprocess died during transcription (exit code %s)",
                    self._proc.returncode,
                )
                self._is_loaded = False
                return
            try:
                line = self._stdout_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            if line is None:
                logger.error("Whisper subprocess closed stdout unexpectedly.")
                self._is_loaded = False
                return
            try:
                msg = json.loads(line)
                if msg.get("type") != "result":
                    continue
                text = msg.get("text", "")
                if text and self._callback:
                    self._callback(STTResult(text=text, is_final=True))
            except Exception as exc:
                logger.warning("Failed to parse Whisper result: %s", exc)
                continue
            return

        logger.warning("Transcription timed out (>30 s).")
