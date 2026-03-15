# Building from Source

These instructions are for building the standalone executable yourself. End users should just download from the [Releases page](https://github.com/hiccup444/RawriisSST/releases).

---

## Requirements

- Python 3.10+
- Git

---

## Windows

```bat
build\build_windows.bat
```

This script:
1. Installs `pyinstaller`, `pillow`, and `openvr` via pip.
2. Auto-converts `assets/RawriisIcon.png` → `assets/RawriisIcon.ico` using Pillow.
3. Runs PyInstaller with `build/windows.spec`.
4. Outputs the build to `dist/RawriisSTT/RawriisSTT.exe`.

**CUDA support in the built exe:**

By default the exe bundles CPU-only PyTorch. To include CUDA support:
```bat
pip install torch --index-url https://download.pytorch.org/whl/cu121
```
Run this before building. Note: CUDA torch is several GB and will significantly increase the output size.

---

## Linux

```bash
bash build/build_linux.sh
```

This script:
1. Installs `pyinstaller`, `pillow`, and `openvr` via pip.
2. Runs PyInstaller with `build/windows.spec` (this same spec file is used for both Windows and Linux - there is no separate Linux spec).
3. Outputs the build to `dist/RawriisSTT/RawriisSTT`.

---

## Output Structure

After a successful build:

```
dist/
  RawriisSTT/
    RawriisSTT.exe        ← the executable
    _internal/            ← bundled Python runtime + dependencies
      assets/             ← icons and sounds
      steamvr/            ← SteamVR action manifest and controller bindings
      openvr/             ← OpenVR Python wrapper
      ...
```

Distribute the entire `dist/RawriisSTT/` folder - the exe won't run without `_internal/`.

---

## Spec File

The PyInstaller spec is at `build/windows.spec`. Key things it handles:

- Locates `openvr` via `importlib.util.find_spec` and bundles it explicitly (PyInstaller can silently drop it otherwise).
- Copies `assets/` and `steamvr/` into `_internal/`.
- Auto-converts the PNG icon to ICO on Windows using Pillow.
- Sets `uac_admin=False` - elevation breaks OpenVR's connection to SteamVR (which runs as a normal user).
- Sets `console=False` - no terminal window for end users.
