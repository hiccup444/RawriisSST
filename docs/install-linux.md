# Linux Setup

RawriisSTT runs from source on Linux. There is no pre-built Linux binary - use the launcher script instead.

---

## Requirements

- Python 3.10 or newer
- `pip`
- PortAudio (required by PyAudio)
- VRChat running via Steam/Proton with OSC enabled

---

## System Dependencies

Install PortAudio before running the launcher, otherwise PyAudio will fail to install:

**Debian / Ubuntu / Mint:**
```bash
sudo apt install portaudio19-dev python3-dev
```

**Arch / Manjaro:**
```bash
sudo pacman -S portaudio
```

**Fedora:**
```bash
sudo dnf install portaudio-devel
```

---

## Running the App

```bash
git clone https://github.com/hiccup444/RawriisSST.git
cd RawriisSTT
python3 launcher.py
```

`launcher.py` checks for missing packages and installs them automatically on first run. Subsequent launches skip straight to the app.

If you prefer to install manually:
```bash
pip install -r requirements.txt
python3 main.py
```

---

## Enable VRChat OSC

VRChat running under Proton does not auto-detect OSC on Linux. Enable it manually:

1. Open the VRChat radial menu.
2. Go to **Options → OSC → Enable**.

Alternatively, add `--enable-sdk-log-levels` to VRChat's launch options in Steam to confirm OSC traffic in the log.

---

## First-Time Setup (Whisper - Recommended)

1. Launch the app with `python3 launcher.py`.
2. Open **Settings → Speech-to-Text**.
3. Download a Whisper model (e.g. `base`) from the model list.
4. Close Settings.
5. On the main window:
   - Select your **microphone**.
   - Set your **language** (or leave on Auto).
   - Confirm **Whisper** is selected.
   - Make sure the **Chatbox** toggle is enabled so transcriptions are sent to VRChat.
6. Click **Launch Whisper**, then **Start Recording**.

---

## GPU Acceleration (Optional)

To run Whisper on an NVIDIA GPU:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

Then set **Whisper Device** to `cuda` in Settings → Speech-to-Text.

For AMD GPU support via ROCm, follow the [PyTorch ROCm install guide](https://pytorch.org/get-started/locally/).

---

## VAD & webrtcvad Notes

`webrtcvad` requires a C compiler to build on Linux. If the install fails:

```bash
sudo apt install build-essential  # Debian/Ubuntu
sudo pacman -S base-devel         # Arch
```

Then retry: `pip install webrtcvad`

---

## SteamVR Bindings on Linux

SteamVR runs natively on Linux via Steam. RawriisSTT will detect it automatically. Controller binding setup is the same as on Windows - see [SteamVR Bindings](steamvr.md).

---

## Troubleshooting

**`No module named 'PyAudio'` during install**
- Install PortAudio system package first (see System Dependencies above), then re-run `launcher.py`.

**Microphone not detected**
- Check PipeWire/PulseAudio is running: `pactl info`
- List available devices: `python3 -c "import sounddevice; print(sounddevice.query_devices())"`

**Nothing appears in VRChat chatbox**
- Confirm OSC is enabled inside VRChat.
- VRChat listens on `127.0.0.1:9000` by default - check Settings → General matches.
