"""ElevenLabs TTS engine.

API reference:
  Voices  : GET  https://api.elevenlabs.io/v1/voices
  Models  : GET  https://api.elevenlabs.io/v1/models
  Synthesize: POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}

The API key is passed in the ``xi-api-key`` header and is NEVER logged.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_BASE = "https://api.elevenlabs.io/v1"


# ──────────────────────────────────────────────── Cache helpers ──

def _cache_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", Path.home())
    else:
        base = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    return Path(base) / "RawriisSTT" / "elevenlabs_cache.json"


def save_cache(voices: list[dict], models: list[dict]) -> None:
    """Persist voices + models lists to disk."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"voices": voices, "models": models}, f, indent=2)


def load_cache() -> tuple[list[dict], list[dict]]:
    """Return (voices, models) from disk cache, or ([], []) if unavailable."""
    path = _cache_path()
    if not path.exists():
        return [], []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("voices", []), data.get("models", [])
    except Exception:
        return [], []


# ──────────────────────────────────────────────── API calls ──

def _get_json(url: str, api_key: str) -> object:
    req = urllib.request.Request(
        url,
        headers={"xi-api-key": api_key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def fetch_voices(api_key: str) -> list[dict]:
    """Return [{voice_id, name, settings}] for the authenticated user's voice library."""
    data = _get_json(f"{_BASE}/voices", api_key)
    result = []
    for v in data.get("voices", []):
        entry: dict = {"voice_id": v["voice_id"], "name": v["name"]}
        raw_settings = v.get("settings") or v.get("voice_settings") or {}
        entry["settings"] = {
            "stability": float(raw_settings.get("stability", 0.5)),
            "similarity_boost": float(raw_settings.get("similarity_boost", 0.75)),
            "style": float(raw_settings.get("style", 0.0)),
            "use_speaker_boost": bool(raw_settings.get("use_speaker_boost", True)),
        }
        result.append(entry)
    return result


def fetch_models(api_key: str) -> list[dict]:
    """Return [{model_id, name}] for TTS-capable ElevenLabs models."""
    data = _get_json(f"{_BASE}/models", api_key)
    return [
        {"model_id": m["model_id"], "name": m["name"]}
        for m in data
        if m.get("can_do_text_to_speech", True)
    ]


# ──────────────────────────────────────────────── TTS synthesis ──

def speak_text(
    text: str,
    api_key: str,
    voice_id: str,
    model_id: str,
    device_indices: List[Optional[int | str]],
    volume: float = 0.8,
    stability: float = 0.5,
    similarity_boost: float = 0.75,
    style: float = 0.0,
    use_speaker_boost: bool = True,
) -> None:
    """Synthesize *text* via ElevenLabs and play through each device.

    Runs in a daemon thread — returns immediately.
    """
    active = [d for d in device_indices if d is not None]
    if not active or not text.strip() or not api_key or not voice_id:
        return
    voice_settings = {
        "stability": stability,
        "similarity_boost": similarity_boost,
        "style": style,
        "use_speaker_boost": use_speaker_boost,
    }
    thread = threading.Thread(
        target=_speak_worker,
        args=(text, api_key, voice_id, model_id or "eleven_monolingual_v1", active, volume, voice_settings),
        daemon=True,
        name="ElevenLabsTTS",
    )
    thread.start()


def _speak_worker(
    text: str,
    api_key: str,
    voice_id: str,
    model_id: str,
    device_indices: List[int],
    volume: float,
    voice_settings: Optional[dict] = None,
) -> None:
    tmp_path: Optional[str] = None
    try:
        import sounddevice as sd
        import soundfile as sf
    except ImportError as exc:
        logger.warning("ElevenLabs TTS: sounddevice/soundfile unavailable: %s", exc)
        return

    try:
        payload: dict = {"text": text, "model_id": model_id}
        if voice_settings:
            payload["voice_settings"] = voice_settings
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{_BASE}/text-to-speech/{voice_id}",
            data=body,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio_bytes = resp.read()
        logger.debug("ElevenLabs: received %d bytes of audio.", len(audio_bytes))

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(audio_bytes)

        data, samplerate = sf.read(tmp_path, dtype="float32")
        if data.size == 0:
            logger.warning("ElevenLabs returned empty audio.")
            return
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        logger.debug("ElevenLabs: decoded audio shape=%s samplerate=%d", data.shape, samplerate)

        # Pre-query devices and prepare per-device audio
        playback_tasks = []
        pulse_tasks: list = []
        for device_selector in device_indices:
            try:
                dev_info = sd.query_devices(device_selector)
                out_ch = int(dev_info["max_output_channels"])
                if out_ch < 1:
                    logger.warning("ElevenLabs: device %s has no output channels — skipping.", device_selector)
                    continue
                logger.debug("ElevenLabs: preparing device %s (%r) out_ch=%d", device_selector, dev_info["name"], out_ch)
                if data.shape[1] == 1 and out_ch >= 2:
                    play_data = data.repeat(2, axis=1)
                elif data.shape[1] > out_ch:
                    play_data = data[:, :out_ch]
                else:
                    play_data = data
                playback_tasks.append((play_data, samplerate, device_selector))
            except Exception as exc:
                if isinstance(device_selector, str):
                    from ..audio.pulse_play import is_supported as _pp_ok
                    if _pp_ok():
                        logger.info(
                            "ElevenLabs: PortAudio cannot reach %r — queuing paplay fallback.",
                            device_selector,
                        )
                        pulse_tasks.append((data, samplerate, device_selector))
                        continue
                logger.warning("ElevenLabs device prep failed for device %s: %s", device_selector, exc)

        # Play to all devices concurrently
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

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        logger.error("ElevenLabs HTTP %s: %s — %s", exc.code, exc.reason, body)
    except Exception as exc:
        logger.exception("ElevenLabs TTS error: %s", exc)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _play_on_device(data, samplerate: int, device_selector: int | str) -> None:
    """Play *data* on a single output device, blocking until playback completes."""
    import sounddevice as sd

    done = threading.Event()
    idx = [0]

    def _cb(outdata, frames, _time, _status):
        remaining = len(data) - idx[0]
        if remaining == 0:
            outdata[:] = 0
            raise sd.CallbackStop
        chunk = min(frames, remaining)
        outdata[:chunk] = data[idx[0] : idx[0] + chunk]
        if chunk < frames:
            outdata[chunk:] = 0
        idx[0] += chunk

    try:
        logger.debug("ElevenLabs: opening output stream on device %s", device_selector)
        with sd.OutputStream(
            samplerate=samplerate,
            channels=data.shape[1],
            dtype="float32",
            device=device_selector,
            callback=_cb,
            finished_callback=done.set,
        ):
            done.wait(timeout=len(data) / samplerate + 5.0)
        logger.debug("ElevenLabs: playback complete on device %s", device_selector)
    except Exception as exc:
        logger.warning("ElevenLabs playback failed on device %s: %s", device_selector, exc)
