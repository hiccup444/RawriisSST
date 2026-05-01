from __future__ import annotations

import logging
import time
from typing import Optional

from PyQt6.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..audio.devices import AudioDevice, enumerate_all_devices, invalidate_device_cache, reinitialize_portaudio
from ..audio.linux_virtual_cable import (
    is_supported as _vc_supported,
    exists as _vc_exists,
    CABLE_NAME as _VC_CABLE_NAME,
)
from ..config.settings import AppSettings, save_settings
from ..osc.vrchat_osc import VRChatOSC
from ..stt.base import STTEngine, STTResult
from ..updater import UpdateChecker
from ..version import __version__, RELEASES_URL

logger = logging.getLogger(__name__)

# Language display names → code
LANGUAGES: list[tuple[str, str]] = [
    ("Auto-detect", "auto"),
    ("English", "en"),
    ("Spanish", "es"),
    ("French", "fr"),
    ("German", "de"),
    ("Italian", "it"),
    ("Portuguese", "pt"),
    ("Dutch", "nl"),
    ("Russian", "ru"),
    ("Chinese", "zh"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Arabic", "ar"),
    ("Hindi", "hi"),
    ("Polish", "pl"),
    ("Swedish", "sv"),
    ("Turkish", "tr"),
]

# Ordered list of engine codes — index matches combo box position
ENGINE_CODES: list[str] = ["whisper", "azure", "vosk", "system"]

# TTS voice engine display names and their setting keys
TTS_ENGINE_CODES: list[str] = ["pyttsx3", "elevenlabs", "polly", "espeak"]
TTS_ENGINE_LABELS: list[str] = ["pyttsx3 (System)", "ElevenLabs", "Amazon Polly", "eSpeak (Moonbase)"]


from .hotkey_capture import HotkeyCaptureDialog as _HotkeyCaptureDialog

# ─────────────────────────────────────────── Hotkey capture dialog ──

class _PresetPickerDialog(QDialog):
    """List-based dialog for selecting (and optionally deleting) a saved preset."""

    def __init__(self, presets: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Load Preset")
        self.setMinimumWidth(320)
        self._presets = presets  # live reference; updated on delete

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self._list = QListWidget()
        self._list.addItems(sorted(presets.keys()))
        self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(self._accept)
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        self._btn_delete = QPushButton("Delete")
        self._btn_delete.clicked.connect(self._delete_selected)
        btn_row.addWidget(self._btn_delete)
        btn_row.addStretch()
        btn_load = QPushButton("Load")
        btn_load.setDefault(True)
        btn_load.clicked.connect(self._accept)
        btn_row.addWidget(btn_load)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    def selected_name(self) -> str | None:
        item = self._list.currentItem()
        return item.text() if item else None

    def _accept(self) -> None:
        if self._list.currentItem():
            self.accept()

    def _delete_selected(self) -> None:
        name = self.selected_name()
        if name is None:
            return
        reply = QMessageBox.question(
            self, "Delete Preset",
            f"Delete preset \"{name}\"?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        from ..config.presets import load_presets, save_presets
        presets = load_presets()
        presets.pop(name, None)
        save_presets(presets)
        row = self._list.currentRow()
        self._list.takeItem(row)
        if self._list.count() == 0:
            self.reject()  # no presets left — close dialog


# ─────────────────────────────────────────────────────────────────── Workers ──

class _STTWorker(QObject):
    """Runs the STT engine in a background QThread and forwards results."""

    result_ready = pyqtSignal(str, bool)   # (text, is_final)
    error_occurred = pyqtSignal(str)

    def __init__(self, engine: STTEngine, device_index: Optional[int], language: str) -> None:
        super().__init__()
        self._engine = engine
        self._device_index = device_index
        self._language = language

    @pyqtSlot()
    def run(self) -> None:
        def on_result(result: STTResult) -> None:
            self.result_ready.emit(result.text, result.is_final)

        try:
            self._engine.start_listening(
                callback=on_result,
                device_index=self._device_index,
                language=self._language,
            )
        except Exception as exc:
            self.error_occurred.emit(str(exc))

    def stop(self) -> None:
        self._engine.stop_listening()


class _WhisperLoadThread(QThread):
    """Background thread that loads a WhisperSTT model."""

    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, engine, parent=None) -> None:
        super().__init__(parent)
        self._engine = engine

    def run(self) -> None:
        try:
            self._engine.load_model()
            self.finished.emit()
        except BaseException as exc:
            self.error.emit(str(exc))


class _VoskLoadThread(QThread):
    """Background thread that loads a VoskSTT model."""

    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, engine, parent=None) -> None:
        super().__init__(parent)
        self._engine = engine

    def run(self) -> None:
        try:
            self._engine.load_model()
            self.finished.emit()
        except BaseException as exc:
            self.error.emit(str(exc))


class _AzureValidateThread(QThread):
    """Background thread that validates Azure Speech credentials."""

    validated = pyqtSignal()
    failed = pyqtSignal(str)  # human-readable error description

    def __init__(self, key: str, region: str, parent=None) -> None:
        super().__init__(parent)
        self._key = key
        self._region = region

    def run(self) -> None:
        try:
            import azure.cognitiveservices.speech as speechsdk
        except ImportError:
            self.failed.emit("azure-cognitiveservices-speech is not installed.")
            return

        if not self._key.strip():
            self.failed.emit("API Key Invalid")
            return
        if not self._region.strip():
            self.failed.emit("Region Missing")
            return

        try:
            config = speechsdk.SpeechConfig(
                subscription=self._key.strip(),
                region=self._region.strip(),
            )
            # Use a short silence push stream so the request completes quickly
            fmt = speechsdk.audio.AudioStreamFormat(
                samples_per_second=16000, bits_per_sample=16, channels=1
            )
            stream = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
            audio_config = speechsdk.audio.AudioConfig(stream=stream)
            recognizer = speechsdk.SpeechRecognizer(
                speech_config=config, audio_config=audio_config
            )
            # Push 0.5 s of silence then close; triggers a quick recognition cycle
            stream.write(bytes(16000))  # 0.5 s × 16000 Hz × 2 bytes / 2 = 16000 bytes
            stream.close()
            result = recognizer.recognize_once()

            if result.reason in (
                speechsdk.ResultReason.RecognizedSpeech,
                speechsdk.ResultReason.NoMatch,
            ):
                self.validated.emit()
            elif result.reason == speechsdk.ResultReason.Canceled:
                details = speechsdk.CancellationDetails(result)
                msg = (details.error_details or "").lower()
                if details.reason == speechsdk.CancellationReason.AuthenticationFailure:
                    if "region" in msg:
                        self.failed.emit("Region Mismatch")
                    else:
                        self.failed.emit("API Key Invalid")
                elif details.reason == speechsdk.CancellationReason.Error:
                    if "connection" in msg or "network" in msg or "timeout" in msg:
                        self.failed.emit("Network Error")
                    elif "region" in msg:
                        self.failed.emit("Region Mismatch")
                    else:
                        self.failed.emit(f"Error: {details.error_details}")
                else:
                    self.failed.emit("Network Error")
            else:
                self.validated.emit()
        except Exception as exc:
            msg = str(exc).lower()
            if "authentication" in msg or "unauthorized" in msg or "key" in msg:
                self.failed.emit("API Key Invalid")
            elif "connection" in msg or "network" in msg or "timeout" in msg:
                self.failed.emit("Network Error")
            else:
                self.failed.emit(f"Error: {exc}")


# ────────────────────────────────────────── ElevenLabs refresh thread ──

class _ELRefreshThread(QThread):
    """Fetches ElevenLabs voices + models in the background."""

    finished = pyqtSignal(list, list)  # (voices, models)
    failed = pyqtSignal(str)

    def __init__(self, api_key: str, parent=None) -> None:
        super().__init__(parent)
        self._api_key = api_key

    def run(self) -> None:
        try:
            from ..tts.elevenlabs_tts import fetch_voices, fetch_models
            voices = fetch_voices(self._api_key)
            models = fetch_models(self._api_key)
            self.finished.emit(voices, models)
        except Exception as exc:
            self.failed.emit(str(exc))


class _PollyRefreshThread(QThread):
    """Fetches Amazon Polly voices in the background."""

    finished = pyqtSignal(list)   # list of voice dicts
    failed = pyqtSignal(str)

    def __init__(
        self,
        access_key_id: str,
        secret_access_key: str,
        region: str,
        engine: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._region = region
        self._engine = engine

    def run(self) -> None:
        try:
            from ..tts.polly_tts import fetch_voices
            voices = fetch_voices(
                self._access_key_id,
                self._secret_access_key,
                self._region,
                self._engine,
            )
            self.finished.emit(voices)
        except Exception as exc:
            self.failed.emit(str(exc))


# ─────────────────────────────────────────────────────────────────── Window ──

class MainWindow(QMainWindow):
    # Signals to dispatch global hotkey callbacks (from pynput thread) to the GUI thread
    _quick_stop_signal = pyqtSignal()
    _resend_signal = pyqtSignal()
    # Signals to dispatch SteamVR action callbacks (from background thread) to the GUI thread
    _steamvr_ptt_press_signal   = pyqtSignal()
    _steamvr_ptt_release_signal = pyqtSignal()
    _steamvr_stop_signal        = pyqtSignal()
    _steamvr_repeat_signal      = pyqtSignal()

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.settings = settings
        self._osc = VRChatOSC(settings.osc_address, settings.osc_port)
        self._engine: Optional[STTEngine] = None
        self._worker: Optional[_STTWorker] = None
        self._thread: Optional[QThread] = None
        self._stopping_threads: list[tuple[QThread, Optional[_STTWorker]]] = []
        self._devices: list[AudioDevice] = []

        # Persistent Whisper engine (model stays loaded between listen sessions)
        self._whisper_engine = None
        self._ptt_handler = None
        self._ptt_active: bool = False      # True while PTT key is held/toggled on
        self._ptt_active_since: float = 0.0 # monotonic time when PTT last became active
        self._is_listening: bool = False     # True between _start_listening and _stop_listening

        # Persistent Vosk engine
        self._vosk_engine = None
        self._vosk_load_thread: Optional[_VoskLoadThread] = None

        # Azure validation state (runtime only — not persisted)
        self._azure_validated: bool = False
        self._azure_validate_thread: Optional[_AzureValidateThread] = None

        # Live transcribe accumulation — grows each segment; committed after silence gap
        self._live_accumulated: str = ""

        # Commit timer: fires 1.5 s after the last STT segment while in live-transcribe mode.
        # Interim sends (while speaking) are silent; only the commit send plays the notification.
        self._live_commit_timer = QTimer(self)
        self._live_commit_timer.setSingleShot(True)
        self._live_commit_timer.setInterval(1500)
        self._live_commit_timer.timeout.connect(self._commit_live_transcript)

        # Amazon Polly refresh thread
        self._polly_refresh_thread: Optional[_PollyRefreshThread] = None

        # Last recognized transcription (for Resend hotkey)
        self._last_transcription: str = ""

        # Global hotkey handlers (Quick Stop TTS, Resend Last Transcription)
        self._quick_stop_handler = None
        self._resend_handler = None
        self._quick_stop_signal.connect(self._do_quick_stop_tts)
        self._resend_signal.connect(self._do_resend_last_transcription)

        # SteamVR input manager
        self._steamvr_manager = None
        self._steamvr_ptt_press_signal.connect(self._do_ptt_press)
        self._steamvr_ptt_release_signal.connect(self._do_ptt_release)
        self._steamvr_stop_signal.connect(self._do_quick_stop_tts)
        self._steamvr_repeat_signal.connect(self._do_resend_last_transcription)

        # Load-model thread (Whisper)
        self._load_thread: Optional[_WhisperLoadThread] = None

        # PTT notification sounds
        from ..audio.sound_player import SoundPlayer
        self._sound_player = SoundPlayer(volume=settings.ptt_sound_volume)

        # Debounced settings save — coalesces rapid UI changes into one disk write
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(lambda: save_settings(self.settings))

        # Virtual cable refresh state (Linux/PipeWire device publication can be delayed)
        self._cable_refresh_attempt: int = 0
        self._cable_refresh_max_attempts: int = 14

        self.setWindowTitle("RawriisSTT")
        self.setMinimumWidth(520)
        self.setWindowFlags(
            Qt.WindowType.Window
            | (Qt.WindowType.WindowStaysOnTopHint if settings.always_on_top else Qt.WindowType.Widget)
        )

        self._build_ui()
        self._populate_all_devices()
        self._apply_settings()
        self._refresh_preset_btn()
        self._start_global_hotkeys()
        self._start_steamvr_input()
        self._start_update_check()

    # ------------------------------------------------------------------ UI build

    def _build_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.setCentralWidget(scroll)

        central = QWidget()
        scroll.setWidget(central)

        root = QVBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        # ── Title bar row ──────────────────────────────────────────────
        title_row = QHBoxLayout()
        title_lbl = QLabel("RawriisSTT")
        title_font = QFont()
        title_font.setPointSize(13)
        title_font.setBold(True)
        title_lbl.setFont(title_font)
        title_row.addWidget(title_lbl)
        version_lbl = QLabel(f"v{__version__}")
        version_lbl.setStyleSheet("color: #888888; font-size: 11px;")
        version_lbl.setContentsMargins(4, 0, 0, 0)
        title_row.addWidget(version_lbl)
        self._lbl_update = QLabel()
        self._lbl_update.setStyleSheet(
            "color: #f0a500; font-size: 11px; text-decoration: underline;"
        )
        self._lbl_update.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lbl_update.setContentsMargins(8, 0, 0, 0)
        self._lbl_update.hide()
        self._lbl_update.mousePressEvent = self._open_release_page
        title_row.addWidget(self._lbl_update)
        title_row.addStretch()
        self._btn_save_preset = QPushButton("Save Preset")
        self._btn_save_preset.setFixedWidth(90)
        self._btn_save_preset.clicked.connect(self._save_preset)
        title_row.addWidget(self._btn_save_preset)
        self._btn_load_preset = QPushButton("Load Preset")
        self._btn_load_preset.setFixedWidth(90)
        self._btn_load_preset.setEnabled(False)
        self._btn_load_preset.clicked.connect(self._load_preset)
        title_row.addWidget(self._btn_load_preset)
        self._btn_settings = QPushButton("Settings")
        self._btn_settings.setFixedWidth(80)
        self._btn_settings.clicked.connect(self._open_settings)
        title_row.addWidget(self._btn_settings)
        root.addLayout(title_row)

        root.addWidget(_h_line())

        # ── Config controls ────────────────────────────────────────────
        cfg_layout = QVBoxLayout()
        cfg_layout.setSpacing(6)

        self._cmb_device = _labeled_combo("Microphone:", cfg_layout)
        self._cmb_device.currentIndexChanged.connect(self._on_device_changed)

        # ── TTS output device rows (hidden until TTS enabled) ──────────
        self._tts_output_widget = QWidget()
        tts_out_layout = QVBoxLayout(self._tts_output_widget)
        tts_out_layout.setSpacing(6)
        tts_out_layout.setContentsMargins(0, 0, 0, 0)

        hp_row = QHBoxLayout()
        hp_lbl = QLabel("Headphones:")
        hp_lbl.setFixedWidth(90)
        hp_row.addWidget(hp_lbl)
        self._cmb_headphones = QComboBox()
        self._cmb_headphones.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._cmb_headphones.currentIndexChanged.connect(self._on_headphones_changed)
        hp_row.addWidget(self._cmb_headphones)
        tts_out_layout.addLayout(hp_row)

        cable_row = QHBoxLayout()
        cable_lbl = QLabel("Output Cable:")
        cable_lbl.setFixedWidth(90)
        cable_row.addWidget(cable_lbl)
        self._cmb_cable = QComboBox()
        self._cmb_cable.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._cmb_cable.currentIndexChanged.connect(self._on_cable_changed)
        cable_row.addWidget(self._cmb_cable)
        tts_out_layout.addLayout(cable_row)

        if _vc_supported():
            self._btn_create_cable = QPushButton("Create Virtual Cable")
            self._btn_create_cable.setToolTip(
                "Creates a PipeWire/PulseAudio virtual sink named RawriisCable.\n"
                "Use this as your Output Cable to route TTS audio into VRChat."
            )
            self._btn_refresh_cable = QPushButton("Refresh Devices")
            self._btn_refresh_cable.setToolTip(
                "Re-scan PipeWire/PulseAudio and PortAudio devices to detect a newly created cable."
            )
            self._lbl_cable_status = QLabel()
            self._lbl_cable_status.setStyleSheet("color: #888888; font-size: 11px;")
            if _vc_exists():
                self._btn_create_cable.setEnabled(False)
                self._lbl_cable_status.setText("Virtual cable already exists.")
            self._btn_create_cable.clicked.connect(self._on_create_virtual_cable)
            self._btn_refresh_cable.clicked.connect(self._on_refresh_virtual_cable)
            cable_btn_row = QHBoxLayout()
            cable_btn_row.addSpacing(94)
            cable_btn_row.addWidget(self._btn_create_cable)
            cable_btn_row.addWidget(self._btn_refresh_cable)
            cable_btn_row.addWidget(self._lbl_cable_status)
            cable_btn_row.addStretch()
            tts_out_layout.addLayout(cable_btn_row)
        else:
            self._btn_create_cable = None
            self._btn_refresh_cable = None
            self._lbl_cable_status = None

        self._tts_output_widget.setVisible(False)
        cfg_layout.addWidget(self._tts_output_widget)

        self._cmb_engine = _labeled_combo("Engine:", cfg_layout)
        self._cmb_engine.currentIndexChanged.connect(self._on_engine_changed)

        # ── Voice Engine row (hidden until TTS enabled) ────────────────
        self._voice_engine_row = QWidget()
        ve_row_layout = QHBoxLayout(self._voice_engine_row)
        ve_row_layout.setContentsMargins(0, 0, 0, 0)
        ve_lbl = QLabel("Voice Engine:")
        ve_lbl.setFixedWidth(90)
        ve_row_layout.addWidget(ve_lbl)
        self._cmb_voice_engine = QComboBox()
        self._cmb_voice_engine.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._cmb_voice_engine.setModel(self._build_voice_engine_model())
        self._cmb_voice_engine.currentIndexChanged.connect(self._on_voice_engine_changed)
        ve_row_layout.addWidget(self._cmb_voice_engine)
        self._voice_engine_row.setVisible(False)
        cfg_layout.addWidget(self._voice_engine_row)

        # ── ElevenLabs sub-panel (voice + model + refresh) ────────────
        self._el_panel = QWidget()
        el_layout = QVBoxLayout(self._el_panel)
        el_layout.setSpacing(6)
        el_layout.setContentsMargins(0, 0, 0, 0)

        # Voice row
        el_voice_row = QHBoxLayout()
        el_voice_lbl = QLabel("Voice:")
        el_voice_lbl.setFixedWidth(90)
        el_voice_row.addWidget(el_voice_lbl)
        self._cmb_el_voice = QComboBox()
        self._cmb_el_voice.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._cmb_el_voice.addItem("(Select Voice)", userData="")
        self._cmb_el_voice.currentIndexChanged.connect(self._on_el_voice_changed)
        el_voice_row.addWidget(self._cmb_el_voice)
        self._btn_el_refresh = QPushButton("Refresh")
        self._btn_el_refresh.setFixedWidth(65)
        self._btn_el_refresh.clicked.connect(self._on_el_refresh)
        el_voice_row.addWidget(self._btn_el_refresh)
        el_layout.addLayout(el_voice_row)

        # Model row
        el_model_row = QHBoxLayout()
        el_model_lbl = QLabel("Model:")
        el_model_lbl.setFixedWidth(90)
        el_model_row.addWidget(el_model_lbl)
        self._cmb_el_model = QComboBox()
        self._cmb_el_model.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._cmb_el_model.addItem("(Select Model)", userData="")
        self._cmb_el_model.currentIndexChanged.connect(self._on_el_model_changed)
        el_model_row.addWidget(self._cmb_el_model)
        el_layout.addLayout(el_model_row)

        self._el_panel.setVisible(False)
        self._el_refresh_thread: Optional[_ELRefreshThread] = None
        self._el_voices_cache: list[dict] = []   # full voice dicts including settings
        cfg_layout.addWidget(self._el_panel)

        # ── Amazon Polly sub-panel (voice + engine + refresh) ──────────
        self._polly_panel = QWidget()
        polly_layout = QVBoxLayout(self._polly_panel)
        polly_layout.setSpacing(6)
        polly_layout.setContentsMargins(0, 0, 0, 0)

        # Voice row
        polly_voice_row = QHBoxLayout()
        polly_voice_lbl = QLabel("Voice:")
        polly_voice_lbl.setFixedWidth(90)
        polly_voice_row.addWidget(polly_voice_lbl)
        self._cmb_polly_voice = QComboBox()
        self._cmb_polly_voice.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._cmb_polly_voice.addItem("(Select Voice)", userData="")
        self._cmb_polly_voice.currentIndexChanged.connect(self._on_polly_voice_changed)
        polly_voice_row.addWidget(self._cmb_polly_voice)
        self._btn_polly_refresh = QPushButton("Refresh")
        self._btn_polly_refresh.setFixedWidth(65)
        self._btn_polly_refresh.clicked.connect(self._on_polly_refresh)
        polly_voice_row.addWidget(self._btn_polly_refresh)
        polly_layout.addLayout(polly_voice_row)

        # Engine row
        polly_engine_row = QHBoxLayout()
        polly_engine_lbl = QLabel("Engine:")
        polly_engine_lbl.setFixedWidth(90)
        polly_engine_row.addWidget(polly_engine_lbl)
        self._cmb_polly_engine = QComboBox()
        self._cmb_polly_engine.addItem("Neural", userData="neural")
        self._cmb_polly_engine.addItem("Standard", userData="standard")
        self._cmb_polly_engine.currentIndexChanged.connect(self._on_polly_engine_changed)
        polly_engine_row.addWidget(self._cmb_polly_engine)
        polly_layout.addLayout(polly_engine_row)

        self._polly_panel.setVisible(False)
        cfg_layout.addWidget(self._polly_panel)

        # ── eSpeak sub-panel (voice name + speed + pitch) ──────────────
        self._espeak_panel = QWidget()
        esp_layout = QVBoxLayout(self._espeak_panel)
        esp_layout.setSpacing(4)
        esp_layout.setContentsMargins(0, 0, 0, 0)

        esp_voice_row = QHBoxLayout()
        esp_voice_lbl = QLabel("Voice:")
        esp_voice_lbl.setFixedWidth(90)
        esp_voice_row.addWidget(esp_voice_lbl)
        self._esp_voice = QLineEdit(self.settings.espeak_voice)
        self._esp_voice.setPlaceholderText("e.g. en, en-us, en+m3, en+f3")
        self._esp_voice.setToolTip(
            "eSpeak-NG voice name.\n"
            "en / en-us — standard English\n"
            "en+m1…m7  — male variants (different robots)\n"
            "en+f1…f4  — female variants\n"
            "en+croak   — gravelly male\n"
            "Run  espeak-ng --voices  to list all available voices."
        )
        self._esp_voice.editingFinished.connect(self._on_esp_voice_changed)
        esp_voice_row.addWidget(self._esp_voice)
        esp_layout.addLayout(esp_voice_row)

        esp_speed_row = QHBoxLayout()
        esp_speed_lbl = QLabel("Speed:")
        esp_speed_lbl.setFixedWidth(90)
        esp_speed_row.addWidget(esp_speed_lbl)
        self._esp_speed = QSlider(Qt.Orientation.Horizontal)
        self._esp_speed.setRange(80, 450)
        self._esp_speed.setValue(self.settings.espeak_speed)
        self._esp_speed.setToolTip("Speaking rate in words per minute (80–450).\nMoonbase Alpha default: ~175")
        self._esp_speed_lbl = QLabel(f"{self.settings.espeak_speed} wpm")
        self._esp_speed_lbl.setFixedWidth(55)
        self._esp_speed.valueChanged.connect(self._on_esp_speed_changed)
        esp_speed_row.addWidget(self._esp_speed)
        esp_speed_row.addWidget(self._esp_speed_lbl)
        esp_layout.addLayout(esp_speed_row)

        esp_pitch_row = QHBoxLayout()
        esp_pitch_lbl = QLabel("Pitch:")
        esp_pitch_lbl.setFixedWidth(90)
        esp_pitch_row.addWidget(esp_pitch_lbl)
        self._esp_pitch = QSlider(Qt.Orientation.Horizontal)
        self._esp_pitch.setRange(0, 99)
        self._esp_pitch.setValue(self.settings.espeak_pitch)
        self._esp_pitch.setToolTip("Voice pitch (0–99). Lower = deeper / more robotic.")
        self._esp_pitch_lbl = QLabel(str(self.settings.espeak_pitch))
        self._esp_pitch_lbl.setFixedWidth(55)
        self._esp_pitch.valueChanged.connect(self._on_esp_pitch_changed)
        esp_pitch_row.addWidget(self._esp_pitch)
        esp_pitch_row.addWidget(self._esp_pitch_lbl)
        esp_layout.addLayout(esp_pitch_row)

        self._espeak_panel.setVisible(False)
        cfg_layout.addWidget(self._espeak_panel)

        self._cmb_language = _labeled_combo("Language:", cfg_layout)
        for display, _ in LANGUAGES:
            self._cmb_language.addItem(display)

        # ── Input mode (all engines) ───────────────────────────────────
        mode_row = QHBoxLayout()
        mode_lbl = QLabel("Input mode:")
        mode_lbl.setFixedWidth(90)
        mode_row.addWidget(mode_lbl)

        self._radio_vad = QRadioButton("VAD")
        self._radio_ptt_hold = QRadioButton("PTT Hold")
        self._radio_ptt_toggle = QRadioButton("PTT Toggle")
        self._radio_vad.setChecked(True)

        try:
            import webrtcvad as _wrtcvad  # noqa: F401
            _webrtcvad_ok = True
        except Exception:
            _webrtcvad_ok = False
        if not _webrtcvad_ok:
            self._radio_vad.setEnabled(False)
            self._radio_vad.setToolTip(
                "webrtcvad is not installed or failed to import.\n"
                "Fix: pip install \"setuptools<81\" then pip install webrtcvad\n"
                "(setuptools>=81 removed pkg_resources which webrtcvad requires)"
            )
            if self.settings.whisper_input_mode == "vad":
                self._radio_ptt_hold.setChecked(True)
                self.settings.whisper_input_mode = "ptt_hold"

        self._btn_group_mode = QButtonGroup()
        self._btn_group_mode.addButton(self._radio_vad, 0)
        self._btn_group_mode.addButton(self._radio_ptt_hold, 1)
        self._btn_group_mode.addButton(self._radio_ptt_toggle, 2)
        self._btn_group_mode.idClicked.connect(self._on_input_mode_changed)

        mode_row.addWidget(self._radio_vad)
        mode_row.addWidget(self._radio_ptt_hold)
        mode_row.addWidget(self._radio_ptt_toggle)
        mode_row.addStretch()
        cfg_layout.addLayout(mode_row)

        # PTT key row (shown in either PTT mode)
        self._ptt_key_row = QWidget()
        ptt_key_layout = QHBoxLayout(self._ptt_key_row)
        ptt_key_layout.setContentsMargins(0, 0, 0, 0)
        ptt_key_lbl = QLabel("PTT key:")
        ptt_key_lbl.setFixedWidth(90)
        ptt_key_layout.addWidget(ptt_key_lbl)
        self._lbl_ptt_key = QLabel(_HotkeyCaptureDialog.fmt(self.settings.ptt_key))
        self._lbl_ptt_key.setFrameShape(QFrame.Shape.StyledPanel)
        self._lbl_ptt_key.setMinimumWidth(70)
        self._lbl_ptt_key.setStyleSheet("padding: 2px 6px;")
        ptt_key_layout.addWidget(self._lbl_ptt_key)
        btn_set_ptt = QPushButton("Set Key")
        btn_set_ptt.setFixedWidth(65)
        btn_set_ptt.clicked.connect(self._set_ptt_key)
        ptt_key_layout.addWidget(btn_set_ptt)
        ptt_key_layout.addStretch()
        cfg_layout.addWidget(self._ptt_key_row)

        # Live transcribe checkbox (all input modes)
        self._chk_live_transcribe = QCheckBox("Live transcribe")
        self._chk_live_transcribe.setChecked(self.settings.ptt_live_transcribe)
        self._chk_live_transcribe.setToolTip(
            "PTT: send each spoken segment to the VRChat chatbox as you speak, accumulating in one entry per press.\n"
            "VAD: send segments as they are recognized; the chatbox entry resets after 3 seconds of silence."
        )
        self._chk_live_transcribe.toggled.connect(self._on_live_transcribe_toggled)
        cfg_layout.addWidget(self._chk_live_transcribe)

        # TTS (Voice) toggle
        self._chk_tts = QCheckBox("TTS (Voice)")
        self._chk_tts.setChecked(self.settings.tts_enabled)
        self._chk_tts.toggled.connect(self._on_tts_toggled)
        cfg_layout.addWidget(self._chk_tts)

        # Initially hide PTT-specific rows (VAD mode default)
        self._ptt_key_row.setVisible(False)

        root.addLayout(cfg_layout)

        # ── Engine panels (each hidden unless their engine is selected) ──
        self._whisper_panel = self._build_whisper_panel()
        self._whisper_panel.setVisible(False)
        root.addWidget(self._whisper_panel)

        self._azure_panel = self._build_azure_panel()
        self._azure_panel.setVisible(False)
        root.addWidget(self._azure_panel)

        self._vosk_panel = self._build_vosk_panel()
        self._vosk_panel.setVisible(False)
        root.addWidget(self._vosk_panel)

        root.addWidget(_h_line())

        # ── Transcript area ────────────────────────────────────────────
        transcript_lbl = QLabel("Transcript")
        transcript_lbl.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        root.addWidget(transcript_lbl)

        self._transcript = QPlainTextEdit()
        self._transcript.setReadOnly(True)
        self._transcript.setMinimumHeight(140)
        self._transcript.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._transcript.setPlaceholderText("Recognized speech will appear here…")
        root.addWidget(self._transcript)

        # ── Manual text input ──────────────────────────────────────────
        manual_row = QHBoxLayout()
        self._manual_input = QLineEdit()
        self._manual_input.setPlaceholderText("Type to send…")
        self._manual_input.returnPressed.connect(self._on_manual_send)
        manual_row.addWidget(self._manual_input)
        self._btn_manual_send = QPushButton("Send")
        self._btn_manual_send.setFixedWidth(55)
        self._btn_manual_send.clicked.connect(self._on_manual_send)
        manual_row.addWidget(self._btn_manual_send)
        root.addLayout(manual_row)

        # ── ElevenLabs voice settings ──────────────────────────────────
        self._el_voice_settings_panel = self._build_el_voice_settings_panel()
        self._el_voice_settings_panel.setVisible(False)
        root.addWidget(self._el_voice_settings_panel)

        root.addWidget(_h_line())

        # ── OSC status row ─────────────────────────────────────────────
        osc_row = QHBoxLayout()
        self._lbl_osc = QLabel(f"OSC  {self.settings.osc_address}:{self.settings.osc_port}")
        self._lbl_osc.setStyleSheet("color: #888888; font-size: 11px;")
        osc_row.addWidget(self._lbl_osc)
        osc_row.addStretch()
        self._chk_chatbox = QCheckBox("Chatbox")
        self._chk_chatbox.setChecked(self.settings.use_chatbox)
        self._chk_chatbox.toggled.connect(self._on_chatbox_toggled)
        osc_row.addWidget(self._chk_chatbox)
        root.addLayout(osc_row)

        # ── Status + buttons ──────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._lbl_status = QLabel("● Idle")
        self._lbl_status.setStyleSheet("color: #888888;")
        btn_row.addWidget(self._lbl_status)
        btn_row.addStretch()

        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setFixedWidth(70)
        self._btn_clear.clicked.connect(self._transcript.clear)
        btn_row.addWidget(self._btn_clear)

        self._btn_toggle = QPushButton("Start Listening")
        self._btn_toggle.setFixedWidth(140)
        self._btn_toggle.setCheckable(True)
        self._btn_toggle.clicked.connect(self._toggle_listening)
        btn_row.addWidget(self._btn_toggle)

        root.addLayout(btn_row)

        # Status bar
        self.setStatusBar(QStatusBar())

    def _build_whisper_panel(self) -> QGroupBox:
        """Whisper-only panel — just the Launch / Close button."""
        box = QGroupBox("Whisper")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 6, 8, 6)

        self._btn_launch_whisper = QPushButton("Launch Whisper")
        self._btn_launch_whisper.clicked.connect(self._on_launch_whisper_clicked)
        layout.addWidget(self._btn_launch_whisper)

        return box

    def _build_azure_panel(self) -> QGroupBox:
        """Azure panel — Validate button + status label."""
        box = QGroupBox("Azure Speech")
        layout = QHBoxLayout(box)
        layout.setSpacing(8)
        layout.setContentsMargins(8, 6, 8, 6)

        self._btn_validate_azure = QPushButton("Validate")
        self._btn_validate_azure.setFixedWidth(80)
        self._btn_validate_azure.clicked.connect(self._on_validate_azure_clicked)
        layout.addWidget(self._btn_validate_azure)

        self._lbl_azure_status = QLabel("Not validated")
        self._lbl_azure_status.setStyleSheet("color: #888888;")
        layout.addWidget(self._lbl_azure_status)
        layout.addStretch()

        return box

    def _build_vosk_panel(self) -> QGroupBox:
        """Vosk panel — Launch / Close button."""
        box = QGroupBox("Vosk")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 6, 8, 6)

        self._btn_launch_vosk = QPushButton("Launch Vosk")
        self._btn_launch_vosk.clicked.connect(self._on_launch_vosk_clicked)
        layout.addWidget(self._btn_launch_vosk)

        return box

    def _build_el_voice_settings_panel(self) -> QWidget:
        """Sliders for ElevenLabs voice settings — shown below the transcript when EL is active."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(4)

        def _make_slider(label: str, tooltip: str, init_val: float) -> tuple[QLabel, QSlider, QLabel]:
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setFixedWidth(120)
            lbl.setToolTip(tooltip)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(int(round(init_val * 100)))
            slider.setToolTip(tooltip)
            val_lbl = QLabel(f"{init_val:.2f}")
            val_lbl.setFixedWidth(32)
            row.addWidget(lbl)
            row.addWidget(slider)
            row.addWidget(val_lbl)
            layout.addLayout(row)
            return lbl, slider, val_lbl

        s = self.settings
        _, self._sld_stability, self._lbl_stability_val = _make_slider(
            "Stability",
            "Controls how consistent the voice is (0.0–1.0)",
            s.elevenlabs_stability,
        )
        _, self._sld_similarity, self._lbl_similarity_val = _make_slider(
            "Similarity Boost",
            "Controls how strongly the output sticks to the original voice characteristics. (0.0–1.0)",
            s.elevenlabs_similarity_boost,
        )
        _, self._sld_style, self._lbl_style_val = _make_slider(
            "Style",
            "Controls how strongly stylistic elements are applied. (0.0–1.0)",
            s.elevenlabs_style,
        )

        boost_row = QHBoxLayout()
        self._chk_speaker_boost = QCheckBox("Use Speaker Boost")
        self._chk_speaker_boost.setChecked(s.elevenlabs_use_speaker_boost)
        self._chk_speaker_boost.setToolTip("Enhances clarity and presence. (true/false)")
        boost_row.addWidget(self._chk_speaker_boost)
        boost_row.addStretch()
        layout.addLayout(boost_row)

        # Wire signals
        def _on_stability(v: int) -> None:
            fv = v / 100.0
            self._lbl_stability_val.setText(f"{fv:.2f}")
            self.settings.elevenlabs_stability = fv
            self._schedule_save()

        def _on_similarity(v: int) -> None:
            fv = v / 100.0
            self._lbl_similarity_val.setText(f"{fv:.2f}")
            self.settings.elevenlabs_similarity_boost = fv
            self._schedule_save()

        def _on_style(v: int) -> None:
            fv = v / 100.0
            self._lbl_style_val.setText(f"{fv:.2f}")
            self.settings.elevenlabs_style = fv
            self._schedule_save()

        def _on_boost(checked: bool) -> None:
            self.settings.elevenlabs_use_speaker_boost = checked
            self._schedule_save()

        self._sld_stability.valueChanged.connect(_on_stability)
        self._sld_similarity.valueChanged.connect(_on_similarity)
        self._sld_style.valueChanged.connect(_on_style)
        self._chk_speaker_boost.toggled.connect(_on_boost)

        return panel

    # ------------------------------------------------------------------ Helpers

    def _restore_combo_selection(self, combo: QComboBox, target_text: str, default_index: int = 0) -> None:
        target = target_text.strip()
        if not target:
            combo.setCurrentIndex(default_index)
            return
        target_lower = target.lower()
        for i in range(combo.count()):
            text = combo.itemText(i)
            if text.lower() == target_lower or target_lower in text.lower():
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(default_index)

    def _populate_all_devices(self, *, preserve_selection: bool = True) -> None:
        selected_input = self.settings.input_device if preserve_selection else ""
        selected_headphones = self.settings.tts_headphones_device if preserve_selection else ""
        selected_cable = self.settings.tts_cable_device if preserve_selection else ""

        invalidate_device_cache()
        in_devices, out_devices = enumerate_all_devices()
        self._devices = in_devices

        for combo in (self._cmb_device, self._cmb_headphones, self._cmb_cable):
            combo.blockSignals(True)

        self._cmb_device.clear()
        self._cmb_device.addItem("System Default", userData=None)
        for dev in in_devices:
            self._cmb_device.addItem(dev.name, userData=dev.index)

        for cmb in (self._cmb_headphones, self._cmb_cable):
            cmb.clear()
            cmb.addItem("None", userData=None)
            for dev in out_devices:
                cmb.addItem(dev.name, userData=dev.index)

        cable_visible = any(_VC_CABLE_NAME.lower() in dev.name.lower() for dev in out_devices)
        if _vc_supported() and _vc_exists() and not cable_visible:
            # userData=None: cable exists in PipeWire but PortAudio hasn't registered it yet.
            # TTS routing checks currentData() is not None, so audio won't route until
            # PortAudio picks up the device and the dropdown is re-populated with a real index.
            self._cmb_cable.addItem(_VC_CABLE_NAME, userData=None)

        self._restore_combo_selection(self._cmb_device, selected_input, default_index=0)
        self._restore_combo_selection(self._cmb_headphones, selected_headphones, default_index=0)
        self._restore_combo_selection(self._cmb_cable, selected_cable, default_index=0)

        for combo in (self._cmb_device, self._cmb_headphones, self._cmb_cable):
            combo.blockSignals(False)

    def _build_voice_engine_model(self) -> QStandardItemModel:
        """Build a QStandardItemModel for the voice engine combo, greying ElevenLabs if no key."""
        model = QStandardItemModel()
        has_el_key = bool(self.settings.elevenlabs_api_key.strip())
        for code, label in zip(TTS_ENGINE_CODES, TTS_ENGINE_LABELS):
            if code == "elevenlabs" and not has_el_key:
                item = QStandardItem("ElevenLabs (api key needed)")
                item.setEnabled(False)
                item.setForeground(QColor("#555555"))
            elif code == "polly":
                from ..tts.polly_tts import is_available as _polly_available, has_credentials as _polly_creds
                if not _polly_available():
                    item = QStandardItem("Amazon Polly (boto3 not installed)")
                    item.setEnabled(False)
                    item.setForeground(QColor("#555555"))
                elif not _polly_creds(self.settings.polly_access_key_id, self.settings.polly_secret_access_key, self.settings.polly_region):
                    item = QStandardItem("Amazon Polly (region needed)")
                    item.setEnabled(False)
                    item.setForeground(QColor("#555555"))
                else:
                    item = QStandardItem(label)
            elif code == "espeak":
                from ..tts.espeak_tts import is_available as _esp_available
                if not _esp_available():
                    item = QStandardItem("eSpeak (not installed)")
                    item.setEnabled(False)
                    item.setForeground(QColor("#555555"))
                else:
                    item = QStandardItem(label)
            else:
                item = QStandardItem(label)
            model.appendRow(item)
        return model

    def _refresh_voice_engine_combo(self) -> None:
        """Rebuild the voice engine combo model (call after API key changes)."""
        # Always restore from settings — _on_voice_engine_changed keeps it in sync,
        # so self.settings.tts_voice_engine is always the correct source of truth.
        self._cmb_voice_engine.currentIndexChanged.disconnect(self._on_voice_engine_changed)
        self._cmb_voice_engine.setModel(self._build_voice_engine_model())
        for i, code in enumerate(TTS_ENGINE_CODES):
            if code == self.settings.tts_voice_engine:
                self._cmb_voice_engine.setCurrentIndex(i)
                break
        self._cmb_voice_engine.currentIndexChanged.connect(self._on_voice_engine_changed)

    def _engine_label_and_available(self, code: str) -> tuple[str, bool]:
        """Return (display label, is_available) for an engine code."""
        if code == "whisper":
            from ..stt.whisper_models import is_model_cached
            if is_model_cached(self.settings.whisper_model):
                return f"Whisper ({self.settings.whisper_model})", True
            return "Whisper (no model)", False
        elif code == "azure":
            if self.settings.azure_key.strip() and self.settings.azure_region.strip():
                return "Azure Speech", True
            return "Azure Speech (api key needed)", False
        elif code == "vosk":
            from ..stt.vosk_models import is_model_cached as _vosk_cached, MODELS as _VOSK_MODELS
            if any(_vosk_cached(m.key) for m in _VOSK_MODELS):
                return "Vosk", True
            return "Vosk (no model downloaded)", False
        elif code == "system":
            return "System Speech", True
        return code, True

    def _refresh_engine_combo(self) -> None:
        """Rebuild the engine combo box labels and availability state."""
        current_code = ENGINE_CODES[self._cmb_engine.currentIndex()] \
            if 0 <= self._cmb_engine.currentIndex() < len(ENGINE_CODES) \
            else self.settings.stt_engine

        self._cmb_engine.currentIndexChanged.disconnect(self._on_engine_changed)

        model = QStandardItemModel()
        for code in ENGINE_CODES:
            label, available = self._engine_label_and_available(code)
            item = QStandardItem(label)
            if not available:
                item.setEnabled(False)
                item.setForeground(QColor("#555555"))
            model.appendRow(item)
        self._cmb_engine.setModel(model)

        for i, code in enumerate(ENGINE_CODES):
            if code == current_code:
                self._cmb_engine.setCurrentIndex(i)
                break

        self._cmb_engine.currentIndexChanged.connect(self._on_engine_changed)

    def _apply_settings(self) -> None:
        self._refresh_engine_combo()

        # Select saved engine
        for i, code in enumerate(ENGINE_CODES):
            if code == self.settings.stt_engine:
                self._cmb_engine.setCurrentIndex(i)
                break

        # Force panel visibility — setCurrentIndex is a no-op when the index
        # hasn't changed (Whisper is index 0), so the signal never fires.
        engine = self.settings.stt_engine
        self._whisper_panel.setVisible(engine == "whisper")
        self._azure_panel.setVisible(engine == "azure")
        self._vosk_panel.setVisible(engine == "vosk")
        if engine == "whisper":
            self._update_launch_btn()
        elif engine == "vosk":
            self._update_vosk_btn()

        # Input mode radios
        mode = self.settings.whisper_input_mode
        is_ptt = mode != "vad"
        if mode == "ptt_hold":
            self._radio_ptt_hold.setChecked(True)
        elif mode == "ptt_toggle":
            self._radio_ptt_toggle.setChecked(True)
        else:
            self._radio_vad.setChecked(True)
        self._ptt_key_row.setVisible(is_ptt)
        self._chk_live_transcribe.setChecked(self.settings.ptt_live_transcribe)

        # PTT key label
        self._lbl_ptt_key.setText(_HotkeyCaptureDialog.fmt(self.settings.ptt_key))

        # Select saved language
        for i, (_, code) in enumerate(LANGUAGES):
            if code == self.settings.whisper_language:
                self._cmb_language.setCurrentIndex(i)
                break

        # Select saved device
        if self.settings.input_device:
            for i in range(self._cmb_device.count()):
                if self.settings.input_device in self._cmb_device.itemText(i):
                    self._cmb_device.setCurrentIndex(i)
                    break

        # TTS settings
        self._update_tts_availability()
        self._chk_tts.setChecked(self.settings.tts_enabled)
        self._tts_output_widget.setVisible(self.settings.tts_enabled)
        self._voice_engine_row.setVisible(self.settings.tts_enabled)
        self._refresh_voice_engine_combo()
        if self.settings.tts_headphones_device:
            for i in range(self._cmb_headphones.count()):
                if self.settings.tts_headphones_device in self._cmb_headphones.itemText(i):
                    self._cmb_headphones.setCurrentIndex(i)
                    break
        if self.settings.tts_cable_device:
            for i in range(self._cmb_cable.count()):
                if self.settings.tts_cable_device in self._cmb_cable.itemText(i):
                    self._cmb_cable.setCurrentIndex(i)
                    break

        # ElevenLabs — load cached voices/models (no API call on startup)
        from ..tts.elevenlabs_tts import load_cache
        el_voices, el_models = load_cache()
        if el_voices or el_models:
            self._populate_el_combos(el_voices, el_models)
        is_el = self.settings.tts_voice_engine == "elevenlabs"
        el_active = self.settings.tts_enabled and is_el
        self._el_panel.setVisible(el_active)
        voice_selected = bool(self.settings.elevenlabs_voice_id)
        self._el_voice_settings_panel.setVisible(el_active and voice_selected)
        is_polly = self.settings.tts_voice_engine == "polly"
        self._polly_panel.setVisible(self.settings.tts_enabled and is_polly)
        # Sync Polly controls to saved settings
        self._sync_polly_panel()
        is_esp = self.settings.tts_voice_engine == "espeak"
        self._espeak_panel.setVisible(self.settings.tts_enabled and is_esp)
        # Sync eSpeak controls to saved settings
        self._esp_voice.setText(self.settings.espeak_voice)
        self._esp_speed.setValue(self.settings.espeak_speed)
        self._esp_pitch.setValue(self.settings.espeak_pitch)

    def _current_language_code(self) -> str:
        idx = self._cmb_language.currentIndex()
        if 0 <= idx < len(LANGUAGES):
            return LANGUAGES[idx][1]
        return "en"

    def _current_engine_code(self) -> str:
        idx = self._cmb_engine.currentIndex()
        if 0 <= idx < len(ENGINE_CODES):
            return ENGINE_CODES[idx]
        return "whisper"

    def _current_device_index(self) -> Optional[int]:
        return self._cmb_device.currentData()

    def _build_engine(self) -> STTEngine:
        code = self._current_engine_code()
        if code == "whisper":
            if self._whisper_engine is None or not self._whisper_engine.is_model_loaded:
                raise RuntimeError(
                    "Whisper model is not loaded.\n"
                    "Click 'Launch Whisper' in the Whisper panel to load the model first."
                )
            # Sync settings that may have changed since the engine was created
            self._whisper_engine.input_mode = self.settings.whisper_input_mode
            return self._whisper_engine
        elif code == "azure":
            if not self._azure_validated:
                raise RuntimeError(
                    "Azure credentials are not validated.\n"
                    "Click 'Validate' in the Azure panel before starting."
                )
            from ..stt.azure_stt import AzureSTT
            return AzureSTT(
                subscription_key=self.settings.azure_key,
                region=self.settings.azure_region,
            )
        elif code == "vosk":
            if self._vosk_engine is None or not self._vosk_engine.is_model_loaded:
                raise RuntimeError(
                    "Vosk model is not loaded.\n"
                    "Click 'Launch Vosk' in the Vosk panel to load the model first."
                )
            return self._vosk_engine
        else:
            from ..stt.system_stt import SystemSTT
            return SystemSTT()

    def _update_launch_btn(self) -> None:
        if self._whisper_engine and self._whisper_engine.is_model_loaded:
            self._btn_launch_whisper.setText("Close Whisper")
        else:
            self._btn_launch_whisper.setText("Launch Whisper")
        self._btn_launch_whisper.setEnabled(True)

    def _listening_status(self) -> tuple[str, str]:
        """Return (status_text, color) appropriate for the current input mode."""
        mode = self.settings.whisper_input_mode
        if mode == "ptt_hold":
            return "● Ready (Hold PTT)", "#2196f3"
        elif mode == "ptt_toggle":
            return "● Ready (Toggle PTT)", "#2196f3"
        return "● Listening", "#4caf50"

    # ------------------------------------------------------------------ Whisper panel slots

    @pyqtSlot()
    def _on_launch_whisper_clicked(self) -> None:
        if self._whisper_engine and self._whisper_engine.is_model_loaded:
            self._close_whisper()
        else:
            self._launch_whisper()

    def _launch_whisper(self) -> None:
        from ..stt.whisper_stt import WhisperSTT

        wanted_model = self.settings.whisper_model
        if (self._whisper_engine is None
                or self._whisper_engine.model_size != wanted_model):
            if self._whisper_engine:
                self._whisper_engine.unload_model()
            self._whisper_engine = WhisperSTT(
                model_size=wanted_model,
                device=self.settings.whisper_device,
                device_index=self.settings.whisper_device_index,
                suppress_words=self.settings.whisper_suppress_words,
                vad_enabled=self.settings.vad_enabled,
                vad_aggressiveness=self.settings.vad_aggressiveness,
                silence_threshold_ms=self.settings.silence_threshold_ms,
                input_mode=self.settings.whisper_input_mode,
                max_record_seconds=self.settings.max_record_seconds,
                live_transcribe=self.settings.ptt_live_transcribe,
            )

        self._btn_launch_whisper.setText("Loading…")
        self._btn_launch_whisper.setEnabled(False)

        # Clean up any previous load thread
        if self._load_thread and self._load_thread.isRunning():
            self._load_thread.quit()
            self._load_thread.wait(2000)

        self._load_thread = _WhisperLoadThread(self._whisper_engine)
        self._load_thread.finished.connect(self._on_whisper_loaded)
        self._load_thread.error.connect(self._on_whisper_load_error)
        self._load_thread.start()

    @pyqtSlot()
    def _on_whisper_loaded(self) -> None:
        self._btn_launch_whisper.setText("Close Whisper")
        self._btn_launch_whisper.setEnabled(True)
        self._refresh_engine_combo()
        logger.info("Whisper model ready.")

    @pyqtSlot(str)
    def _on_whisper_load_error(self, msg: str) -> None:
        self._whisper_engine = None
        self._btn_launch_whisper.setText("Launch Whisper")
        self._btn_launch_whisper.setEnabled(True)
        self._show_error(f"Failed to load Whisper model:\n{msg}")

    def _close_whisper(self) -> None:
        if self._worker is not None:
            self._stop_listening(wait_for_thread=True)
        if self._whisper_engine:
            self._whisper_engine.unload_model()
            self._whisper_engine = None
        self._btn_launch_whisper.setText("Launch Whisper")
        self._btn_launch_whisper.setEnabled(True)

    # ------------------------------------------------------------------ Azure panel slots

    @pyqtSlot()
    def _on_validate_azure_clicked(self) -> None:
        key = self.settings.azure_key.strip()
        region = self.settings.azure_region.strip()
        if not key or not region:
            self._lbl_azure_status.setText("Enter API key and region in Settings first.")
            self._lbl_azure_status.setStyleSheet("color: #f44336;")
            return

        self._azure_validated = False
        self._btn_validate_azure.setText("Validating…")
        self._btn_validate_azure.setEnabled(False)
        self._lbl_azure_status.setText("Connecting…")
        self._lbl_azure_status.setStyleSheet("color: #888888;")

        if self._azure_validate_thread and self._azure_validate_thread.isRunning():
            self._azure_validate_thread.quit()
            self._azure_validate_thread.wait(2000)

        self._azure_validate_thread = _AzureValidateThread(key, region)
        self._azure_validate_thread.validated.connect(self._on_azure_validated)
        self._azure_validate_thread.failed.connect(self._on_azure_validate_failed)
        self._azure_validate_thread.start()

    @pyqtSlot()
    def _on_azure_validated(self) -> None:
        self._azure_validated = True
        self._btn_validate_azure.setText("Validate")
        self._btn_validate_azure.setEnabled(True)
        self._lbl_azure_status.setText("Ready to Use!")
        self._lbl_azure_status.setStyleSheet("color: #4caf50; font-weight: bold;")

    @pyqtSlot(str)
    def _on_azure_validate_failed(self, reason: str) -> None:
        self._azure_validated = False
        self._btn_validate_azure.setText("Validate")
        self._btn_validate_azure.setEnabled(True)
        self._lbl_azure_status.setText(reason)
        self._lbl_azure_status.setStyleSheet("color: #f44336; font-weight: bold;")

    # ------------------------------------------------------------------ Vosk panel slots

    @pyqtSlot()
    def _on_launch_vosk_clicked(self) -> None:
        if self._vosk_engine and self._vosk_engine.is_model_loaded:
            self._close_vosk()
        else:
            self._launch_vosk()

    def _launch_vosk(self) -> None:
        from ..stt.vosk_stt import VoskSTT
        from ..stt.vosk_models import get_model_path as _vosk_path, MODELS as _VOSK_MODELS

        model_path = next(
            (p for m in _VOSK_MODELS if (p := _vosk_path(m.key))),
            None,
        )
        if not model_path:
            self._show_error("No Vosk model is downloaded.\nGo to Settings → STT and download a model.")
            return

        if self._vosk_engine is None:
            self._vosk_engine = VoskSTT(model_path=model_path)

        self._btn_launch_vosk.setText("Loading…")
        self._btn_launch_vosk.setEnabled(False)

        if self._vosk_load_thread and self._vosk_load_thread.isRunning():
            self._vosk_load_thread.quit()
            self._vosk_load_thread.wait(2000)

        self._vosk_load_thread = _VoskLoadThread(self._vosk_engine)
        self._vosk_load_thread.finished.connect(self._on_vosk_loaded)
        self._vosk_load_thread.error.connect(self._on_vosk_load_error)
        self._vosk_load_thread.start()

    @pyqtSlot()
    def _on_vosk_loaded(self) -> None:
        self._btn_launch_vosk.setText("Close Vosk")
        self._btn_launch_vosk.setEnabled(True)
        logger.info("Vosk model ready.")

    @pyqtSlot(str)
    def _on_vosk_load_error(self, msg: str) -> None:
        self._vosk_engine = None
        self._btn_launch_vosk.setText("Launch Vosk")
        self._btn_launch_vosk.setEnabled(True)
        self._show_error(f"Failed to load Vosk model:\n{msg}")

    def _close_vosk(self) -> None:
        if self._worker is not None and self._current_engine_code() == "vosk":
            self._stop_listening()
        if self._vosk_engine:
            self._vosk_engine.unload_model()
            self._vosk_engine = None
        self._btn_launch_vosk.setText("Launch Vosk")
        self._btn_launch_vosk.setEnabled(True)

    def _update_vosk_btn(self) -> None:
        if self._vosk_engine and self._vosk_engine.is_model_loaded:
            self._btn_launch_vosk.setText("Close Vosk")
        else:
            self._btn_launch_vosk.setText("Launch Vosk")
        self._btn_launch_vosk.setEnabled(True)

    @pyqtSlot(int)
    def _on_input_mode_changed(self, btn_id: int) -> None:
        modes = {0: "vad", 1: "ptt_hold", 2: "ptt_toggle"}
        mode = modes.get(btn_id, "vad")
        self.settings.whisper_input_mode = mode
        self._schedule_save()
        is_ptt = mode != "vad"
        self._ptt_key_row.setVisible(is_ptt)
        self._update_tts_availability()
        self.adjustSize()
        if self._whisper_engine:
            self._whisper_engine.input_mode = mode

        # If a session is already active, sync PTT handler to the new mode
        if self._is_listening:
            self._ptt_active = False
            if is_ptt:
                self._start_ptt_handler()
            else:
                self._stop_ptt_handler()
            # Whisper's capture loop is chosen at session start (_loop_vad vs
            # _loop_ptt_standard). Restart the capture thread so it picks up
            # the new mode. Non-Whisper engines are gated in _on_result and
            # don't need a restart.
            if self._current_engine_code() == "whisper":
                self._restart_capture()

    @pyqtSlot(bool)
    def _on_live_transcribe_toggled(self, checked: bool) -> None:
        self.settings.ptt_live_transcribe = checked
        self._schedule_save()
        if self._whisper_engine:
            self._whisper_engine.live_transcribe = checked

    def _update_tts_availability(self) -> None:
        """Grey out and uncheck TTS (Voice) when VAD mode is active — PTT is required.

        settings.tts_enabled is never modified here so the preference is
        restored when the user switches back to a PTT mode.
        """
        is_ptt = self.settings.whisper_input_mode != "vad"
        self._chk_tts.setEnabled(is_ptt)
        # Update the checkbox visual state without triggering _on_tts_toggled
        self._chk_tts.blockSignals(True)
        if is_ptt:
            self._chk_tts.setChecked(self.settings.tts_enabled)
            self._chk_tts.setToolTip(
                "Speak transcribed text back through your selected audio outputs.\n"
                "Configure output devices and voice engine above."
            )
        else:
            self._chk_tts.setChecked(False)
            self._chk_tts.setToolTip(
                "TTS (Voice) requires PTT Hold or PTT Toggle mode.\n"
                "In VAD mode the app cannot tell when you have finished speaking,\n"
                "so there is no reliable moment to trigger voice output."
            )
        self._chk_tts.blockSignals(False)
        # Show or hide the TTS panels to match the effective state
        effective = is_ptt and self.settings.tts_enabled
        self._tts_output_widget.setVisible(effective)
        self._voice_engine_row.setVisible(effective)
        is_el = self.settings.tts_voice_engine == "elevenlabs"
        self._el_panel.setVisible(effective and is_el)
        voice_selected = bool(self.settings.elevenlabs_voice_id)
        self._el_voice_settings_panel.setVisible(effective and is_el and voice_selected)
        is_polly = self.settings.tts_voice_engine == "polly"
        self._polly_panel.setVisible(effective and is_polly)
        is_esp = self.settings.tts_voice_engine == "espeak"
        self._espeak_panel.setVisible(effective and is_esp)

    @pyqtSlot(bool)
    def _on_tts_toggled(self, checked: bool) -> None:
        self.settings.tts_enabled = checked
        self._schedule_save()
        self._tts_output_widget.setVisible(checked)
        self._voice_engine_row.setVisible(checked)
        is_el = self.settings.tts_voice_engine == "elevenlabs"
        self._el_panel.setVisible(checked and is_el)
        voice_selected = bool(self.settings.elevenlabs_voice_id)
        self._el_voice_settings_panel.setVisible(checked and is_el and voice_selected)
        is_polly = self.settings.tts_voice_engine == "polly"
        self._polly_panel.setVisible(checked and is_polly)
        is_esp = self.settings.tts_voice_engine == "espeak"
        self._espeak_panel.setVisible(checked and is_esp)
        self.adjustSize()

    @pyqtSlot(int)
    def _on_device_changed(self, index: int) -> None:
        text = self._cmb_device.itemText(index)
        self.settings.input_device = "" if text == "System Default" else text
        self._schedule_save()

    @pyqtSlot(int)
    def _on_headphones_changed(self, index: int) -> None:
        text = self._cmb_headphones.itemText(index)
        self.settings.tts_headphones_device = "" if text == "None" else text
        self._schedule_save()

    @pyqtSlot(int)
    def _on_cable_changed(self, index: int) -> None:
        text = self._cmb_cable.itemText(index)
        self.settings.tts_cable_device = "" if text == "None" else text
        self._schedule_save()

    @pyqtSlot()
    def _on_create_virtual_cable(self) -> None:
        from ..audio.linux_virtual_cable import create as _vc_create
        # Pa_Terminate() must not be called while a stream is open
        if self._worker is not None:
            self._stop_listening(wait_for_thread=True)
        self._btn_create_cable.setEnabled(False)
        try:
            _vc_create()
        except Exception as exc:
            self._btn_create_cable.setEnabled(True)
            QMessageBox.critical(self, "Virtual Cable Error", str(exc))
            return

        self._cable_refresh_attempt = 0
        if self._lbl_cable_status:
            self._lbl_cable_status.setText("Virtual cable created. Waiting for device list refresh...")
        QTimer.singleShot(300, self._refresh_after_cable_create)

    @pyqtSlot()
    def _on_refresh_virtual_cable(self) -> None:
        if self._worker is not None:
            self._stop_listening(wait_for_thread=True)
        self._cable_refresh_attempt = 0
        if self._lbl_cable_status:
            self._lbl_cable_status.setText("Refreshing audio devices...")
        QTimer.singleShot(0, self._refresh_after_cable_create)

    def _refresh_after_cable_create(self) -> None:
        # If capture thread is still running, retry shortly before touching PortAudio.
        if self._thread is not None and self._thread.isRunning():
            self._cable_refresh_attempt += 1
            if self._cable_refresh_attempt <= self._cable_refresh_max_attempts:
                if self._lbl_cable_status:
                    self._lbl_cable_status.setText("Waiting for microphone stream to stop...")
                QTimer.singleShot(300, self._refresh_after_cable_create)
            return

        self._cable_refresh_attempt += 1
        reinit_ok = reinitialize_portaudio(retries=3, settle_delay=0.2)
        self._populate_all_devices(preserve_selection=True)

        found_index = -1
        for i in range(self._cmb_cable.count()):
            if _VC_CABLE_NAME.lower() in self._cmb_cable.itemText(i).lower():
                found_index = i
                break

        if found_index >= 0:
            self._cmb_cable.setCurrentIndex(found_index)
            if self._btn_create_cable:
                self._btn_create_cable.setEnabled(False)
            if self._lbl_cable_status:
                selected_data = self._cmb_cable.currentData()
                if selected_data is None:
                    self._lbl_cable_status.setText(
                        "Cable exists in PipeWire but not yet routable. Audio will route once PortAudio detects it."
                    )
                else:
                    self._lbl_cable_status.setText("Virtual cable created and detected.")
            return

        if self._cable_refresh_attempt < self._cable_refresh_max_attempts:
            delay_ms = min(3000, 300 + self._cable_refresh_attempt * 250)
            if self._lbl_cable_status:
                if reinit_ok:
                    self._lbl_cable_status.setText("Waiting for PipeWire/PulseAudio to publish RawriisCable...")
                else:
                    self._lbl_cable_status.setText("PortAudio busy; retrying device refresh...")
            QTimer.singleShot(delay_ms, self._refresh_after_cable_create)
            return

        if _vc_exists():
            for i in range(self._cmb_cable.count()):
                if _VC_CABLE_NAME.lower() in self._cmb_cable.itemText(i).lower():
                    self._cmb_cable.setCurrentIndex(i)
                    break
            if self._lbl_cable_status:
                self._lbl_cable_status.setText("Virtual cable exists in PipeWire. Using name-based output until PortAudio catches up.")
            if self._btn_create_cable:
                self._btn_create_cable.setEnabled(False)
            return

        if self._lbl_cable_status:
            self._lbl_cable_status.setText("Cable refresh failed. Try creating or refreshing again.")
        if self._btn_create_cable:
            self._btn_create_cable.setEnabled(True)

    @pyqtSlot(int)
    def _on_voice_engine_changed(self, index: int) -> None:
        # Reject clicks on disabled (greyed-out) items
        model = self._cmb_voice_engine.model()
        item = model.item(index) if model else None
        if item and not item.isEnabled():
            prev_idx = next(
                (i for i, c in enumerate(TTS_ENGINE_CODES) if c == self.settings.tts_voice_engine), 0
            )
            self._cmb_voice_engine.blockSignals(True)
            self._cmb_voice_engine.setCurrentIndex(prev_idx)
            self._cmb_voice_engine.blockSignals(False)
            return
        if 0 <= index < len(TTS_ENGINE_CODES):
            self.settings.tts_voice_engine = TTS_ENGINE_CODES[index]
            self._schedule_save()
        is_el = self.settings.tts_voice_engine == "elevenlabs"
        el_active = self.settings.tts_enabled and is_el
        self._el_panel.setVisible(el_active)
        voice_selected = bool(self.settings.elevenlabs_voice_id)
        self._el_voice_settings_panel.setVisible(el_active and voice_selected)
        is_polly = self.settings.tts_voice_engine == "polly"
        self._polly_panel.setVisible(self.settings.tts_enabled and is_polly)
        is_esp = self.settings.tts_voice_engine == "espeak"
        self._espeak_panel.setVisible(self.settings.tts_enabled and is_esp)
        self.adjustSize()

    @pyqtSlot(int)
    def _on_el_voice_changed(self, index: int) -> None:
        voice_id = self._cmb_el_voice.itemData(index) or ""
        self.settings.elevenlabs_voice_id = voice_id
        # Apply this voice's default settings to the sliders
        if voice_id:
            voice_data = next((v for v in self._el_voices_cache if v.get("voice_id") == voice_id), None)
            if voice_data:
                vs = voice_data.get("settings", {})
                self._apply_el_voice_settings(vs)
        # Show/hide the settings panel based on whether a voice is selected
        is_el = self.settings.tts_voice_engine == "elevenlabs"
        el_active = self.settings.tts_enabled and is_el
        self._el_voice_settings_panel.setVisible(el_active and bool(voice_id))
        self._schedule_save()
        self.adjustSize()

    def _apply_el_voice_settings(self, vs: dict) -> None:
        """Push voice settings dict into the sliders/checkbox without saving (caller saves)."""
        stability = float(vs.get("stability", self.settings.elevenlabs_stability))
        similarity = float(vs.get("similarity_boost", self.settings.elevenlabs_similarity_boost))
        style = float(vs.get("style", self.settings.elevenlabs_style))
        boost = bool(vs.get("use_speaker_boost", self.settings.elevenlabs_use_speaker_boost))
        for slider, lbl, val in (
            (self._sld_stability, self._lbl_stability_val, stability),
            (self._sld_similarity, self._lbl_similarity_val, similarity),
            (self._sld_style, self._lbl_style_val, style),
        ):
            slider.blockSignals(True)
            slider.setValue(int(round(val * 100)))
            lbl.setText(f"{val:.2f}")
            slider.blockSignals(False)
        self._chk_speaker_boost.blockSignals(True)
        self._chk_speaker_boost.setChecked(boost)
        self._chk_speaker_boost.blockSignals(False)
        self.settings.elevenlabs_stability = stability
        self.settings.elevenlabs_similarity_boost = similarity
        self.settings.elevenlabs_style = style
        self.settings.elevenlabs_use_speaker_boost = boost

    @pyqtSlot(int)
    def _on_el_model_changed(self, index: int) -> None:
        model_id = self._cmb_el_model.itemData(index) or ""
        self.settings.elevenlabs_model_id = model_id
        self._schedule_save()

    # ------------------------------------------------------------------ eSpeak slots

    @pyqtSlot()
    def _on_esp_voice_changed(self) -> None:
        self.settings.espeak_voice = self._esp_voice.text().strip() or "en"
        self._schedule_save()

    @pyqtSlot(int)
    def _on_esp_speed_changed(self, value: int) -> None:
        self.settings.espeak_speed = value
        self._esp_speed_lbl.setText(f"{value} wpm")
        self._schedule_save()

    @pyqtSlot(int)
    def _on_esp_pitch_changed(self, value: int) -> None:
        self.settings.espeak_pitch = value
        self._esp_pitch_lbl.setText(str(value))
        self._schedule_save()

    # ── Amazon Polly handlers ──────────────────────────────────────────

    def _sync_polly_panel(self) -> None:
        """Sync Polly combo boxes to current settings (called on _apply_settings)."""
        # Engine
        engine_idx = 0 if self.settings.polly_engine == "neural" else 1
        self._cmb_polly_engine.blockSignals(True)
        self._cmb_polly_engine.setCurrentIndex(engine_idx)
        self._cmb_polly_engine.blockSignals(False)
        # Voice — restore saved selection if present in combo
        saved = self.settings.polly_voice_id
        if saved:
            for i in range(self._cmb_polly_voice.count()):
                if self._cmb_polly_voice.itemData(i) == saved:
                    self._cmb_polly_voice.blockSignals(True)
                    self._cmb_polly_voice.setCurrentIndex(i)
                    self._cmb_polly_voice.blockSignals(False)
                    break

    @pyqtSlot(int)
    def _on_polly_voice_changed(self, index: int) -> None:
        voice_id = self._cmb_polly_voice.itemData(index) or ""
        self.settings.polly_voice_id = voice_id
        self._schedule_save()

    @pyqtSlot(int)
    def _on_polly_engine_changed(self, index: int) -> None:
        engine = self._cmb_polly_engine.itemData(index) or "neural"
        self.settings.polly_engine = engine
        self._schedule_save()

    @pyqtSlot()
    def _on_polly_refresh(self) -> None:
        if not self.settings.polly_region:
            QMessageBox.warning(
                self, "Amazon Polly",
                "No region set.\nGo to Settings → Text-to-Speech and enter your AWS region."
            )
            return
        from ..tts.polly_tts import is_available as _polly_available
        if not _polly_available():
            QMessageBox.warning(
                self, "Amazon Polly",
                "boto3 is not installed.\nRun: pip install boto3"
            )
            return
        if self._polly_refresh_thread and self._polly_refresh_thread.isRunning():
            return
        self._btn_polly_refresh.setText("Loading…")
        self._btn_polly_refresh.setEnabled(False)
        self._polly_refresh_thread = _PollyRefreshThread(
            self.settings.polly_access_key_id,
            self.settings.polly_secret_access_key,
            self.settings.polly_region,
            self.settings.polly_engine,
        )
        self._polly_refresh_thread.finished.connect(self._on_polly_refresh_done)
        self._polly_refresh_thread.failed.connect(self._on_polly_refresh_failed)
        self._polly_refresh_thread.start()

    @pyqtSlot(list)
    def _on_polly_refresh_done(self, voices: list) -> None:
        self._btn_polly_refresh.setText("Refresh")
        self._btn_polly_refresh.setEnabled(True)
        self._cmb_polly_voice.blockSignals(True)
        self._cmb_polly_voice.clear()
        self._cmb_polly_voice.addItem("(Select Voice)", userData="")
        for v in voices:
            label = f"{v['name']} ({v['language_name']}, {v['gender']})"
            self._cmb_polly_voice.addItem(label, userData=v["voice_id"])
        # Restore saved selection
        saved = self.settings.polly_voice_id
        if saved:
            for i in range(self._cmb_polly_voice.count()):
                if self._cmb_polly_voice.itemData(i) == saved:
                    self._cmb_polly_voice.setCurrentIndex(i)
                    break
        self._cmb_polly_voice.blockSignals(False)

    @pyqtSlot(str)
    def _on_polly_refresh_failed(self, message: str) -> None:
        self._btn_polly_refresh.setText("Refresh")
        self._btn_polly_refresh.setEnabled(True)
        self._show_error(f"Amazon Polly refresh failed:\n{message}")

    @pyqtSlot()
    def _on_el_refresh(self) -> None:
        if not self.settings.elevenlabs_api_key:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "ElevenLabs",
                "No API key set.\nGo to Settings → Text-to-Speech and paste your ElevenLabs API key."
            )
            return
        if self._el_refresh_thread and self._el_refresh_thread.isRunning():
            return
        self._btn_el_refresh.setText("Loading…")
        self._btn_el_refresh.setEnabled(False)
        self._el_refresh_thread = _ELRefreshThread(self.settings.elevenlabs_api_key)
        self._el_refresh_thread.finished.connect(self._on_el_refresh_done)
        self._el_refresh_thread.failed.connect(self._on_el_refresh_failed)
        self._el_refresh_thread.start()

    @pyqtSlot(list, list)
    def _on_el_refresh_done(self, voices: list, models: list) -> None:
        from ..tts.elevenlabs_tts import save_cache
        save_cache(voices, models)
        self._populate_el_combos(voices, models)
        self._btn_el_refresh.setText("Refresh")
        self._btn_el_refresh.setEnabled(True)

    @pyqtSlot(str)
    def _on_el_refresh_failed(self, message: str) -> None:
        self._btn_el_refresh.setText("Refresh")
        self._btn_el_refresh.setEnabled(True)
        self._show_error(f"ElevenLabs refresh failed:\n{message}")

    def _populate_el_combos(self, voices: list[dict], models: list[dict]) -> None:
        """Populate ElevenLabs voice and model combos from cached data."""
        self._el_voices_cache = voices  # keep full data for settings lookup

        self._cmb_el_voice.blockSignals(True)
        self._cmb_el_voice.clear()
        self._cmb_el_voice.addItem("(Select Voice)", userData="")
        for v in voices:
            self._cmb_el_voice.addItem(v["name"], userData=v["voice_id"])
        # Restore saved selection and apply that voice's default settings
        saved_vid = self.settings.elevenlabs_voice_id
        if saved_vid:
            for i in range(self._cmb_el_voice.count()):
                if self._cmb_el_voice.itemData(i) == saved_vid:
                    self._cmb_el_voice.setCurrentIndex(i)
                    voice_data = next((v for v in voices if v.get("voice_id") == saved_vid), None)
                    if voice_data:
                        self._apply_el_voice_settings(voice_data.get("settings", {}))
                    break
        self._cmb_el_voice.blockSignals(False)

        self._cmb_el_model.blockSignals(True)
        self._cmb_el_model.clear()
        self._cmb_el_model.addItem("(Select Model)", userData="")
        for m in models:
            self._cmb_el_model.addItem(m["name"], userData=m["model_id"])
        # Restore saved selection
        saved_mid = self.settings.elevenlabs_model_id
        if saved_mid:
            for i in range(self._cmb_el_model.count()):
                if self._cmb_el_model.itemData(i) == saved_mid:
                    self._cmb_el_model.setCurrentIndex(i)
                    break
        self._cmb_el_model.blockSignals(False)

    def _set_ptt_key(self) -> None:
        dlg = _HotkeyCaptureDialog(current_key=self.settings.ptt_key, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.captured_key():
            self.settings.ptt_key = dlg.captured_key()
            self._lbl_ptt_key.setText(_HotkeyCaptureDialog.fmt(self.settings.ptt_key))
            self._schedule_save()

    # ------------------------------------------------------------------ PTT handler

    def _start_ptt_handler(self) -> None:
        from ..stt.ptt_handler import PTTHandler
        if self._ptt_handler:
            self._ptt_handler.stop()

        mode = self.settings.whisper_input_mode
        engine = self._whisper_engine
        sounds = self._sound_player

        if mode == "ptt_hold":
            def on_press():
                if not self._is_listening:
                    return
                if self.settings.whisper_input_mode != "ptt_hold":
                    return  # stale hook from a previous mode — ignore
                self._live_commit_timer.stop()
                self._live_accumulated = ""
                self._ptt_active = True
                self._ptt_active_since = time.monotonic()
                if engine:
                    engine.ptt_press()
                sounds.play_start()

            def on_release():
                if not self._is_listening:
                    return
                if self.settings.whisper_input_mode != "ptt_hold":
                    return  # stale hook from a previous mode — ignore
                self._ptt_active = False
                if engine:
                    engine.ptt_release()
                sounds.play_stop()

            self._ptt_handler = PTTHandler(
                key=self.settings.ptt_key,
                mode="ptt_hold",
                on_press=on_press,
                on_release=on_release,
            )
        else:  # ptt_toggle
            def on_press():
                if not self._is_listening:
                    return
                if self.settings.whisper_input_mode != "ptt_toggle":
                    return  # stale hook from a previous mode — ignore
                self._ptt_active = not self._ptt_active
                if self._ptt_active:
                    self._live_commit_timer.stop()
                    self._live_accumulated = ""
                    self._ptt_active_since = time.monotonic()
                    if engine:
                        engine.ptt_press()
                    sounds.play_start()
                else:
                    if engine:
                        engine.ptt_release()
                    sounds.play_stop()

            self._ptt_handler = PTTHandler(
                key=self.settings.ptt_key,
                mode="ptt_toggle",
                on_press=on_press,
            )

        self._ptt_handler.start()

    def _stop_ptt_handler(self) -> None:
        if self._ptt_handler:
            self._ptt_handler.stop()
            self._ptt_handler = None

    @pyqtSlot()
    def _do_ptt_press(self) -> None:
        """Start PTT recording — called in the GUI thread (e.g. from a SteamVR signal)."""
        if not self._is_listening:
            return
        self._live_commit_timer.stop()
        self._live_accumulated = ""
        self._ptt_active = True
        self._ptt_active_since = time.monotonic()
        if self._whisper_engine:
            self._whisper_engine.ptt_press()
        if self.settings.ptt_sound_enabled:
            self._sound_player.play_start()

    @pyqtSlot()
    def _do_ptt_release(self) -> None:
        """Stop PTT recording — called in the GUI thread (e.g. from a SteamVR signal)."""
        if not self._is_listening:
            return
        self._ptt_active = False
        if self._whisper_engine:
            self._whisper_engine.ptt_release()
        if self.settings.ptt_sound_enabled:
            self._sound_player.play_stop()

    def _finalize_stopped_thread(self, thread: QThread, worker: Optional[_STTWorker]) -> None:
        entry = (thread, worker)
        if entry in self._stopping_threads:
            self._stopping_threads.remove(entry)
        thread.deleteLater()
        if worker:
            worker.deleteLater()

    def _restart_capture(self) -> None:
        """Stop and restart the audio capture thread without touching UI state.

        Used when the input mode changes mid-session (e.g. VAD → PTT Hold) so
        that Whisper's capture loop re-evaluates which loop function to run.
        """
        if self._worker:
            self._worker.stop()
        if self._thread:
            self._thread.quit()
            self._thread.wait(3000)
        self._worker = None
        self._thread = None

        try:
            self._engine = self._build_engine()
        except Exception as exc:
            self._show_error(str(exc))
            self._is_listening = False
            self._btn_toggle.setText("Start Listening")
            self._btn_toggle.setChecked(False)
            self._set_status("● Idle", "#888888")
            return

        device_index = self._current_device_index()
        language = self._current_language_code()
        self._worker = _STTWorker(self._engine, device_index, language)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.result_ready.connect(self._on_result)
        self._worker.error_occurred.connect(self._on_stt_error)
        self._thread.start()
        status_text, status_color = self._listening_status()
        self._set_status(status_text, status_color)

    # ------------------------------------------------------------------ Slots

    @pyqtSlot()
    def _toggle_listening(self) -> None:
        if self._btn_toggle.isChecked():
            self._start_listening()
        else:
            self._stop_listening()

    def _start_listening(self) -> None:
        try:
            self._engine = self._build_engine()
        except Exception as exc:
            self._show_error(str(exc))
            self._btn_toggle.setChecked(False)
            return

        device_index = self._current_device_index()
        language = self._current_language_code()

        # Guard: on Linux (including WSL), sounddevice may have no default device.
        # Attempting to open the stream returns PortAudioError: Error querying device -1.
        if device_index is None:
            import sys as _sys
            if _sys.platform == "linux":
                import sounddevice as _sd
                if _sd.default.device[0] < 0:
                    _is_wsl = False
                    try:
                        with open("/proc/version") as _f:
                            _is_wsl = "microsoft" in _f.read().lower()
                    except OSError:
                        pass
                    if _is_wsl:
                        _msg = (
                            "No audio input device was found.\n\n"
                            "WSL2 on Windows 11 provides audio via WSLg automatically — "
                            "open a fresh WSL terminal and try again.\n\n"
                            "If you installed standalone pulseaudio, remove it:\n"
                            "  sudo apt remove --purge pulseaudio\n\n"
                            "See docs/install-linux.md for full WSL audio troubleshooting."
                        )
                    else:
                        _msg = (
                            "No audio input device was found.\n\n"
                            "Check that a microphone is connected and recognised "
                            "by your system (try: pactl info)."
                        )
                    QMessageBox.critical(self, "No Audio Device", _msg)
                    self._btn_toggle.setChecked(False)
                    return

        # Start PTT handler for all engines when in a PTT mode
        if self.settings.whisper_input_mode in ("ptt_hold", "ptt_toggle"):
            self._ptt_active = False
            self._start_ptt_handler()

        self._worker = _STTWorker(self._engine, device_index, language)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.result_ready.connect(self._on_result)
        self._worker.error_occurred.connect(self._on_stt_error)

        self._thread.start()
        self._is_listening = True
        self._btn_toggle.setText("Stop Listening")
        status_text, status_color = self._listening_status()
        self._set_status(status_text, status_color)
        if self.settings.send_osc:
            self._osc.send_listening(True)

    def _stop_listening(self, *, wait_for_thread: bool = False, timeout_ms: int = 3000) -> None:
        self._is_listening = False
        self._ptt_active = False
        self._live_commit_timer.stop()
        self._live_accumulated = ""
        self._stop_ptt_handler()
        # Update UI immediately — don't block the event loop waiting for the thread
        self._btn_toggle.setText("Start Listening")
        self._btn_toggle.setChecked(False)
        self._set_status("● Idle", "#888888")
        if self.settings.send_osc:
            self._osc.send_listening(False)

        worker, thread = self._worker, self._thread
        self._worker = None
        self._thread = None
        self._engine = None

        if worker:
            worker.stop()  # sets stop event + sentinel; returns immediately
        if thread:
            self._stopping_threads.append((thread, worker))
            thread.finished.connect(
                lambda t=thread, w=worker: self._finalize_stopped_thread(t, w)
            )
            thread.quit()
            if wait_for_thread:
                thread.wait(timeout_ms)
                if not thread.isRunning():
                    self._finalize_stopped_thread(thread, worker)

    def _on_manual_send(self) -> None:
        text = self._manual_input.text().strip()
        if not text:
            logger.debug("Manual send: empty input, ignoring.")
            return
        logger.debug("Manual send: %r", text)
        self._manual_input.clear()
        self._on_result(text, is_final=True, manual=True)

    @pyqtSlot(str, bool)
    def _on_result(self, text: str, is_final: bool, manual: bool = False) -> None:
        mode = self.settings.whisper_input_mode
        # Non-Whisper engines produce results continuously; gate them on PTT state.
        # (Whisper already handles PTT gating internally.)
        # manual=True bypasses this gate — typed sends are never voice-gated.
        if not manual and mode != "vad" and self._current_engine_code() != "whisper":
            if not self._ptt_active:
                return
            # Discard results that were recognized before PTT was pressed —
            # they arrive in the signal queue slightly after activation.
            if time.monotonic() - self._ptt_active_since < 0.3:
                return

        if is_final:
            self._transcript.appendPlainText(text)
            self._last_transcription = text

            if self.settings.send_osc and self.settings.use_chatbox:
                if self.settings.ptt_live_transcribe:
                    segment = text.strip()
                    if segment:
                        if self._live_accumulated:
                            self._live_accumulated += " " + segment
                        else:
                            self._live_accumulated = segment
                        # Interim send — always immediate, no notification (mid-utterance preview)
                        self._osc.send_chatbox(
                            self._live_accumulated,
                            send_immediately=True,
                            play_notification=False,
                        )
                        # Restart quiet timer; commit (with notification) fires after 1.5 s of silence
                        self._live_commit_timer.start()
                else:
                    send_immediately = not self.settings.chatbox_show_keyboard
                    logger.info(
                        "OSC chatbox send: text=%r send_immediately=%s play_notification=%s",
                        text, send_immediately, self.settings.chatbox_play_notification,
                    )
                    self._osc.send_chatbox(
                        text,
                        send_immediately=send_immediately,
                        play_notification=self.settings.chatbox_play_notification,
                    )
            else:
                if manual:
                    logger.warning(
                        "Manual send skipped OSC: send_osc=%s use_chatbox=%s",
                        self.settings.send_osc, self.settings.use_chatbox,
                    )

            # TTS — PTT modes only for STT results (avoids mic feedback in VAD);
            # manual typed sends always fire TTS if enabled.
            if self.settings.tts_enabled and (manual or mode != "vad"):
                h_idx = self._cmb_headphones.currentData()  # int or None
                c_idx = self._cmb_cable.currentData()        # int or None
                if h_idx is not None or c_idx is not None:
                    tts_engine = self.settings.tts_voice_engine
                    logger.info(
                        "TTS firing: engine=%s headphones_idx=%s cable_idx=%s",
                        tts_engine, h_idx, c_idx,
                    )
                    if tts_engine == "elevenlabs":
                        from ..tts.elevenlabs_tts import speak_text as el_speak
                        el_speak(
                            text,
                            self.settings.elevenlabs_api_key,
                            self.settings.elevenlabs_voice_id,
                            self.settings.elevenlabs_model_id,
                            [h_idx, c_idx],
                            volume=self.settings.ptt_sound_volume,
                            stability=self.settings.elevenlabs_stability,
                            similarity_boost=self.settings.elevenlabs_similarity_boost,
                            style=self.settings.elevenlabs_style,
                            use_speaker_boost=self.settings.elevenlabs_use_speaker_boost,
                        )
                    elif tts_engine == "polly":
                        from ..tts.polly_tts import speak_text as polly_speak
                        polly_speak(
                            text,
                            self.settings.polly_access_key_id,
                            self.settings.polly_secret_access_key,
                            self.settings.polly_region,
                            self.settings.polly_voice_id,
                            self.settings.polly_engine,
                            [h_idx, c_idx],
                            volume=self.settings.ptt_sound_volume,
                        )
                    elif tts_engine == "espeak":
                        from ..tts.espeak_tts import speak_text as esp_speak
                        esp_speak(
                            text,
                            [h_idx, c_idx],
                            volume=self.settings.ptt_sound_volume,
                            voice=self.settings.espeak_voice,
                            speed=self.settings.espeak_speed,
                            pitch=self.settings.espeak_pitch,
                        )
                    else:
                        from ..tts.system_tts import speak_text
                        speak_text(
                            text,
                            [h_idx, c_idx],
                            volume=self.settings.ptt_sound_volume,
                        )
                else:
                    if manual:
                        logger.warning("TTS enabled but no output devices selected — skipping playback.")

            status_text, status_color = self._listening_status()
            self._set_status(status_text, status_color)
        else:
            self.statusBar().showMessage(f"…{text}", 2000)

    def _commit_live_transcript(self) -> None:
        """Fired by the quiet timer after 1.5 s of silence in live-transcribe mode.

        Sends the accumulated text with the user's notification preference, then
        resets the accumulator so the next utterance starts a fresh chatbox entry.
        """
        if not self._live_accumulated:
            return
        if self.settings.send_osc and self.settings.use_chatbox:
            send_immediately = not self.settings.chatbox_show_keyboard
            self._osc.send_chatbox(
                self._live_accumulated,
                send_immediately=send_immediately,
                play_notification=self.settings.chatbox_play_notification,
            )
        self._live_accumulated = ""

    # ------------------------------------------------------------------ Global hotkeys

    def _start_global_hotkeys(self) -> None:
        """Register Quick Stop TTS and Resend hotkeys if configured."""
        self._stop_global_hotkeys()
        from ..stt.ptt_handler import PTTHandler

        if self.settings.tts_quick_stop_key:
            self._quick_stop_handler = PTTHandler(
                key=self.settings.tts_quick_stop_key,
                mode="ptt_toggle",
                on_press=lambda: self._quick_stop_signal.emit(),
            )
            self._quick_stop_handler.start()

        if self.settings.tts_resend_key:
            self._resend_handler = PTTHandler(
                key=self.settings.tts_resend_key,
                mode="ptt_toggle",
                on_press=lambda: self._resend_signal.emit(),
            )
            self._resend_handler.start()

    def _stop_global_hotkeys(self) -> None:
        if self._quick_stop_handler:
            self._quick_stop_handler.stop()
            self._quick_stop_handler = None
        if self._resend_handler:
            self._resend_handler.stop()
            self._resend_handler = None

    # --------------------------------------------------------------- SteamVR input

    def _start_steamvr_input(self) -> None:
        """Start the SteamVR input manager (silently no-ops if openvr is not installed)."""
        self._stop_steamvr_input()
        try:
            from ..input.steamvr_input import SteamVRInputManager, register_manifest
        except ImportError:
            return  # openvr package not installed

        import sys
        from pathlib import Path
        # PyInstaller 6+ places all datas files in _internal/ (sys._MEIPASS),
        # NOT next to the exe.  Path(sys.executable).parent is the exe directory.
        if getattr(sys, "frozen", False):
            base = Path(sys._MEIPASS)
        else:
            base = Path(__file__).parents[2]
        manifest_path   = str(base / "steamvr" / "actions.json")
        vrmanifest_path = str(base / "steamvr" / "RawriisSTT.vrmanifest")

        register_manifest(vrmanifest_path, manifest_path)

        self._steamvr_manager = SteamVRInputManager(
            action_manifest_path=manifest_path,
            vrmanifest_path=vrmanifest_path,
            on_ptt_press=lambda: self._steamvr_ptt_press_signal.emit(),
            on_ptt_release=lambda: self._steamvr_ptt_release_signal.emit(),
            on_stop_tts=lambda: self._steamvr_stop_signal.emit(),
            on_repeat_tts=lambda: self._steamvr_repeat_signal.emit(),
            ptt_mode=self.settings.whisper_input_mode,
        )
        self._steamvr_manager.start()

    def _stop_steamvr_input(self) -> None:
        if self._steamvr_manager:
            self._steamvr_manager.stop()
            self._steamvr_manager = None

    @pyqtSlot()
    def _do_quick_stop_tts(self) -> None:
        """Stop currently playing TTS audio (called in the GUI thread)."""
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    @pyqtSlot()
    def _do_resend_last_transcription(self) -> None:
        """Re-send the last transcription to OSC and/or TTS (called in the GUI thread)."""
        text = self._last_transcription
        if not text:
            return
        if self.settings.send_osc and self.settings.use_chatbox:
            send_immediately = not self.settings.chatbox_show_keyboard
            self._osc.send_chatbox(
                text,
                send_immediately=send_immediately,
                play_notification=self.settings.chatbox_play_notification,
            )
        if self.settings.tts_enabled and self.settings.whisper_input_mode != "vad":
            h_idx = self._cmb_headphones.currentData()
            c_idx = self._cmb_cable.currentData()
            if h_idx is not None or c_idx is not None:
                tts_engine = self.settings.tts_voice_engine
                if tts_engine == "elevenlabs":
                    from ..tts.elevenlabs_tts import speak_text as el_speak
                    el_speak(
                        text,
                        self.settings.elevenlabs_api_key,
                        self.settings.elevenlabs_voice_id,
                        self.settings.elevenlabs_model_id,
                        [h_idx, c_idx],
                        volume=self.settings.ptt_sound_volume,
                        stability=self.settings.elevenlabs_stability,
                        similarity_boost=self.settings.elevenlabs_similarity_boost,
                        style=self.settings.elevenlabs_style,
                        use_speaker_boost=self.settings.elevenlabs_use_speaker_boost,
                    )
                elif tts_engine == "polly":
                    from ..tts.polly_tts import speak_text as polly_speak
                    polly_speak(
                        text,
                        self.settings.polly_access_key_id,
                        self.settings.polly_secret_access_key,
                        self.settings.polly_region,
                        self.settings.polly_voice_id,
                        self.settings.polly_engine,
                        [h_idx, c_idx],
                        volume=self.settings.ptt_sound_volume,
                    )
                elif tts_engine == "espeak":
                    from ..tts.espeak_tts import speak_text as esp_speak
                    esp_speak(
                        text,
                        [h_idx, c_idx],
                        volume=self.settings.ptt_sound_volume,
                        voice=self.settings.espeak_voice,
                        speed=self.settings.espeak_speed,
                        pitch=self.settings.espeak_pitch,
                    )
                else:
                    from ..tts.system_tts import speak_text
                    speak_text(text, [h_idx, c_idx], volume=self.settings.ptt_sound_volume)

    # ------------------------------------------------------------------ Presets

    def _refresh_preset_btn(self) -> None:
        from ..config.presets import load_presets
        self._btn_load_preset.setEnabled(bool(load_presets()))

    def _save_preset(self) -> None:
        from ..config.presets import load_presets, save_presets, preset_from_settings

        name, ok = QInputDialog.getText(self, "Save Preset", "Preset name:")
        if not ok or not name.strip():
            return
        name = name.strip()

        presets = load_presets()
        if name in presets:
            reply = QMessageBox.question(
                self, "Overwrite Preset",
                f"A preset named \"{name}\" already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        presets[name] = preset_from_settings(self.settings)
        save_presets(presets)
        self._refresh_preset_btn()
        self.statusBar().showMessage(f"Preset \"{name}\" saved.", 3000)

    def _load_preset(self) -> None:
        from ..config.presets import load_presets, apply_preset_to_settings
        presets = load_presets()
        if not presets:
            return

        dialog = _PresetPickerDialog(presets, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            self._refresh_preset_btn()  # user may have deleted presets
            return
        name = dialog.selected_name()
        if name is None:
            return

        # Re-read in case the user deleted presets inside the dialog
        presets = load_presets()
        if name not in presets:
            return

        preset = presets[name]
        warnings: list[str] = []

        # --- edge-case checks ---
        engine = preset.get("stt_engine", "")
        if engine == "whisper":
            from ..stt.whisper_models import is_model_cached
            model = preset.get("whisper_model", "base")
            if not is_model_cached(model):
                warnings.append(
                    f"Whisper model \"{model}\" is not downloaded.\n"
                    "Go to Settings → STT to download it after loading."
                )
        elif engine == "azure":
            if not self.settings.azure_key or not self.settings.azure_region:
                warnings.append(
                    "This preset uses Azure STT but no Azure key/region is configured.\n"
                    "Go to Settings → STT to enter your credentials."
                )
        elif engine == "vosk":
            from ..stt.vosk_models import is_model_cached as vosk_cached, MODELS as VOSK_MODELS
            if not any(vosk_cached(m.key) for m in VOSK_MODELS):
                warnings.append(
                    "This preset uses Vosk but no Vosk model is downloaded.\n"
                    "Go to Settings → STT to download one."
                )

        voice_engine = preset.get("tts_voice_engine", "")
        if voice_engine == "elevenlabs" and not self.settings.elevenlabs_api_key:
            warnings.append(
                "This preset uses ElevenLabs TTS but no API key is configured.\n"
                "Go to Settings → TTS to enter your key."
            )

        if warnings:
            QMessageBox.warning(self, "Preset Warning", "\n\n".join(warnings))

        apply_preset_to_settings(preset, self.settings)
        self._apply_settings()
        self._schedule_save()
        self._refresh_preset_btn()
        self.statusBar().showMessage(f"Preset \"{name}\" loaded.", 3000)

    @pyqtSlot(str)
    def _on_stt_error(self, message: str) -> None:
        self._show_error(message)
        self._stop_listening()

    @pyqtSlot(bool)
    def _on_chatbox_toggled(self, checked: bool) -> None:
        self.settings.use_chatbox = checked
        self._schedule_save()

    @pyqtSlot(int)
    def _on_engine_changed(self, index: int) -> None:
        # If the clicked item is disabled (greyed out), revert to the current saved engine
        model = self._cmb_engine.model()
        item = model.item(index) if model else None
        if item and not item.isEnabled():
            prev_idx = next(
                (i for i, c in enumerate(ENGINE_CODES) if c == self.settings.stt_engine), 0
            )
            self._cmb_engine.blockSignals(True)
            self._cmb_engine.setCurrentIndex(prev_idx)
            self._cmb_engine.blockSignals(False)
            return

        code = ENGINE_CODES[index] if 0 <= index < len(ENGINE_CODES) else "whisper"
        self.settings.stt_engine = code
        self._schedule_save()

        self._whisper_panel.setVisible(code == "whisper")
        self._azure_panel.setVisible(code == "azure")
        self._vosk_panel.setVisible(code == "vosk")

        if code != "whisper" and self._whisper_engine:
            self._close_whisper()

        if code == "whisper":
            self._update_launch_btn()
        elif code == "vosk":
            self._update_vosk_btn()

    def _schedule_save(self) -> None:
        """Debounced save — restarts the 500ms timer on every call; one disk write per burst."""
        self._save_timer.start()

    def _set_status(self, text: str, color: str) -> None:
        self._lbl_status.setText(text)
        self._lbl_status.setStyleSheet(f"color: {color};")

    def _show_error(self, message: str) -> None:
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.critical(self, "Error", message)

    def _open_settings(self) -> None:
        from .settings_dialog import SettingsDialog
        old_model = self.settings.whisper_model
        old_azure_key = self.settings.azure_key
        old_azure_region = self.settings.azure_region
        old_ptt_key = self.settings.ptt_key
        old_quick_stop_key = self.settings.tts_quick_stop_key
        old_resend_key = self.settings.tts_resend_key
        old_input_mode = self.settings.whisper_input_mode
        from ..stt.vosk_models import is_model_cached as _vosk_cached, MODELS as _VOSK_MODELS
        old_vosk_available = any(_vosk_cached(m.key) for m in _VOSK_MODELS)
        dlg = SettingsDialog(self.settings, parent=self)
        if dlg.exec():
            self._osc.update_config(self.settings.osc_address, self.settings.osc_port)
            self._lbl_osc.setText(f"OSC  {self.settings.osc_address}:{self.settings.osc_port}")
            self._chk_chatbox.setChecked(self.settings.use_chatbox)
            # Sync sound volume immediately
            self._sound_player.set_volume(self.settings.ptt_sound_volume)
            # If the selected Whisper model changed, unload the engine so it reloads fresh
            if self.settings.whisper_model != old_model and self._whisper_engine:
                self._close_whisper()
            # If Azure credentials changed, invalidate previous validation
            if (self.settings.azure_key != old_azure_key
                    or self.settings.azure_region != old_azure_region):
                self._azure_validated = False
                self._lbl_azure_status.setText("Not validated")
                self._lbl_azure_status.setStyleSheet("color: #888888;")
                self._btn_validate_azure.setText("Validate")
                self._btn_validate_azure.setEnabled(True)
            # If Vosk model availability changed, unload the cached engine
            new_vosk_available = any(_vosk_cached(m.key) for m in _VOSK_MODELS)
            if old_vosk_available != new_vosk_available and self._vosk_engine:
                self._close_vosk()
            # Sync PTT key label if changed via Settings
            if self.settings.ptt_key != old_ptt_key:
                self._lbl_ptt_key.setText(_HotkeyCaptureDialog.fmt(self.settings.ptt_key))
            # Re-register global hotkeys if either changed
            if (self.settings.tts_quick_stop_key != old_quick_stop_key
                    or self.settings.tts_resend_key != old_resend_key):
                self._start_global_hotkeys()
            # Update SteamVR PTT mode if input mode changed
            if self.settings.whisper_input_mode != old_input_mode and self._steamvr_manager:
                self._steamvr_manager.set_ptt_mode(self.settings.whisper_input_mode)
            self._refresh_engine_combo()
            self._refresh_voice_engine_combo()
            if self._current_engine_code() == "whisper":
                self._update_launch_btn()
            elif self._current_engine_code() == "vosk":
                self._update_vosk_btn()

    # ------------------------------------------------------------------ Window events

    def closeEvent(self, event) -> None:
        self._stop_listening()
        self._stop_ptt_handler()
        self._stop_global_hotkeys()
        self._stop_steamvr_input()
        for thread in (self._load_thread, self._vosk_load_thread, self._azure_validate_thread):
            if thread and thread.isRunning():
                thread.quit()
                thread.wait(2000)
        self._save_timer.stop()   # cancel any pending debounce
        save_settings(self.settings)  # always write on exit
        super().closeEvent(event)
        QApplication.quit()

    # ---------------------------------------------------------------- update check

    def _start_update_check(self) -> None:
        self._update_checker = UpdateChecker()
        self._update_checker.update_available.connect(self._on_update_available)
        self._update_checker.start()

    def _on_update_available(self, tag: str, url: str) -> None:
        self._update_url = url
        self._lbl_update.setText(f"Update available: {tag}")
        self._lbl_update.show()

    def _open_release_page(self, *_) -> None:
        import webbrowser
        webbrowser.open(getattr(self, "_update_url", RELEASES_URL))


# ------------------------------------------------------------------ Utilities

def _h_line() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    line.setStyleSheet("color: #333333;")
    return line


def _labeled_combo(label_text: str, parent_layout: QVBoxLayout) -> QComboBox:
    row = QHBoxLayout()
    lbl = QLabel(label_text)
    lbl.setFixedWidth(90)
    cmb = QComboBox()
    cmb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    row.addWidget(lbl)
    row.addWidget(cmb)
    parent_layout.addLayout(row)
    return cmb
