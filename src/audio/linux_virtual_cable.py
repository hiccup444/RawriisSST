"""Helpers for creating a PipeWire/PulseAudio virtual sink on Linux.

The virtual sink (RawriisCable) acts as a virtual audio cable — TTS output is
routed to it, and VRChat (or any other app) can pick up the monitor source.
"""
from __future__ import annotations

CABLE_NAME = "RawriisCable"


def is_supported() -> bool:
    """True on Linux (excluding WSL) with pactl available and a reachable PulseAudio/PipeWire server.

    WSL is excluded because virtual sinks created inside WSL are not visible to
    Windows apps and the feature provides no useful routing there.
    """
    import sys
    import shutil
    import subprocess
    if sys.platform != "linux":
        return False
    try:
        with open("/proc/version") as _f:
            if "microsoft" in _f.read().lower():
                return False
    except OSError:
        pass
    if not shutil.which("pactl"):
        return False
    try:
        r = subprocess.run(["pactl", "info"], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def exists() -> bool:
    """True if RawriisCable sink or RawriisCable.monitor source exists."""
    import subprocess
    try:
        sinks = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True, text=True, timeout=5,
        )
        sources = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=5,
        )
        return (
            CABLE_NAME in sinks.stdout
            or f"{CABLE_NAME}.monitor" in sources.stdout
        )
    except Exception:
        return False


def create() -> int:
    """Create the RawriisCable virtual sink.

    Returns the pactl module ID on success.
    Raises RuntimeError if the cable already exists, if pactl fails,
    or if the sink cannot be verified after creation.
    """
    if exists():
        raise RuntimeError("Virtual cable already exists.")
    import subprocess
    r = subprocess.run(
        [
            "pactl", "load-module", "module-null-sink",
            f"sink_name={CABLE_NAME}",
            f"sink_properties=device.description={CABLE_NAME}",
        ],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "pactl load-module failed")
    if not exists():
        raise RuntimeError("Module loaded but RawriisCable did not appear — check PulseAudio/PipeWire logs.")
    try:
        return int(r.stdout.strip())
    except ValueError:
        return -1
