from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import List
from pathlib import Path


def _config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", Path.home())
    else:
        base = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    return Path(base) / "RawriisSTT"


CONFIG_PATH = _config_dir() / "settings.json"


@dataclass
class AppSettings:
    # OSC
    osc_address: str = "127.0.0.1"
    osc_port: int = 9000
    send_osc: bool = True
    use_chatbox: bool = True
    chatbox_play_notification: bool = False

    # Audio
    input_device: str = ""          # device name; empty = system default

    # STT engine selection
    stt_engine: str = "whisper"     # whisper | azure | vosk | system

    # Whisper
    whisper_model: str = "base"     # tiny | base | small | medium | large
    whisper_language: str = "en"
    whisper_device: str = "cpu"     # cpu | cuda
    whisper_device_index: int = 0   # CUDA device index (0 = first GPU)
    whisper_suppress_words: List[str] = field(default_factory=list)  # token-level word suppression

    # Azure
    azure_key: str = ""
    azure_region: str = ""

    # Vosk
    vosk_model_path: str = ""

    # VAD
    vad_enabled: bool = True
    silence_threshold_ms: int = 700
    vad_aggressiveness: int = 2     # 0–3 (webrtcvad levels)

    # UI
    always_on_top: bool = False
    dark_mode: bool = True

    # Whisper input mode
    whisper_input_mode: str = "vad"   # "vad" | "ptt_hold" | "ptt_toggle"
    ptt_key: str = "F9"
    ptt_live_transcribe: bool = False

    # Recording
    max_record_seconds: int = 10      # Hard cap per PTT press / VAD utterance (8–15)

    # Notification sounds
    ptt_sound_enabled: bool = True
    ptt_sound_volume: float = 0.8     # 0.0–1.0

    # TTS (Voice)
    tts_enabled: bool = False
    tts_headphones_device: str = ""   # output device name; empty = None
    tts_cable_device: str = ""        # output device name; empty = None
    tts_voice_engine: str = "pyttsx3" # pyttsx3 | elevenlabs | polly | espeak

    # eSpeak-NG TTS (Moonbase Alpha style)
    espeak_voice: str = "en"       # voice name, e.g. "en", "en-us", "en+m3", "en+f3"
    espeak_speed: int = 175        # words per minute (80–450)
    espeak_pitch: int = 50         # pitch 0–99

    # Amazon Polly TTS
    polly_access_key_id: str = ""
    polly_secret_access_key: str = ""
    polly_region: str = "us-east-1"
    polly_voice_id: str = "Joanna"
    polly_engine: str = "neural"    # standard | neural

    # ElevenLabs TTS
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model_id: str = ""
    elevenlabs_stability: float = 0.5
    elevenlabs_similarity_boost: float = 0.75
    elevenlabs_style: float = 0.0
    elevenlabs_use_speaker_boost: bool = True

    # TTS Playback behaviour
    tts_delay_before_audio_ms: int = 0       # delay between text output and audio start
    tts_stop_on_new: bool = False            # stop current playback when a new message arrives
    tts_queue_enabled: bool = True           # queue messages and play them sequentially
    tts_queue_delay_ms: int = 0             # gap between queued messages (ms)
    tts_smart_split: bool = False           # break long messages at word boundaries
    tts_smart_split_limit: int = 144        # character limit per chunk (VRChat chatbox = 144)

    # Hotkeys
    tts_quick_stop_key: str = ""   # global hotkey to stop current TTS playback
    tts_resend_key: str = ""       # global hotkey to re-send last transcription

    # Chatbox behaviour
    chatbox_show_keyboard: bool = False  # populate VRChat keyboard instead of sending directly


def load_settings() -> AppSettings:
    if not CONFIG_PATH.exists():
        return AppSettings()
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        defaults = asdict(AppSettings())
        defaults.update(data)
        return AppSettings(**{k: v for k, v in defaults.items() if k in AppSettings.__dataclass_fields__})
    except Exception:
        return AppSettings()


def save_settings(settings: AppSettings) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(asdict(settings), f, indent=2)
