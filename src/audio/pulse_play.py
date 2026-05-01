"""Direct PipeWire/PulseAudio sink routing via paplay.

Used as a fallback when PortAudio (sounddevice) cannot enumerate a virtual
sink that exists in PipeWire — e.g. a null sink created at runtime that
ALSA has not exposed to PortAudio yet.  Linux only; no-op on other platforms.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def is_supported() -> bool:
    """True on Linux with paplay available."""
    if sys.platform != "linux":
        return False
    import shutil
    return shutil.which("paplay") is not None


def play_to_sink(data: np.ndarray, samplerate: int, sink_name: str) -> None:
    """Write *data* (float32 numpy, frames×channels) as WAV and pipe it to
    the named PipeWire/PulseAudio sink via paplay.

    Blocks until playback is complete.  Logs a warning on any failure.
    """
    if sys.platform != "linux":
        return

    tmp_path: str | None = None
    try:
        import soundfile as sf
    except ImportError:
        logger.warning("pulse_play: soundfile not available — cannot use paplay fallback.")
        return

    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        sf.write(tmp_path, data, samplerate)

        duration = len(data) / max(samplerate, 1)
        logger.debug("pulse_play: routing to PipeWire sink %r via paplay (%.1fs)", sink_name, duration)

        result = subprocess.run(
            ["paplay", "--device", sink_name, tmp_path],
            capture_output=True,
            timeout=duration + 15,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            if "not found" in stderr.lower() or "no such" in stderr.lower():
                logger.warning(
                    "pulse_play: sink %r not found. "
                    "Ensure the virtual cable exists (Settings → Create Cable).",
                    sink_name,
                )
            else:
                logger.warning(
                    "pulse_play: paplay exited %d for sink %r: %s",
                    result.returncode, sink_name, stderr,
                )
        else:
            logger.debug("pulse_play: playback complete for sink %r", sink_name)

    except FileNotFoundError:
        logger.warning(
            "pulse_play: paplay not found. "
            "Install with: sudo apt install pulseaudio-utils"
        )
    except subprocess.TimeoutExpired:
        logger.warning("pulse_play: paplay timed out for sink %r", sink_name)
    except Exception as exc:
        logger.warning("pulse_play: unexpected error for sink %r: %s", sink_name, exc)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def play_to_sink_async(data: np.ndarray, samplerate: int, sink_name: str) -> threading.Thread:
    """Same as play_to_sink but runs in a daemon thread. Returns the thread."""
    t = threading.Thread(
        target=play_to_sink,
        args=(data, samplerate, sink_name),
        daemon=True,
        name=f"PulsePlay-{sink_name}",
    )
    t.start()
    return t
