# STT & TTS Engine Setup

---

## Speech-to-Text Engines

### Whisper (Recommended)

Local transcription powered by OpenAI's Whisper model via [faster-whisper](https://github.com/SYSTRAN/faster-whisper). No internet or API key required.

**Setup:**
1. Open **Settings → Speech-to-Text**.
2. Under **Whisper Models**, choose a model size and click **Download**.
3. Set **Whisper Device** to `cpu` (default) or `cuda` if you have an NVIDIA GPU.
4. Close Settings.
5. On the main window, select **Whisper** as the engine.
6. Click **Launch Whisper** - wait for the model to finish loading.
7. Click **Start Recording**.

**Model sizes:**

| Model | Size | Notes |
|---|---|---|
| `tiny` | ~75 MB | Fastest, lowest accuracy |
| `base` | ~145 MB | Good starting point |
| `small` | ~465 MB | Better accuracy |
| `medium` | ~1.5 GB | High accuracy |
| `large-v3` | ~3 GB | Best accuracy, slowest on CPU |

Larger models require more VRAM/RAM. `base` or `small` are recommended for most users.

**Language:**
Set a specific language on the main window to improve accuracy. Leave it on **Auto** to let Whisper detect the language automatically.

---

### Azure Speech

Cloud-based transcription from Microsoft. Fast and highly accurate. Requires an Azure subscription.

**Setup:**
1. Create an [Azure account](https://azure.microsoft.com/free/) and a **Speech** resource.
2. Copy your **API Key** and **Region** (e.g. `eastus`) from the Azure portal.
3. Open **Settings → Speech-to-Text** and paste them into the Azure fields.
4. Select **Azure** as the engine on the main window.
5. Click **Start Recording**.

No model download required - transcription happens in the cloud.

---

### Vosk

Fully offline, lightweight speech recognition. Smaller models than Whisper, lower accuracy, but very fast even on old hardware.

**Setup:**
1. Open **Settings → Speech-to-Text**.
2. Under **Vosk Models**, find a model for your language and click **Download**.
3. Select **Vosk** as the engine on the main window.
4. Click **Start Recording**.

---

### System STT

Uses the `SpeechRecognition` library, which sends audio to the **Google Web Speech API** by default. Requires an internet connection.

No setup required - select **System STT** on the main window and click **Start Recording**.

---

## Text-to-Speech Engines

TTS reads incoming messages aloud through your audio output device. Enable it on the main window and select an engine in **Settings → Text-to-Speech**.

### System TTS

Uses `pyttsx3` (Windows SAPI / Linux eSpeak). No setup, no internet, no API key.

Select **System TTS** in Settings → Text-to-Speech. Voice and speed can be adjusted from the TTS settings.

---

### ElevenLabs

High-quality AI voices streamed from the ElevenLabs API. Requires an ElevenLabs account (free tier available).

**Setup:**
1. Sign up at [elevenlabs.io](https://elevenlabs.io) and copy your **API Key** from your profile.
2. Open **Settings → Text-to-Speech** and paste it into the **ElevenLabs API Key** field.
3. Select **ElevenLabs** as the TTS engine.
4. Click **Refresh** next to the Voice dropdown to load your voice library, then select a voice.

For creating custom or cloned voices and tuning voice parameters, see [Custom ElevenLabs Voices](voices.md).

---

### Amazon Polly

Neural text-to-speech via AWS. Requires an AWS account.

**Setup:**
1. Create an [AWS account](https://aws.amazon.com) and an IAM user with `AmazonPollyReadOnlyAccess` permissions.
2. Generate an **Access Key ID** and **Secret Access Key** for that user.
3. Open **Settings → Text-to-Speech** and fill in:
   - **Access Key ID**
   - **Secret Access Key**
   - **Region** (e.g. `us-east-1`)
4. Select **Amazon Polly** as the TTS engine.

---

## TTS Options

These apply to all TTS engines and are found in **Settings → Text-to-Speech**:

| Option | Description |
|---|---|
| **Delay before playback** | Adds a pause (ms) before TTS audio plays - useful to sync with VRChat chatbox display |
| **Stop on new message** | Cuts off current TTS when a new transcription arrives |
| **Queue messages** | Plays messages one after another instead of cutting off |
| **Smart split** | Splits long messages at word boundaries to respect VRChat's 144-character chatbox limit |
