"""SteamVR Input — polls three boolean actions via OpenVR's action system.

Actions:
  /actions/main/in/push_to_talk  — forwards to the existing PTT system
  /actions/main/in/stop_tts      — immediately stops TTS audio
  /actions/main/in/repeat_tts    — re-sends the last transcription

Thread model:
  A single daemon thread handles initialization retries AND the poll loop.
  If SteamVR is not running, initialization is retried every RETRY_INTERVAL_S
  seconds without blocking the GUI.  If SteamVR shuts down while running,
  openvr.shutdown() is called and the thread re-enters the retry loop.

  All action callbacks are called from the background thread.  The caller
  (MainWindow) is responsible for marshalling them to the GUI thread via
  pyqtSignal — the same pattern used by PTTHandler.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

POLL_HZ = 30
POLL_INTERVAL = 1.0 / POLL_HZ
RETRY_INTERVAL_S = 5.0

ACTION_SET    = "/actions/main"
ACT_PTT       = "/actions/main/in/push_to_talk"
ACT_STOP_TTS  = "/actions/main/in/stop_tts"
ACT_REPEAT    = "/actions/main/in/repeat_tts"


# ──────────────────────────────────────────────────── one-time registration ──

def register_manifest(vrmanifest_path: str, action_manifest_path: str) -> None:
    """Patch the vrmanifest with the absolute action_manifest_path, then register
    with SteamVR via vrpathreg.

    SteamVR's binding editor requires an absolute action_manifest_path; relative
    paths cause "failed to load manifest" errors.  We rewrite the field in place
    on every launch so the path stays correct even if the app is moved.

    Safe to call on every launch — vrpathreg is idempotent.
    Silently skipped if vrpathreg cannot be found.
    """
    _patch_action_manifest(action_manifest_path)
    import json as _json
    mf = Path(vrmanifest_path)
    mf_dir = mf.parent

    # When frozen by PyInstaller 6+, the exe is at dist/RawriisSTT/RawriisSTT.exe
    # and _internal/ is a sibling of the exe.  sys.executable gives us the correct dir.
    # In dev mode the exe_dir is the steamvr/ folder (sibling of assets/).
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
    else:
        exe_dir = mf_dir.parent

    try:
        data = _json.loads(mf.read_text(encoding="utf-8"))
        for app in data.get("applications", []):
            app["action_manifest_path"] = action_manifest_path
            # Copy icon to the exe directory and point image_path there.
            # SteamVR has trouble loading icons from inside _internal/; placing
            # the icon next to the exe is more reliable.
            img = app.get("image_path", "")
            if img:
                src_icon = mf_dir / img if not Path(img).is_absolute() else Path(img)
                dst_icon = exe_dir / src_icon.name
                if src_icon.exists() and src_icon != dst_icon:
                    import shutil as _shutil
                    try:
                        _shutil.copy2(str(src_icon), str(dst_icon))
                        logger.info("Copied icon to exe dir: %s", dst_icon)
                    except Exception as _ie:
                        logger.warning("Could not copy icon: %s", _ie)
                final_icon = dst_icon if dst_icon.exists() else src_icon
                app["image_path"] = str(final_icon)
                logger.info("SteamVR image_path set to: %s (exists=%s)", final_icon, final_icon.exists())
            # Make binary paths absolute so SteamVR can launch the app
            for key, name in (("binary_path_windows", "RawriisSTT.exe"),
                               ("binary_path_linux",   "RawriisSTT")):
                if key in app and not Path(app[key]).is_absolute():
                    app[key] = str(exe_dir / name)
            logger.info("SteamVR exe dir resolved to: %s", exe_dir)
        mf.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Patched vrmanifest written")
    except Exception as exc:
        logger.warning("Could not patch vrmanifest: %s", exc)

    vrpathreg = _find_vrpathreg()
    if not vrpathreg:
        logger.debug("vrpathreg not found — skipping manifest registration")
        return
    try:
        # Remove first to flush SteamVR's cached manifest, then re-add
        subprocess.run([vrpathreg, "removemanifest", vrmanifest_path],
                       capture_output=True, timeout=10)
        result = subprocess.run(
            [vrpathreg, "addmanifest", vrmanifest_path],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            logger.info("SteamVR manifest registered: %s", vrmanifest_path)
        else:
            logger.warning(
                "vrpathreg addmanifest returned %d: %s",
                result.returncode,
                result.stderr.decode(errors="replace").strip(),
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("vrpathreg failed: %s", exc)


def _patch_action_manifest(action_manifest_path: str) -> None:
    """Rewrite binding_url entries in actions.json to absolute paths.

    SteamVR resolves relative binding_url paths against the process working
    directory, not the manifest file location.  When running as a PyInstaller
    exe the manifest lives in _internal/steamvr/ but the CWD is the exe dir,
    so relative paths silently fail to load, leaving all actions with no
    bindings and bActive=False forever.
    """
    import json as _json
    mf = Path(action_manifest_path)
    mf_dir = mf.parent
    try:
        data = _json.loads(mf.read_text(encoding="utf-8"))
        changed = False
        for entry in data.get("default_bindings", []):
            url = entry.get("binding_url", "")
            if url and not Path(url).is_absolute():
                abs_url = str(mf_dir / url)
                entry["binding_url"] = abs_url
                changed = True
        if changed:
            mf.write_text(_json.dumps(data, indent=2), encoding="utf-8")
            logger.info("Patched action manifest binding_url paths to absolute")
    except Exception as exc:
        logger.warning("Could not patch action manifest: %s", exc)


def _find_vrpathreg() -> Optional[str]:
    """Return the path to the vrpathreg binary, or None if not found."""
    # 1. STEAMVR_RUNTIME environment variable
    runtime_env = os.environ.get("STEAMVR_RUNTIME")
    if runtime_env:
        candidate = Path(runtime_env) / "bin" / ("vrpathreg.exe" if sys.platform == "win32" else "vrpathreg")
        if candidate.exists():
            return str(candidate)

    # 2. Common Windows install locations
    if sys.platform == "win32":
        steam_paths = [
            Path(r"C:\Program Files (x86)\Steam\steamapps\common\SteamVR\bin\win64\vrpathreg.exe"),
            Path(r"C:\Program Files\Steam\steamapps\common\SteamVR\bin\win64\vrpathreg.exe"),
        ]
        # Also check PROGRAMFILES env
        pf = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        steam_paths.append(Path(pf) / "Steam" / "steamapps" / "common" / "SteamVR" / "bin" / "win64" / "vrpathreg.exe")
        for p in steam_paths:
            if p.exists():
                return str(p)

    # 3. Linux: check ~/.steam/steam/steamapps/common/SteamVR
    if sys.platform == "linux":
        linux_candidates = [
            Path.home() / ".steam" / "steam" / "steamapps" / "common" / "SteamVR" / "bin" / "vrpathreg.sh",
            Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common" / "SteamVR" / "bin" / "vrpathreg.sh",
        ]
        for p in linux_candidates:
            if p.exists():
                return str(p)

    # 4. Fall back to PATH
    import shutil
    return shutil.which("vrpathreg")


def _is_steamvr_running() -> bool:
    """Return True if the SteamVR server process is currently running."""
    try:
        if sys.platform == "win32":
            import ctypes
            # Use EnumProcesses via psapi to avoid spawning any console window
            TH32CS_SNAPPROCESS = 0x00000002
            import ctypes.wintypes as wt
            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize",              wt.DWORD),
                    ("cntUsage",            wt.DWORD),
                    ("th32ProcessID",       wt.DWORD),
                    ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID",        wt.DWORD),
                    ("cntThreads",          wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase",      ctypes.c_long),
                    ("dwFlags",             wt.DWORD),
                    ("szExeFile",           ctypes.c_char * 260),
                ]
            snap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap == ctypes.c_void_p(-1).value:
                return False
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            found = False
            if ctypes.windll.kernel32.Process32First(snap, ctypes.byref(entry)):
                while True:
                    if entry.szExeFile.lower() == b"vrserver.exe":
                        found = True
                        break
                    if not ctypes.windll.kernel32.Process32Next(snap, ctypes.byref(entry)):
                        break
            ctypes.windll.kernel32.CloseHandle(snap)
            return found
        else:
            import subprocess as _sp
            result = _sp.run(["pgrep", "-x", "vrserver"], capture_output=True, timeout=3)
            return result.returncode == 0
    except Exception:
        return False


# ──────────────────────────────────────────────── SteamVRInputManager class ──

class SteamVRInputManager:
    """Polls SteamVR boolean actions and forwards transitions to callbacks.

    Designed to be long-lived: start() once, stop() on shutdown.
    Handles SteamVR not being available at startup or disappearing at runtime.
    """

    def __init__(
        self,
        action_manifest_path: str,
        vrmanifest_path: str,
        on_ptt_press: Callable[[], None],
        on_ptt_release: Callable[[], None],
        on_stop_tts: Callable[[], None],
        on_repeat_tts: Callable[[], None],
        ptt_mode: str,
    ) -> None:
        self._action_manifest = action_manifest_path
        self._vrmanifest_path = vrmanifest_path
        self._on_ptt_press    = on_ptt_press
        self._on_ptt_release  = on_ptt_release
        self._on_stop_tts     = on_stop_tts
        self._on_repeat_tts   = on_repeat_tts
        self._ptt_mode        = ptt_mode      # "ptt_hold" | "ptt_toggle"
        self._ptt_active      = False         # toggle-mode state
        self._stop_event      = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock            = threading.Lock()
        self._import_warned   = False         # log ImportError only once

    # ------------------------------------------------------------------ public

    def start(self) -> None:
        """Start the background polling thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._thread_fn, daemon=True, name="steamvr-input")
        self._thread.start()
        logger.info("SteamVRInputManager started")

    def stop(self) -> None:
        """Signal the thread to exit and wait for it."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        logger.info("SteamVRInputManager stopped")

    def set_ptt_mode(self, mode: str) -> None:
        """Update the PTT mode at runtime (thread-safe)."""
        with self._lock:
            self._ptt_mode = mode
            self._ptt_active = False

    # ----------------------------------------------------------------- private

    def _thread_fn(self) -> None:
        while not self._stop_event.is_set():
            openvr = self._try_init()
            if openvr is None:
                # SteamVR unavailable — wait and retry
                self._stop_event.wait(RETRY_INTERVAL_S)
                continue

            logger.info("SteamVR connected — beginning input polling")
            try:
                self._poll_loop(openvr)
            except Exception as exc:
                logger.warning("SteamVR poll loop exited with error: %s", exc)
            finally:
                try:
                    openvr.shutdown()
                except Exception:
                    pass
                logger.info("SteamVR disconnected — will retry in %ds", RETRY_INTERVAL_S)

    def _try_init(self):
        """Attempt to initialise OpenVR and load action handles.

        Returns the openvr module on success, or None on failure.
        """
        try:
            import openvr
        except (ImportError, OSError) as exc:
            if not self._import_warned:
                logger.warning("openvr unavailable (%s: %s) — will keep retrying", type(exc).__name__, exc)
                self._import_warned = True
            return None

        if not _is_steamvr_running():
            return None

        try:
            openvr.init(openvr.VRApplication_Background)
        except openvr.OpenVRError as exc:
            logger.debug("OpenVR init failed: %s %r", exc, exc)
            return None

        # Register our manifest directly in the running SteamVR session.
        # This overrides any stale registration that vrpathreg may have left
        # from a previous install location.
        try:
            openvr.VRApplications().addApplicationManifest(self._vrmanifest_path, False)
            logger.info("App manifest registered in session: %s", self._vrmanifest_path)
        except Exception as exc:
            logger.warning("addApplicationManifest failed: %s", exc)

        # Identify this process as our registered app key so SteamVR applies
        # the correct saved bindings. Required for VRApplication_Background apps
        # that don't have a window for SteamVR to match against automatically.
        try:
            import os as _os
            openvr.VRApplications().identifyApplication(_os.getpid(), "com.rawrii.rawriisstt")
            logger.info("Identified process %d as com.rawrii.rawriisstt", _os.getpid())
        except Exception as exc:
            logger.warning("identifyApplication failed: %s", exc)

        try:
            vrinput = openvr.VRInput()
            err = vrinput.setActionManifestPath(self._action_manifest)
            if err:
                logger.warning("setActionManifestPath error %s — aborting (path=%s)", err, self._action_manifest)
                openvr.shutdown()
                return None
            logger.info("setActionManifestPath OK: %s", self._action_manifest)

            self._action_set_handle = vrinput.getActionSetHandle(ACTION_SET)
            self._h_ptt    = vrinput.getActionHandle(ACT_PTT)
            self._h_stop   = vrinput.getActionHandle(ACT_STOP_TTS)
            self._h_repeat = vrinput.getActionHandle(ACT_REPEAT)

            logger.info("Action handles: ptt=%s stop=%s repeat=%s set=%s",
                        self._h_ptt, self._h_stop, self._h_repeat, self._action_set_handle)
            return openvr
        except Exception as exc:
            logger.warning("Failed to load action handles: %s", exc)
            try:
                openvr.shutdown()
            except Exception:
                pass
            return None

    def _poll_loop(self, openvr) -> None:
        vrinput   = openvr.VRInput()
        vrsystem  = openvr.VRSystem()
        event     = openvr.VREvent_t()

        # Build a ctypes array of VRActiveActionSet_t — the API requires a pointer
        # to a C array, not a Python list.  Passing a plain list causes the struct
        # size to be computed incorrectly, which silently keeps bActive=False.
        ActiveSets = openvr.VRActiveActionSet_t * 1
        active_sets = ActiveSets()
        active_sets[0].ulActionSet = self._action_set_handle
        active_sets[0].ulRestrictedToDevice = openvr.k_ulInvalidInputValueHandle
        active_sets[0].nPriority = 0

        # Per-action previous-state trackers
        _prev: dict[int, bool] = {
            self._h_ptt:    False,
            self._h_stop:   False,
            self._h_repeat: False,
        }

        _last_active = None
        while not self._stop_event.is_set():
            # Check for VREvent_Quit
            while vrsystem.pollNextEvent(event):
                if event.eventType == openvr.VREvent_Quit:
                    logger.info("Received VREvent_Quit")
                    return

            try:
                vrinput.updateActionState(active_sets)
            except openvr.OpenVRError as exc:
                logger.warning("updateActionState failed: %s", exc)
                return

            try:
                ptt_data = vrinput.getDigitalActionData(
                    self._h_ptt, openvr.k_ulInvalidInputValueHandle
                )
                active_now = bool(ptt_data.bActive)
                if active_now != _last_active:
                    logger.info("SteamVR PTT bActive changed: %s (bState=%s)",
                                active_now, bool(ptt_data.bState))
                    _last_active = active_now
            except Exception:
                pass

            for handle, action_name, fire_fn in (
                (self._h_ptt,    "push_to_talk", self._handle_ptt),
                (self._h_stop,   "stop_tts",     self._handle_stop_tts),
                (self._h_repeat, "repeat_tts",   self._handle_repeat_tts),
            ):
                try:
                    data = vrinput.getDigitalActionData(
                        handle, openvr.k_ulInvalidInputValueHandle
                    )
                except openvr.OpenVRError:
                    continue

                state = bool(data.bState)
                changed = bool(data.bChanged)

                if changed:
                    if state:
                        logger.info("SteamVR: %s pressed", action_name)
                    fire_fn(state)
                else:
                    prev = _prev[handle]
                    if state != prev:
                        if state:
                            logger.info("SteamVR: %s pressed (edge detect)", action_name)
                        fire_fn(state)
                _prev[handle] = state

            time.sleep(POLL_INTERVAL)

    def _handle_ptt(self, state: bool) -> None:
        with self._lock:
            mode = self._ptt_mode

        if mode == "ptt_hold":
            if state:
                self._on_ptt_press()
            else:
                self._on_ptt_release()
        elif mode == "ptt_toggle":
            if state:  # button-down only
                if self._ptt_active:
                    self._on_ptt_release()
                else:
                    self._on_ptt_press()
                self._ptt_active = not self._ptt_active

    def _handle_stop_tts(self, state: bool) -> None:
        if state:  # button-down only
            self._on_stop_tts()

    def _handle_repeat_tts(self, state: bool) -> None:
        if state:  # button-down only
            self._on_repeat_tts()
