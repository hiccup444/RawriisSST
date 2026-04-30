from __future__ import annotations

from PyQt6.QtCore import QObject, Qt, QThread, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QComboBox,
    QPlainTextEdit,
)

import logging as _logging
import sys as _sys
_sdlog = _logging.getLogger(__name__)

from ..config.settings import AppSettings, save_settings


def _is_cuda_available() -> bool:
    """Return True if a CUDA-capable device is accessible to ctranslate2/faster-whisper."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        pass
    # Fallback: probe for the CUDA runtime library directly
    try:
        import ctypes
        ctypes.cdll.LoadLibrary("nvcuda.dll" if _sys.platform == "win32" else "libcuda.so.1")
        return True
    except OSError:
        return False


def _get_cuda_device_names() -> list[str]:
    """Return a list of CUDA device display names, one per GPU.

    Tries nvidia-smi first (works on Windows and Linux without extra deps),
    then falls back to torch if available, then to generic "GPU N" labels.
    """
    import subprocess as _sp
    try:
        result = _sp.run(
            ["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            names = []
            for line in result.stdout.strip().splitlines():
                parts = line.split(",", 1)
                if len(parts) == 2:
                    names.append(f"GPU {parts[0].strip()}: {parts[1].strip()}")
            if names:
                return names
    except Exception:
        pass

    # Fallback: torch
    try:
        import torch
        if torch.cuda.is_available():
            return [f"GPU {i}: {torch.cuda.get_device_name(i)}"
                    for i in range(torch.cuda.device_count())]
    except Exception:
        pass

    # Last resort: count from ctranslate2 and use generic labels
    try:
        import ctranslate2
        count = ctranslate2.get_cuda_device_count()
        return [f"GPU {i}" for i in range(max(count, 1))]
    except Exception:
        return ["GPU 0"]


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._selected_whisper_model: str = settings.whisper_model
        self.setWindowTitle("Settings")
        self.setMinimumWidth(440)
        self.setModal(True)

        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._make_general_tab(), "General")
        tabs.addTab(self._make_stt_tab(), "Speech-to-Text")
        tabs.addTab(self._make_tts_tab(), "Text-to-Speech")
        tabs.addTab(self._make_hotkeys_tab(), "Hotkeys")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ Tabs

    def _make_general_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # OSC group
        osc_group = QGroupBox("OSC (VRChat)")
        osc_form = QFormLayout(osc_group)

        self._osc_address = QLineEdit(self.settings.osc_address)
        osc_form.addRow("Address:", self._osc_address)

        self._osc_port = QSpinBox()
        self._osc_port.setRange(1, 65535)
        self._osc_port.setValue(self.settings.osc_port)
        osc_form.addRow("Port:", self._osc_port)

        self._chk_send_osc = QCheckBox("Enable OSC sending")
        self._chk_send_osc.setChecked(self.settings.send_osc)
        osc_form.addRow(self._chk_send_osc)

        self._chk_chatbox = QCheckBox("Send to VRChat chatbox")
        self._chk_chatbox.setChecked(self.settings.use_chatbox)
        osc_form.addRow(self._chk_chatbox)

        self._chk_notification = QCheckBox("Play VRChat chatbox notification sound")
        self._chk_notification.setChecked(self.settings.chatbox_play_notification)
        osc_form.addRow(self._chk_notification)

        self._chk_show_keyboard = QCheckBox("Show Keyboard Before Sending Message")
        self._chk_show_keyboard.setChecked(self.settings.chatbox_show_keyboard)
        self._chk_show_keyboard.setToolTip(
            "When enabled, VRChat's keyboard will pop up with your transcription\n"
            "so you can review or edit it before it is sent to the chatbox."
        )
        osc_form.addRow(self._chk_show_keyboard)

        layout.addWidget(osc_group)

        # UI group
        ui_group = QGroupBox("Interface")
        ui_form = QFormLayout(ui_group)

        self._chk_on_top = QCheckBox("Always on top")
        self._chk_on_top.setChecked(self.settings.always_on_top)
        ui_form.addRow(self._chk_on_top)

        self._chk_dark_mode = QCheckBox("Dark mode")
        self._chk_dark_mode.setChecked(self.settings.dark_mode)
        self._chk_dark_mode.toggled.connect(self._on_dark_mode_toggled)
        ui_form.addRow(self._chk_dark_mode)

        layout.addWidget(ui_group)

        # Sounds group
        sounds_group = QGroupBox("Notification Sounds")
        sounds_form = QFormLayout(sounds_group)

        self._chk_ptt_sound = QCheckBox("Enable PTT sounds")
        self._chk_ptt_sound.setChecked(self.settings.ptt_sound_enabled)
        sounds_form.addRow(self._chk_ptt_sound)

        self._ptt_volume, vol_lbl = _make_slider(
            0, 100, int(self.settings.ptt_sound_volume * 100),
            lambda v: f"{v}%",
        )
        sounds_form.addRow("PTT sound volume:", _slider_row(self._ptt_volume, vol_lbl))

        layout.addWidget(sounds_group)
        return w

    def _make_stt_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Whisper group
        whisper_group = QGroupBox("Whisper (local)")
        wf = QVBoxLayout(whisper_group)

        # Compute device
        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("Compute device:"))
        self._whisper_device = QComboBox()
        self._whisper_device.addItems(["cpu", "cuda"])
        self._whisper_device.setFixedWidth(80)
        cuda_ok = _is_cuda_available()
        if not cuda_ok:
            cuda_item = self._whisper_device.model().item(1)
            cuda_item.setEnabled(False)
            cuda_item.setToolTip("CUDA not available — requires an NVIDIA GPU and CUDA toolkit")
        saved_device = self.settings.whisper_device
        effective_device = saved_device if cuda_ok or saved_device != "cuda" else "cpu"
        self._whisper_device.setCurrentText(effective_device)
        device_row.addWidget(self._whisper_device)

        # GPU selector (only shown/enabled when CUDA is selected)
        self._gpu_label = QLabel("GPU:")
        self._gpu_label.setContentsMargins(8, 0, 0, 0)
        self._whisper_gpu = QComboBox()
        self._whisper_gpu.setFixedWidth(200)
        if cuda_ok:
            gpu_names = _get_cuda_device_names()
            self._whisper_gpu.addItems(gpu_names)
            saved_idx = self.settings.whisper_device_index
            if 0 <= saved_idx < len(gpu_names):
                self._whisper_gpu.setCurrentIndex(saved_idx)
        else:
            self._whisper_gpu.addItem("GPU 0")
        device_row.addWidget(self._gpu_label)
        device_row.addWidget(self._whisper_gpu)
        device_row.addStretch()

        # Show GPU selector only when CUDA is selected
        def _update_gpu_visibility(device_text: str) -> None:
            visible = device_text == "cuda" and cuda_ok
            self._gpu_label.setVisible(visible)
            self._whisper_gpu.setVisible(visible)

        self._whisper_device.currentTextChanged.connect(_update_gpu_visibility)
        _update_gpu_visibility(effective_device)
        wf.addLayout(device_row)

        # Per-model rows
        self._model_rows: dict[str, _ModelRow] = {}
        from ..stt.whisper_models import MODELS
        for info in MODELS:
            row = _ModelRow(info, self.settings.whisper_model)
            row.selected.connect(self._on_model_selected)
            self._model_rows[info.key] = row
            wf.addWidget(row)

        # Download progress label (shared across all rows)
        self._dl_status = QLabel("")
        self._dl_status.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        self._dl_status.setWordWrap(True)
        wf.addWidget(self._dl_status)

        # Word suppression list
        suppress_label = QLabel("Banned words / phrases (one per line):")
        suppress_label.setToolTip(
            "Words or phrases whose tokens will be suppressed inside Whisper's beam search,\n"
            "so the model cannot generate them at all — not just stripped after the fact.\n"
            "Useful for recurring hallucinations (e.g. 'Thanks for watching.').\n"
            "Takes effect the next time the model is loaded."
        )
        wf.addWidget(suppress_label)
        self._suppress_words = QPlainTextEdit()
        self._suppress_words.setPlaceholderText(
            "e.g.\nthanks for watching\nplease subscribe\num\nuh"
        )
        self._suppress_words.setFixedHeight(72)
        self._suppress_words.setPlainText(
            "\n".join(self.settings.whisper_suppress_words)
        )
        wf.addWidget(self._suppress_words)

        layout.addWidget(whisper_group)

        # Azure group
        azure_group = QGroupBox("Azure Speech")
        af = QFormLayout(azure_group)

        self._azure_key = QLineEdit(self.settings.azure_key)
        self._azure_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._azure_key.setPlaceholderText("Subscription key")
        af.addRow("API Key:", self._azure_key)

        self._azure_region = QLineEdit(self.settings.azure_region)
        self._azure_region.setPlaceholderText("e.g. eastus")
        af.addRow("Region:", self._azure_region)

        layout.addWidget(azure_group)

        # Vosk group
        vosk_group = QGroupBox("Vosk (offline)")
        vf = QVBoxLayout(vosk_group)

        from ..stt.vosk_models import MODELS as VOSK_MODELS
        for info in VOSK_MODELS:
            row = _VoskModelRow(info)
            vf.addWidget(row)

        layout.addWidget(vosk_group)

        # VAD group
        vad_group = QGroupBox("Voice Activity Detection (VAD)")
        vad_form = QFormLayout(vad_group)

        try:
            import webrtcvad as _wv  # noqa: F401
            _webrtcvad_ok = True
        except Exception:
            _webrtcvad_ok = False

        self._chk_vad = QCheckBox("Enable VAD (reduces false positives)")
        self._chk_vad.setChecked(self.settings.vad_enabled and _webrtcvad_ok)
        if not _webrtcvad_ok:
            self._chk_vad.setEnabled(False)
            vad_group.setToolTip(
                "webrtcvad is not installed or failed to import.\n"
                "Fix: pip install \"setuptools<81\" then pip install webrtcvad\n"
                "(setuptools>=81 removed pkg_resources which webrtcvad requires)"
            )
        vad_form.addRow(self._chk_vad)

        self._vad_aggr = QSpinBox()
        self._vad_aggr.setRange(0, 3)
        self._vad_aggr.setValue(self.settings.vad_aggressiveness)
        self._vad_aggr.setToolTip("0 = least aggressive, 3 = most aggressive")
        vad_form.addRow("Aggressiveness (0–3):", self._vad_aggr)

        self._silence_ms = QSpinBox()
        self._silence_ms.setRange(100, 5000)
        self._silence_ms.setSingleStep(100)
        self._silence_ms.setSuffix(" ms")
        self._silence_ms.setValue(self.settings.silence_threshold_ms)
        vad_form.addRow("Silence threshold:", self._silence_ms)

        self._max_record, rec_lbl = _make_slider(
            8, 15, self.settings.max_record_seconds,
            lambda v: f"{v} s",
        )
        vad_form.addRow("Max recording length:", _slider_row(self._max_record, rec_lbl))

        layout.addWidget(vad_group)
        return w

    def _make_tts_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        el_group = QGroupBox("ElevenLabs")
        ef = QFormLayout(el_group)

        self._el_key = QLineEdit(self.settings.elevenlabs_api_key)
        self._el_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._el_key.setPlaceholderText("xi-api-key")
        ef.addRow("API Key:", self._el_key)

        note = QLabel(
            "Paste your ElevenLabs API key here.\n"
            "Use the Refresh Voices button in the main window to load your voice library."
        )
        note.setStyleSheet("color: #888888; font-size: 11px;")
        note.setWordWrap(True)
        ef.addRow(note)

        layout.addWidget(el_group)

        polly_group = QGroupBox("Amazon Polly")
        pf = QFormLayout(polly_group)

        self._polly_access_key = QLineEdit(self.settings.polly_access_key_id)
        self._polly_access_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._polly_access_key.setPlaceholderText("AWS Access Key ID")
        pf.addRow("Access Key ID:", self._polly_access_key)

        self._polly_secret_key = QLineEdit(self.settings.polly_secret_access_key)
        self._polly_secret_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._polly_secret_key.setPlaceholderText("AWS Secret Access Key")
        pf.addRow("Secret Key:", self._polly_secret_key)

        self._polly_region = QLineEdit(self.settings.polly_region)
        self._polly_region.setPlaceholderText("e.g. us-east-1")
        pf.addRow("Region:", self._polly_region)

        polly_note = QLabel(
            "Requires boto3 (pip install boto3) and an AWS account with Polly access.\n"
            "Leave Access Key fields blank to use the AWS credential chain\n"
            "(env vars / ~/.aws/credentials). Use Refresh Voices in the main\n"
            "window after saving to load the voice list."
        )
        polly_note.setStyleSheet("color: #888888; font-size: 11px;")
        polly_note.setWordWrap(True)
        pf.addRow(polly_note)

        layout.addWidget(polly_group)

        # ── Playback group ────────────────────────────────────────────
        pb_group = QGroupBox("Playback")
        pf = QFormLayout(pb_group)

        self._tts_delay_before = QSpinBox()
        self._tts_delay_before.setRange(0, 10000)
        self._tts_delay_before.setSingleStep(50)
        self._tts_delay_before.setSuffix(" ms")
        self._tts_delay_before.setValue(self.settings.tts_delay_before_audio_ms)
        self._tts_delay_before.setToolTip(
            "Wait this many milliseconds after text output before starting audio playback."
        )
        pf.addRow("Delay Before Audio:", self._tts_delay_before)

        self._chk_stop_on_new = QCheckBox("Stop Currently Playing TTS on New TTS")
        self._chk_stop_on_new.setChecked(self.settings.tts_stop_on_new)
        self._chk_stop_on_new.setToolTip(
            "When a new TTS message arrives, immediately stop the currently playing message."
        )
        pf.addRow(self._chk_stop_on_new)

        self._chk_queue = QCheckBox("Message Queue")
        self._chk_queue.setChecked(self.settings.tts_queue_enabled)
        self._chk_queue.setToolTip(
            "Queue multiple TTS messages and play them one after another."
        )
        pf.addRow(self._chk_queue)

        self._tts_queue_delay = QSpinBox()
        self._tts_queue_delay.setRange(0, 10000)
        self._tts_queue_delay.setSingleStep(50)
        self._tts_queue_delay.setSuffix(" ms")
        self._tts_queue_delay.setValue(self.settings.tts_queue_delay_ms)
        self._tts_queue_delay.setToolTip(
            "Wait this many milliseconds before playing the next message in the queue."
        )
        pf.addRow("Delay Before Next in Queue:", self._tts_queue_delay)

        self._chk_smart_split = QCheckBox("Smart String Splitting")
        self._chk_smart_split.setChecked(self.settings.tts_smart_split)
        self._chk_smart_split.setToolTip(
            "Automatically break messages longer than the character limit into multiple messages."
        )
        pf.addRow(self._chk_smart_split)

        self._tts_split_limit = QSpinBox()
        self._tts_split_limit.setRange(10, 500)
        self._tts_split_limit.setSingleStep(10)
        self._tts_split_limit.setSuffix(" chars")
        self._tts_split_limit.setValue(self.settings.tts_smart_split_limit)
        self._tts_split_limit.setToolTip(
            "Maximum characters per message chunk when Smart String Splitting is enabled.\n"
            "VRChat chatbox limit is 144 characters."
        )
        pf.addRow("Split Limit:", self._tts_split_limit)

        layout.addWidget(pb_group)

        # ── Mutual exclusion and dependency logic ─────────────────────
        def _update_queue_deps() -> None:
            queue_on = self._chk_queue.isChecked()
            self._tts_queue_delay.setEnabled(queue_on)
            self._chk_smart_split.setEnabled(queue_on)
            self._tts_split_limit.setEnabled(queue_on and self._chk_smart_split.isChecked())

        def _on_queue_toggled(checked: bool) -> None:
            if checked and self._chk_stop_on_new.isChecked():
                self._chk_stop_on_new.setChecked(False)
            _update_queue_deps()

        def _on_stop_on_new_toggled(checked: bool) -> None:
            if checked and self._chk_queue.isChecked():
                self._chk_queue.setChecked(False)
            _update_queue_deps()

        def _on_smart_split_toggled(checked: bool) -> None:
            self._tts_split_limit.setEnabled(self._chk_queue.isChecked() and checked)

        self._chk_queue.toggled.connect(_on_queue_toggled)
        self._chk_stop_on_new.toggled.connect(_on_stop_on_new_toggled)
        self._chk_smart_split.toggled.connect(_on_smart_split_toggled)

        # Apply initial disabled states
        _update_queue_deps()

        return w

    def _make_hotkeys_tab(self) -> QWidget:
        from PyQt6.QtWidgets import QFrame
        from .hotkey_capture import HotkeyCaptureDialog

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # ── PTT Key ───────────────────────────────────────────────────
        hk_group = QGroupBox("PTT Key")
        hk_form = QFormLayout(hk_group)

        self._ptt_key_val: str = self.settings.ptt_key
        self._lbl_ptt_key = QLabel(HotkeyCaptureDialog.fmt(self._ptt_key_val) or "(unset)")
        self._lbl_ptt_key.setFrameShape(QFrame.Shape.StyledPanel)
        self._lbl_ptt_key.setMinimumWidth(90)
        self._lbl_ptt_key.setStyleSheet("padding: 2px 6px;")
        btn_set_ptt = QPushButton("Set...")
        btn_set_ptt.setFixedWidth(55)

        def _set_ptt() -> None:
            dlg = HotkeyCaptureDialog(current_key=self._ptt_key_val, title="Set PTT Key", parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.captured_key():
                self._ptt_key_val = dlg.captured_key()
                self._lbl_ptt_key.setText(HotkeyCaptureDialog.fmt(self._ptt_key_val))

        btn_set_ptt.clicked.connect(_set_ptt)

        ptt_row = QWidget()
        ptt_row_layout = QHBoxLayout(ptt_row)
        ptt_row_layout.setContentsMargins(0, 0, 0, 0)
        ptt_row_layout.addWidget(self._lbl_ptt_key)
        ptt_row_layout.addWidget(btn_set_ptt)
        ptt_row_layout.addStretch()
        hk_form.addRow("PTT key:", ptt_row)

        note = QLabel("Used for both PTT Hold and PTT Toggle modes. Also configurable in the main window.")
        note.setStyleSheet("color: #888888; font-size: 11px;")
        note.setWordWrap(True)
        hk_form.addRow(note)

        layout.addWidget(hk_group)

        # ── TTS Hotkeys ───────────────────────────────────────────────
        tts_hk_group = QGroupBox("TTS Hotkeys")
        tts_hk_form = QFormLayout(tts_hk_group)

        def _make_clearable_hotkey_row(current: str, title: str):
            val_holder = [current]
            lbl = QLabel(HotkeyCaptureDialog.fmt(current) or "(unbound)")
            lbl.setFrameShape(QFrame.Shape.StyledPanel)
            lbl.setMinimumWidth(90)
            lbl.setStyleSheet("padding: 2px 6px;")

            btn_set = QPushButton("Set...")
            btn_set.setFixedWidth(55)
            btn_clear = QPushButton("Clear")
            btn_clear.setFixedWidth(50)

            def _set():
                dlg = HotkeyCaptureDialog(current_key=val_holder[0], title=title, parent=self)
                if dlg.exec() == QDialog.DialogCode.Accepted and dlg.captured_key():
                    val_holder[0] = dlg.captured_key()
                    lbl.setText(HotkeyCaptureDialog.fmt(val_holder[0]))

            def _clear():
                val_holder[0] = ""
                lbl.setText("(unbound)")

            btn_set.clicked.connect(_set)
            btn_clear.clicked.connect(_clear)

            row_w = QWidget()
            row_layout = QHBoxLayout(row_w)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(lbl)
            row_layout.addWidget(btn_set)
            row_layout.addWidget(btn_clear)
            row_layout.addStretch()
            return row_w, val_holder

        self._quick_stop_key_row, self._quick_stop_key_val_holder = _make_clearable_hotkey_row(
            self.settings.tts_quick_stop_key, "Set Quick Stop TTS Key"
        )
        self._quick_stop_key_row.setToolTip(
            "Global hotkey that immediately stops the currently playing TTS message.\n"
            "If Message Queue is enabled the next queued message will play after the configured delay."
        )
        tts_hk_form.addRow("Quick Stop TTS:", self._quick_stop_key_row)

        self._resend_key_row, self._resend_key_val_holder = _make_clearable_hotkey_row(
            self.settings.tts_resend_key, "Set Resend Key"
        )
        self._resend_key_row.setToolTip(
            "Global hotkey that re-sends the last transcription to OSC (if enabled) and TTS (if enabled)."
        )
        tts_hk_form.addRow("Resend Last Transcription:", self._resend_key_row)

        tts_note = QLabel("Click Set... to assign a key. Click Clear to unbind.")
        tts_note.setStyleSheet("color: #888888; font-size: 11px;")
        tts_note.setWordWrap(True)
        tts_hk_form.addRow(tts_note)

        layout.addWidget(tts_hk_group)

        vr_group = QGroupBox("SteamVR Input")
        vr_layout = QVBoxLayout(vr_group)

        vr_note = QLabel("VR Bindings are managed by SteamVR")
        vr_note.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        vr_layout.addWidget(vr_note)

        vr_btn = QPushButton("Open SteamVR Bindings")
        vr_btn.setFixedWidth(180)
        vr_btn.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl("steam://open/steamvrinput")
        ))
        vr_layout.addWidget(vr_btn)

        layout.addWidget(vr_group)
        return w

    # ------------------------------------------------------------------ Actions

    @pyqtSlot(str)
    def _on_model_selected(self, model_key: str) -> None:
        _sdlog.debug("_on_model_selected called with key=%r", model_key)
        self._selected_whisper_model = model_key
        # Deselect all other rows
        for key, row in self._model_rows.items():
            if key != model_key:
                row.set_selected(False)

    def _on_dark_mode_toggled(self, checked: bool) -> None:
        from PyQt6.QtWidgets import QApplication
        from .theme import apply_theme
        apply_theme(QApplication.instance(), checked)

    def closeEvent(self, event) -> None:
        """Pressing X on the settings window saves just like clicking OK."""
        self._do_save()
        self.setResult(QDialog.DialogCode.Accepted)
        event.accept()

    def _save_and_accept(self) -> None:
        self._do_save()
        self.accept()

    def _do_save(self) -> None:
        _sdlog.debug("_do_save: _selected_whisper_model=%r", self._selected_whisper_model)
        s = self.settings

        # General
        s.osc_address = self._osc_address.text().strip() or "127.0.0.1"
        s.osc_port = self._osc_port.value()
        s.send_osc = self._chk_send_osc.isChecked()
        s.use_chatbox = self._chk_chatbox.isChecked()
        s.chatbox_play_notification = self._chk_notification.isChecked()
        s.chatbox_show_keyboard = self._chk_show_keyboard.isChecked()
        s.always_on_top = self._chk_on_top.isChecked()
        s.dark_mode = self._chk_dark_mode.isChecked()

        # STT
        s.whisper_model = self._selected_whisper_model
        s.whisper_device = self._whisper_device.currentText()
        s.whisper_device_index = self._whisper_gpu.currentIndex()
        s.whisper_suppress_words = [
            w for w in (line.strip() for line in self._suppress_words.toPlainText().splitlines())
            if w
        ]
        s.azure_key = self._azure_key.text().strip()
        s.azure_region = self._azure_region.text().strip()
        # Vosk model path is auto-managed; update from cache
        from ..stt.vosk_models import get_model_path as vosk_get_model_path, MODELS as VOSK_MODELS
        for _vinfo in VOSK_MODELS:
            _p = vosk_get_model_path(_vinfo.key)
            if _p:
                s.vosk_model_path = _p
                break
        else:
            s.vosk_model_path = ""
        s.vad_enabled = self._chk_vad.isChecked()
        s.vad_aggressiveness = self._vad_aggr.value()
        s.silence_threshold_ms = self._silence_ms.value()
        s.max_record_seconds = self._max_record.value()

        # General — sounds
        s.ptt_sound_enabled = self._chk_ptt_sound.isChecked()
        s.ptt_sound_volume = self._ptt_volume.value() / 100.0

        # TTS
        s.elevenlabs_api_key = self._el_key.text().strip()
        s.polly_access_key_id = self._polly_access_key.text().strip()
        s.polly_secret_access_key = self._polly_secret_key.text().strip()
        s.polly_region = self._polly_region.text().strip() or "us-east-1"
        s.tts_delay_before_audio_ms = self._tts_delay_before.value()
        s.tts_stop_on_new = self._chk_stop_on_new.isChecked()
        s.tts_queue_enabled = self._chk_queue.isChecked()
        s.tts_queue_delay_ms = self._tts_queue_delay.value()
        s.tts_smart_split = self._chk_smart_split.isChecked()
        s.tts_smart_split_limit = self._tts_split_limit.value()

        # Hotkeys
        if self._ptt_key_val:
            s.ptt_key = self._ptt_key_val
        s.tts_quick_stop_key = self._quick_stop_key_val_holder[0]
        s.tts_resend_key = self._resend_key_val_holder[0]

        save_settings(s)


# ---------------------------------------------------------------------------
# Per-model row widget
# ---------------------------------------------------------------------------

class _ModelRow(QWidget):
    """A single row in the Whisper model list: radio-style select + download button."""

    selected = pyqtSignal(str)   # emits model key when the user selects this row

    def __init__(self, info, current_model_key: str, parent=None) -> None:
        super().__init__(parent)
        self._info = info
        self._thread: QThread | None = None
        self._worker: _DownloadWorker | None = None

        from ..stt.whisper_models import is_model_cached
        self._cached = is_model_cached(info.key)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)

        # Radio-style select button (checkable)
        self._btn_select = QPushButton(info.display)
        self._btn_select.setCheckable(True)
        self._btn_select.setChecked(info.key == current_model_key)
        self._btn_select.setFixedWidth(100)
        self._btn_select.clicked.connect(self._on_select)
        layout.addWidget(self._btn_select)

        # Size label
        size_text = f"{info.size_mb} MB" if info.size_mb < 1000 else f"{info.size_mb/1000:.1f} GB"
        size_lbl = QLabel(size_text)
        size_lbl.setFixedWidth(60)
        size_lbl.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(size_lbl)

        # Status label / progress bar
        self._status_lbl = QLabel()
        self._status_lbl.setFixedWidth(140)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)   # indeterminate
        self._progress.setFixedHeight(14)
        self._progress.setFixedWidth(140)
        self._progress.hide()
        layout.addWidget(self._status_lbl)
        layout.addWidget(self._progress)

        # Download / Delete button
        self._btn_action = QPushButton()
        self._btn_action.setFixedWidth(80)
        self._btn_action.clicked.connect(self._on_action)
        layout.addWidget(self._btn_action)

        self._refresh_state()

    def _refresh_state(self) -> None:
        from ..stt.whisper_models import is_model_cached, get_cached_size_mb
        self._cached = is_model_cached(self._info.key)
        if self._cached:
            mb = get_cached_size_mb(self._info.key)
            self._status_lbl.setText(f"Downloaded ({mb} MB)" if mb else "Downloaded")
            self._status_lbl.setStyleSheet("color: #4caf50; font-size: 11px;")
            self._btn_action.setText("Delete")
            self._btn_action.setEnabled(True)
            self._btn_select.setEnabled(True)
        else:
            self._status_lbl.setText("Not downloaded")
            self._status_lbl.setStyleSheet("color: #888888; font-size: 11px;")
            self._btn_action.setText("Download")
            self._btn_action.setEnabled(True)
            self._btn_select.setEnabled(False)
            if self._btn_select.isChecked():
                self._btn_select.setChecked(False)

    def set_selected(self, value: bool) -> None:
        self._btn_select.setChecked(value)

    @pyqtSlot()
    def _on_select(self) -> None:
        _sdlog.debug("_on_select called for key=%r, _cached=%r", self._info.key, self._cached)
        if not self._cached:
            self._btn_select.setChecked(False)
            return
        self._btn_select.setChecked(True)
        self.selected.emit(self._info.key)

    @pyqtSlot()
    def _on_action(self) -> None:
        if self._cached:
            self._delete_model()
        else:
            self._start_download()

    def _start_download(self) -> None:
        self._btn_action.setEnabled(False)
        self._status_lbl.hide()
        self._progress.show()

        self._worker = _DownloadWorker(self._info.key)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_download_done)
        self._worker.error.connect(self._on_download_error)
        self._thread.start()

    @pyqtSlot()
    def _on_download_done(self) -> None:
        self._progress.hide()
        self._status_lbl.show()
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        self._refresh_state()

    @pyqtSlot(str)
    def _on_download_error(self, message: str) -> None:
        self._progress.hide()
        self._status_lbl.show()
        self._status_lbl.setText("Download failed")
        self._status_lbl.setStyleSheet("color: #f44336; font-size: 11px;")
        self._btn_action.setEnabled(True)
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.critical(self, "Download Failed", message)

    def _delete_model(self) -> None:
        from PyQt6.QtWidgets import QMessageBox
        from ..stt.whisper_models import _model_cache_dir, _get_info
        import shutil

        reply = QMessageBox.question(
            self,
            "Delete Model",
            f"Delete the {self._info.display} model from disk?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        info = _get_info(self._info.key)
        if info:
            cache_dir = _model_cache_dir(info.repo_id)
            if cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)
        self._refresh_state()


# ---------------------------------------------------------------------------
# Background download worker
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Slider helpers
# ---------------------------------------------------------------------------

def _make_slider(
    min_val: int, max_val: int, value: int, label_fmt
) -> tuple[QSlider, QLabel]:
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(min_val, max_val)
    slider.setValue(value)
    lbl = QLabel(label_fmt(value))
    lbl.setFixedWidth(40)
    lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    slider.valueChanged.connect(lambda v: lbl.setText(label_fmt(v)))
    return slider, lbl


def _slider_row(slider: QSlider, label: QLabel) -> QWidget:
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(slider)
    row.addWidget(label)
    return w


class _DownloadWorker(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, model_key: str) -> None:
        super().__init__()
        self._key = model_key

    @pyqtSlot()
    def run(self) -> None:
        from ..stt.whisper_models import download_model
        try:
            download_model(self._key)
            self.finished.emit()
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Vosk per-model row widget
# ---------------------------------------------------------------------------

class _VoskModelRow(QWidget):
    """A single row for a managed Vosk model: shows status + download/delete."""

    def __init__(self, info, parent=None) -> None:
        super().__init__(parent)
        self._info = info
        self._thread: QThread | None = None
        self._worker: "_VoskDownloadWorker | None" = None

        from ..stt.vosk_models import is_model_cached
        self._cached = is_model_cached(info.key)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)

        # Model name label
        name_lbl = QLabel(info.display)
        name_lbl.setFixedWidth(160)
        layout.addWidget(name_lbl)

        # Approximate size label
        size_lbl = QLabel(f"~{info.size_mb} MB")
        size_lbl.setFixedWidth(60)
        size_lbl.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(size_lbl)

        # Status label / progress bar
        self._status_lbl = QLabel()
        self._status_lbl.setFixedWidth(140)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setFixedHeight(14)
        self._progress.setFixedWidth(140)
        self._progress.hide()
        layout.addWidget(self._status_lbl)
        layout.addWidget(self._progress)

        # Download / Delete button
        self._btn_action = QPushButton()
        self._btn_action.setFixedWidth(80)
        self._btn_action.clicked.connect(self._on_action)
        layout.addWidget(self._btn_action)

        self._refresh_state()

    def _refresh_state(self) -> None:
        from ..stt.vosk_models import is_model_cached, get_cached_size_mb
        self._cached = is_model_cached(self._info.key)
        if self._cached:
            mb = get_cached_size_mb(self._info.key)
            self._status_lbl.setText(f"Downloaded ({mb} MB)" if mb else "Downloaded")
            self._status_lbl.setStyleSheet("color: #4caf50; font-size: 11px;")
            self._btn_action.setText("Delete")
            self._btn_action.setEnabled(True)
        else:
            self._status_lbl.setText("Not downloaded")
            self._status_lbl.setStyleSheet("color: #888888; font-size: 11px;")
            self._btn_action.setText("Download")
            self._btn_action.setEnabled(True)

    @pyqtSlot()
    def _on_action(self) -> None:
        if self._cached:
            self._delete_model()
        else:
            self._start_download()

    def _start_download(self) -> None:
        self._btn_action.setEnabled(False)
        self._status_lbl.hide()
        self._progress.show()

        self._worker = _VoskDownloadWorker(self._info.key)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_download_done)
        self._worker.error.connect(self._on_download_error)
        self._thread.start()

    @pyqtSlot()
    def _on_download_done(self) -> None:
        self._progress.hide()
        self._status_lbl.show()
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        self._refresh_state()

    @pyqtSlot(str)
    def _on_download_error(self, message: str) -> None:
        self._progress.hide()
        self._status_lbl.show()
        self._status_lbl.setText("Download failed")
        self._status_lbl.setStyleSheet("color: #f44336; font-size: 11px;")
        self._btn_action.setEnabled(True)
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.critical(self, "Download Failed", message)

    def _delete_model(self) -> None:
        from PyQt6.QtWidgets import QMessageBox
        from ..stt.vosk_models import delete_model

        reply = QMessageBox.question(
            self,
            "Delete Model",
            f"Delete the {self._info.display} model from disk?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        delete_model(self._info.key)
        self._refresh_state()


class _VoskDownloadWorker(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, model_key: str) -> None:
        super().__init__()
        self._key = model_key

    @pyqtSlot()
    def run(self) -> None:
        from ..stt.vosk_models import download_model
        try:
            download_model(self._key)
            self.finished.emit()
        except Exception as exc:
            self.error.emit(str(exc))
