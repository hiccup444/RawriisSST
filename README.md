# RawriisSTT

<p align="center">
  <img src="assets/RawriisIcon.png" width="128" alt="RawriisSTT icon">
</p>

<p align="center">
  Speech-to-Text for VRChat - local, private, and controller-friendly
</p>

<p align="center">
  <a href="https://github.com/hiccup444/RawriisSST/releases"><img alt="Downloads" src="https://img.shields.io/github/downloads/hiccup444/RawriisSST/total?label=Downloads"></a>
  <a href="https://github.com/hiccup444/RawriisSST/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/hiccup444/RawriisSST?label=Release"></a>
  <img alt="Platform" src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-blue">
  <a href="https://discord.gg/4bG2dEfRSJ"><img alt="Discord" src="https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white"></a>
</p>

---

RawriisSTT listens to your microphone and sends your speech to the VRChat chatbox over OSC — no subscription required. Designed for minimal performance impact, it uses efficient engines like faster-whisper for quick, local transcription. Choose from multiple local or cloud speech engines, bind push-to-talk to a keyboard key or SteamVR controller button, and optionally have messages read back through the text-to-speech voice of your choice.

---

## Documentation

| Guide | Description |
|---|---|
| [Windows Setup](docs/install-windows.md) | Download, install, and get started on Windows |
| [Linux Setup](docs/install-linux.md) | Running from source on Linux |
| [STT & TTS Engines](docs/engines.md) | Per-engine setup for Whisper, Azure, Vosk, ElevenLabs, Polly |
| [Custom ElevenLabs Voices](docs/voices.md) | Creating and cloning voices on ElevenLabs to use in the app |
| [SteamVR Bindings](docs/steamvr.md) | Binding controller buttons to push-to-talk |
| [Building from Source](docs/building.md) | Compiling your own exe |

---

## Features

🎙️ **Multiple STT engines**
- **Whisper** (local, CPU or CUDA) - no internet, no API key
- **Azure Speech** (cloud) - fast and accurate with an Azure subscription
- **Vosk** (offline) - lightweight, no internet required
- **System STT** - uses your OS speech recognition

🔊 **Text-to-Speech playback**
- **System TTS** (pyttsx3 / espeak) - zero setup
- **ElevenLabs** - high-quality AI voices with full parameter control
- **Amazon Polly** - neural voices via AWS
- Configurable delay before playback begins

💬 **VRChat chatbox integration**
- Sends transcriptions via OSC to `/chatbox/input`
- Broadcasts a listening state to your avatar parameters (`stt_listening`)

🎮 **SteamVR controller bindings**
- Bind push-to-talk to any controller button through the SteamVR input system
- Works without a headset connected (background app mode)

⌨️ **Hotkeys**
- Keyboard push-to-talk (hold or toggle mode)
- PTT push/release sound effects
- Global hotkeys to stop or re-send the last TTS message

🧠 **Voice Activity Detection**
- WebRTC VAD - automatic speech detection with configurable aggressiveness and silence timeout

📋 **TTS queue & smart split**
- Queue messages and play them sequentially
- Automatically split long messages at word boundaries (respects VRChat's 144-character chatbox limit)

💾 **Presets**
- Save and load full configuration snapshots to switch between setups instantly

📦 **In-app model downloads**
- Download Whisper and Vosk models directly from the UI - no manual file management

🎨 **Themed UI** built with PyQt6 - dark mode or pastel pink light mode, switchable live from Settings

---

## Quick Start (Windows)

1. Download the latest `RawriisSTT-vX.X.X.zip` from [Releases](https://github.com/hiccup444/RawriisSST/releases).
2. Extract and run `RawriisSTT.exe`.
3. Enable VRChat OSC: **Options → OSC → Enable**.
4. In **Settings → Speech-to-Text**, pick a Whisper model and download it.
5. Back on the main window, select your microphone and click **Launch Whisper**.
6. Click **Start Recording** - your speech will now appear in the VRChat chatbox.

See [Windows Setup](docs/install-windows.md) for a full walkthrough.

---

## Credits

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) - efficient Whisper inference via CTranslate2
- [pyopenvr](https://github.com/cmbruns/pyopenvr) - Python bindings for OpenVR / SteamVR
- [python-osc](https://github.com/attwad/python-osc) - OSC messaging
- [webrtcvad](https://github.com/wiseman/py-webrtcvad) - Voice Activity Detection
- [Vosk](https://alphacephei.com/vosk/) - offline speech recognition
- [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) - UI framework
