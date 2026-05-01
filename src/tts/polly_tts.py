"""Amazon Polly TTS engine.

Requires boto3:
    pip install boto3

Credentials are supplied explicitly via AWS access key ID + secret access key.
Alternatively, if both are left blank, boto3 will fall back to the standard
AWS credential chain (environment variables, ~/.aws/credentials, IAM role, etc.).

Neural engine voices (recommended):
  Joanna, Matthew, Salli, Joey, Ivy, Kendra, Kimberly, Kevin,
  Amy, Emma, Brian, Olivia, Aria, Ayanda ...

Standard engine voices support more languages but sound less natural.

Run ``fetch_voices()`` or use the Refresh Voices button in the main window
to see all voices available in your region.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────── availability ──

def is_available() -> bool:
    """Return True if boto3 is installed."""
    try:
        import boto3  # noqa: F401
        return True
    except ImportError:
        return False


def has_credentials(access_key_id: str, secret_access_key: str, region: str) -> bool:
    """Return True if enough config is present to attempt a Polly call."""
    # Region is always required; keys may be empty if using the AWS credential chain.
    return bool(region)


# ──────────────────────────────────────────────── client ──

def _make_client(access_key_id: str, secret_access_key: str, region: str):
    import boto3
    kwargs: dict = {"region_name": region}
    if access_key_id:
        kwargs["aws_access_key_id"] = access_key_id
    if secret_access_key:
        kwargs["aws_secret_access_key"] = secret_access_key
    return boto3.client("polly", **kwargs)


# ──────────────────────────────────────────────── voice list ──

def fetch_voices(
    access_key_id: str,
    secret_access_key: str,
    region: str,
    engine: str = "neural",
) -> list[dict]:
    """Return a sorted list of ``{voice_id, name, gender, language_name}`` dicts."""
    client = _make_client(access_key_id, secret_access_key, region)
    voices: list[dict] = []
    resp = client.describe_voices(Engine=engine)
    for v in resp.get("Voices", []):
        voices.append({
            "voice_id": v["Id"],
            "name": v["Name"],
            "gender": v.get("Gender", ""),
            "language_name": v.get("LanguageName", ""),
            "language_code": v.get("LanguageCode", ""),
        })
    voices.sort(key=lambda v: (v["language_code"], v["name"]))
    return voices


# ──────────────────────────────────────────────── synthesis ──

def speak_text(
    text: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    voice_id: str,
    engine: str,
    device_indices: List[Optional[int | str]],
    volume: float = 0.8,
) -> None:
    """Synthesize *text* via Amazon Polly and play through each device.

    Runs in a daemon thread — returns immediately.
    ``None`` entries in *device_indices* are skipped.
    """
    active = [d for d in device_indices if d is not None]
    if not active or not text.strip() or not region or not voice_id:
        return
    thread = threading.Thread(
        target=_speak_worker,
        args=(text, access_key_id, secret_access_key, region, voice_id, engine, active, volume),
        daemon=True,
        name="PollyTTS",
    )
    thread.start()


def _speak_worker(
    text: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    voice_id: str,
    engine: str,
    device_indices: List[int | str],
    volume: float,
) -> None:
    tmp_path: Optional[str] = None
    try:
        import sounddevice as sd
        import soundfile as sf
    except ImportError as exc:
        logger.warning("Polly TTS: sounddevice/soundfile unavailable: %s", exc)
        return

    try:
        client = _make_client(access_key_id, secret_access_key, region)
        resp = client.synthesize_speech(
            Text=text,
            OutputFormat="mp3",
            VoiceId=voice_id,
            Engine=engine,
        )
        audio_bytes = resp["AudioStream"].read()

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(audio_bytes)

        data, samplerate = sf.read(tmp_path, dtype="float32")
        if data.size == 0:
            logger.warning("Polly returned empty audio for: %r", text)
            return
        if data.ndim == 1:
            data = data.reshape(-1, 1)

        if volume != 1.0:
            data = data * max(0.0, min(2.0, volume))

        # Pre-query devices and prepare per-device audio
        playback_tasks = []
        pulse_tasks: list = []
        for device_selector in device_indices:
            try:
                dev_info = sd.query_devices(device_selector)
                out_ch = int(dev_info["max_output_channels"])
                if out_ch < 1:
                    logger.warning("Polly: device %s has no output channels, skipping.", device_selector)
                    continue
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
                            "Polly: PortAudio cannot reach %r — queuing paplay fallback.",
                            device_selector,
                        )
                        pulse_tasks.append((data, samplerate, device_selector))
                        continue
                logger.warning("Polly device prep failed for device %s: %s", device_selector, exc)

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

    except Exception as exc:
        logger.exception("Amazon Polly TTS error: %s", exc)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _play_on_device(data, samplerate: int, device_selector: int | str) -> None:
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
        logger.warning("Polly playback failed on device %s: %s", device_selector, exc)
