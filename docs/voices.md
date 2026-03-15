# Custom Voices with ElevenLabs

RawriisSTT supports ElevenLabs as a TTS engine, giving you access to their full voice library and instant voice cloning. This guide covers creating or cloning a voice on the ElevenLabs website and using it in the app.

---

## Prerequisites

- An ElevenLabs account (free tier works - see below for limits)
- RawriisSTT with ElevenLabs selected as your TTS engine

---

## Step 1 - Create an ElevenLabs Account

1. Go to [elevenlabs.io](https://elevenlabs.io) and sign up for a free account.
2. The free tier gives you a limited number of characters per month. Paid plans remove or raise this limit.

---

## Step 2 - Get Your API Key

1. Click your profile icon in the top-right corner of the ElevenLabs dashboard.
2. Go to **Profile + API key**.
3. Copy your **API Key**.

> **Permissions:** ElevenLabs lets you create restricted API keys that only allow certain actions. If you created a restricted key, make sure it has at least the following permissions enabled:
> - **Speech synthesis** - required to generate TTS audio
> - **Voices - Read** - required for the Refresh button to load your voice list
>
> The simplest option is to unrestrict your API key.

4. In RawriisSTT, open **Settings → Text-to-Speech**.
5. Paste the key into the **API Key** field and click **OK**.

---

## Step 3 - Choose or Create a Voice

You have two main options: use a voice from the ElevenLabs Voice Library, or clone your own.

---

### Option A - Use a Pre-Made Voice from the Library

ElevenLabs has a large public voice library with community-shared voices.

1. In the ElevenLabs dashboard, click **Voices** in the left sidebar.
2. Click **Voice Library** (or **Add Voice → Voice Library**).
3. Browse or search for a voice you like. You can preview them before adding.
4. Click **Add** on the voice you want - it will appear in your voice list.

---

### Option B - Clone Your Own Voice (Instant Voice Clone)

Instant Voice Clone lets you create a voice that sounds like you (or anyone you have permission to clone) from audio samples.

**Requirements:**
- At least 1 minute of clean audio (more is better - 5+ minutes recommended)
- Audio should be clear, with minimal background noise and no music
- WAV, MP3, M4A, FLAC, and OGG are all accepted

**Steps:**

1. In the ElevenLabs dashboard, go to **Voices**.
2. Click **Add Voice**.
3. Select **Instant Voice Clone**.
4. Give your voice a name.
5. Upload your audio samples. You can upload multiple files - ElevenLabs will combine them.
6. Add an optional description and labels (these help organize but don't affect output).
7. Check the consent box confirming you have rights to clone the voice.
8. Click **Add Voice**.

Your cloned voice will now appear in your voice list.

> **Tip:** For best clone quality, use samples where you speak naturally at a consistent volume. Recording in a quiet room with a decent microphone makes a noticeable difference. Reading aloud from a book or script works well.

---

## Step 4 - Connect the Voice to RawriisSTT

1. In the RawriisSTT main window, select **ElevenLabs** from the TTS engine dropdown.
2. Click the **Refresh** button next to the Voice dropdown. The app will fetch your voice list from ElevenLabs.
3. Select your voice from the **Voice** dropdown.
4. Select a **Model** from the model dropdown:
   - **Eleven Multilingual v2** - best quality, supports many languages
   - **Eleven Turbo v2.5** - faster, lower latency, good for real-time use
   - **Eleven English v1** - older English-only model, lowest latency
5. Your voice settings panel will appear below. Adjust as needed (see below).

---

## Step 5 - Tune the Voice Settings

When a voice is selected, four sliders and a toggle appear in the main window. These control how the voice sounds and are applied in real time.

| Setting | Range | What it Does |
|---|---|---|
| **Stability** | 0.0 - 1.0 | Lower = more expressive and varied; Higher = more consistent and flat. For TTS use, 0.5-0.7 is a good starting point. |
| **Similarity** | 0.0 - 1.0 | How closely the output matches the original voice sample. Too high can introduce artifacts on cloned voices. 0.7-0.85 usually works well. |
| **Style** | 0.0 - 1.0 | Style exaggeration. Only available on v2+ models. 0.0 is neutral - raise it to emphasize the voice's character. Can cause instability at high values. |
| **Speaker Boost** | On/Off | Adds extra clarity and presence. Usually worth leaving on. |

When you select a voice, RawriisSTT pre-fills these values from that voice's recommended defaults. You can adjust them and they will be remembered.

---

## Troubleshooting

**The Refresh button does nothing / voice list is empty**
- Make sure your API key is saved in **Settings → Text-to-Speech**.
- Check your internet connection.
- If you just added a new voice on the website, it may take a moment to appear - try refreshing again.

**Voice sounds robotic or has artifacts**
- Lower the Similarity slider slightly (try 0.65-0.75).
- Lower the Style slider to 0.0 if it's above 0.
- If using an Instant Voice Clone, try uploading more or cleaner audio samples.

**Output cuts off or sounds garbled**
- Try switching to a different model - Turbo models prioritize speed and can occasionally have quality tradeoffs on longer phrases.
- Enable **Smart Split** in Settings → Text-to-Speech so long messages are broken into smaller chunks.

**"No API key set" error when clicking Refresh**
- The API key must be saved first. Open **Settings → Text-to-Speech**, paste your key, and click **OK** before refreshing.
