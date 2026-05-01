"""eSpeak-NG TTS — Moonbase Alpha style robotic/synthetic speech.

eSpeak-NG must be installed separately:
  Windows : https://github.com/espeak-ng/espeak-ng/releases
            (typical path: C:\\Program Files\\eSpeak NG\\espeak-ng.exe)
  Linux   : sudo apt install espeak-ng      (Debian/Ubuntu)
             sudo pacman -S espeak-ng        (Arch)
             sudo dnf install espeak-ng      (Fedora)

Voice examples:
  en          — English (default, classic Moonbase Alpha tone)
  en-us       — American English
  en+m1 … +m7 — Male variants (different robots!)
  en+f1 … +f4 — Female variants
  en+croak    — Extra gravelly male
  en+whisper  — Whisper voice
Run ``espeak-ng --voices`` to list all available voices.
"""
from __future__ import annotations

import io
import logging
import subprocess
import sys
import threading
import wave
from pathlib import Path
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────── binary detection ──

def _espeak_cmd() -> str:
    """Return the espeak-ng executable path for the current platform."""
    if sys.platform == "win32":
        candidates = [
            Path(r"C:\Program Files\eSpeak NG\espeak-ng.exe"),
            Path(r"C:\Program Files (x86)\eSpeak NG\espeak-ng.exe"),
        ]
        for path in candidates:
            if path.exists():
                return str(path)
    return "espeak-ng"   # must be in PATH on Linux / fallback on Windows


def is_available() -> bool:
    """Return True if espeak-ng is found and executable."""
    try:
        result = subprocess.run(
            [_espeak_cmd(), "--version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


# ──────────────────────────────────────────────── synthesis ──

def generate_audio(
    text: str,
    voice: str = "en",
    speed: int = 175,
    pitch: int = 50,
    amplitude: int = 100,
) -> tuple[np.ndarray, int]:
    """Synthesize *text* via espeak-ng and return ``(float32_audio, sample_rate)``.

    The audio array is always 2-D (frames × 1) so it can be fed directly to
    sounddevice which may up-mix channels as needed.
    """
    cmd = [
        _espeak_cmd(),
        "--stdout",
        "-v", voice,
        "-s", str(speed),
        "-p", str(pitch),
        "-a", str(amplitude),
        text,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
    except FileNotFoundError:
        raise RuntimeError(
            "espeak-ng not found.\n\n"
            "Install it from https://github.com/espeak-ng/espeak-ng/releases\n"
            "(Windows) or via your package manager (Linux)."
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"espeak-ng exited with code {result.returncode}.\n"
            f"{result.stderr.decode(errors='replace')}"
        )

    wav_bytes = result.stdout
    try:
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            n_frames = wf.getnframes()
            n_channels = wf.getnchannels()
            rate = wf.getframerate()
            raw = wf.readframes(n_frames)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse eSpeak WAV output: {exc}") from exc

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        audio = audio.reshape(-1, n_channels)
    else:
        audio = audio.reshape(-1, 1)
    return audio, rate


# ──────────────────────────────────────────────── public API ──

def speak_text(
    text: str,
    device_indices: List[Optional[int]],
    volume: float = 1.0,
    voice: str = "en",
    speed: int = 175,
    pitch: int = 50,
) -> None:
    """Synthesize *text* and play it on every device in *device_indices*.

    Runs in a background daemon thread — returns immediately.
    ``None`` entries in *device_indices* are skipped.
    """
    active = [d for d in device_indices if d is not None]
    if not active or not text.strip():
        return
    thread = threading.Thread(
        target=_speak_worker,
        args=(text, active, volume, voice, speed, pitch),
        daemon=True,
        name="eSpeakTTS",
    )
    thread.start()


def _speak_worker(
    text: str,
    device_indices: List[int | str],
    volume: float,
    voice: str,
    speed: int,
    pitch: int,
) -> None:
    try:
        import sounddevice as sd

        audio, rate = generate_audio(text, voice=voice, speed=speed, pitch=pitch)

        if volume != 1.0:
            audio = audio * max(0.0, min(2.0, volume))

        # Pre-match channels for each device
        playback_tasks = []
        pulse_tasks: list = []
        for device_selector in device_indices:
            try:
                dev_info = sd.query_devices(device_selector)
                out_channels = int(dev_info["max_output_channels"])
                if out_channels < 1:
                    logger.warning("eSpeak: device %s has no output channels, skipping.", device_selector)
                    continue
                if audio.shape[1] == 1 and out_channels >= 2:
                    play_data = audio.repeat(2, axis=1)
                elif audio.shape[1] > out_channels:
                    play_data = audio[:, :out_channels]
                else:
                    play_data = audio
                playback_tasks.append((play_data, rate, device_selector))
            except Exception as exc:
                if isinstance(device_selector, str):
                    from ..audio.pulse_play import is_supported as _pp_ok
                    if _pp_ok():
                        logger.info(
                            "eSpeak: PortAudio cannot reach %r — queuing paplay fallback.",
                            device_selector,
                        )
                        pulse_tasks.append((audio, rate, device_selector))
                        continue
                logger.warning("eSpeak TTS device prep failed for device %s: %s", device_selector, exc)

        sd_threads = [
            threading.Thread(target=_play_on_device, args=task, daemon=True)
            for task in playback_tasks
        ]
        pp_threads: list = []
        if pulse_tasks:
            from ..audio.pulse_play import play_to_sink_async as _pp_async
            for pp_task in pulse_tasks:
                pp_threads.append(_pp_async(*pp_task))
        for t in sd_threads:
            t.start()
        for t in sd_threads + pp_threads:
            t.join()

    except Exception as exc:
        logger.exception("eSpeak TTS error: %s", exc)


def _play_on_device(data: np.ndarray, samplerate: int, device_selector: int | str) -> None:
    import sounddevice as sd

    done = threading.Event()
    idx = [0]

    def _cb(outdata, frames, _time, _status):
        remaining = len(data) - idx[0]
        if remaining == 0:
            outdata[:] = 0
            raise sd.CallbackStop
        chunk = min(frames, remaining)
        outdata[:chunk] = data[idx[0]: idx[0] + chunk]
        if chunk < frames:
            outdata[chunk:] = 0
        idx[0] += chunk

    try:
        with sd.OutputStream(
            samplerate=samplerate,
            channels=data.shape[1],
            dtype="float32",
            device=device_selector,
            callback=_cb,
            finished_callback=done.set,
        ):
            done.wait(timeout=len(data) / samplerate + 5.0)
    except Exception as exc:
        logger.warning("eSpeak playback failed on device %s: %s", device_selector, exc)
