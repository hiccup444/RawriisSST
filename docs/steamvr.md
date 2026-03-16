# SteamVR Controller Bindings

RawriisSTT integrates with the SteamVR input system, letting you bind push-to-talk (and other actions) to any controller button - no third-party software needed.

---

## Requirements

- SteamVR installed via Steam
- RawriisSTT running

A headset does not need to be connected - RawriisSTT connects to SteamVR in background mode.

---

## Binding Your Controller

1. Launch RawriisSTT.
2. Launch SteamVR (headset connection is optional).
3. Open **Settings → Hotkeys** in RawriisSTT.
4. Click **Open SteamVR Bindings** - this opens the SteamVR binding editor in your browser. (or open it manually through steam)
5. In the binding editor, click **Show Other Apps** at the bottom of the app list.
6. Find **RawriisSTT** and click on it.
7. Assign your preferred controller button to the **Push-to-Talk** action.
8. Click **Save Personal Binding**.

The binding is saved by SteamVR and will persist between sessions.

---

## Supported Controllers

Default bindings are included for the following controller types. SteamVR loads the correct binding automatically:

| Controller | Default PTT Button |
|---|---|
| Meta Quest (Touch) | Right A |
| Valve Index (Knuckles) | Right A |
| HTC Vive | Right trigger |
| Windows Mixed Reality | Right thumbstick click |
| HP Reverb G2 | Right thumbstick click |
| Generic / other | Right trigger |

**Stop TTS** and **Repeat TTS** have no default binding — assign them manually in the SteamVR binding editor if needed.

---

## Available Actions

| Action | Description |
|---|---|
| **Push-to-Talk** | Mode selected in-app between toggle and hold functionality. |
| **Stop TTS** | Immediately stops any playing TTS audio |
| **Repeat TTS** | Re-sends the last TTS message |

---

## How It Works

RawriisSTT runs as a **background application** in SteamVR - it does not require a VR overlay or headset. When SteamVR is running, the app automatically connects and begins polling for button inputs.

The connection is attempted every few seconds if SteamVR is not yet running. No restart is needed - launch SteamVR at any point and RawriisSTT will pick it up automatically.

---

## Troubleshooting

**RawriisSTT doesn't appear in the SteamVR binding editor**
- Make sure RawriisSTT is running and SteamVR is open.
- Check the app's log for `SteamVR manifest registered` - if it's missing, try restarting the app.
- Fully restart SteamVR (not just the headset) after the first launch to flush the manifest cache.

**Binding editor shows "failed to load manifest"**
- This usually means the app wasn't running when you opened the binding editor.
- Launch RawriisSTT first, then open the SteamVR binding editor.

**Controller input isn't triggering PTT**
- Confirm the binding was saved in the SteamVR editor.
- Make sure SteamVR is running - the app logs `SteamVR connected` when the link is established.

**App icon is blank in SteamVR**
- The icon is loaded from `assets/RawriisIcon.png` at startup. If it's missing, the icon will be blank. Re-download or re-extract the app.
