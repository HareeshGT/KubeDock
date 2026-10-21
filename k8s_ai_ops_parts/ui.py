from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QMessageBox,
    QShortcut,
    QProgressBar,
    QComboBox,
    QSlider,
)
import os
import tempfile
from collections import deque

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QGraphicsBlurEffect

try:
    from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
    _QT_MULTIMEDIA_AVAILABLE = True
except Exception:
    QMediaContent = None
    QMediaPlayer = None
    _QT_MULTIMEDIA_AVAILABLE = False

from ui_icons import set_icon, apply_text_icon, icon_button
from themes import T, load_settings, save_settings
from utils import monospace_font
from .core import *
from .core import _TTS_AVAILABLE, _VOICE_AVAILABLE, _NEURAL_TTS_AVAILABLE, _QWEN_TTS_AVAILABLE


class K8sAIOpsUIMixin:
    def __init__(self, ssh=None, namespace_getter=None, context_getter=None, kube_context_getter=None, parent=None):
        super().__init__(parent)
        self.ssh = ssh
        self._namespace_getter = namespace_getter
        self._context_getter = context_getter
        self._kube_context_getter = kube_context_getter
        self._workers = []
        self._ai_worker = None
        self._operation_worker = None
        self._scale_previous_worker = None
        self._pending_scale_action = None
        self._voice_worker = None
        self._voice_recording = False
        self._narrator = None
        self._personality = get_assistant_personality()
        self._voice_engine = get_voice_engine()
        if self._voice_engine == "openai" and not (_NEURAL_TTS_AVAILABLE and _QT_MULTIMEDIA_AVAILABLE):
            self._voice_engine = "system"
        if self._voice_engine == "qwen" and not _QT_MULTIMEDIA_AVAILABLE:
            self._voice_engine = "system"
        self._voice_feedback_available = bool(
            _TTS_AVAILABLE
            or (_NEURAL_TTS_AVAILABLE and _QT_MULTIMEDIA_AVAILABLE and ai_assist.get_api_key('openai'))
            or (_QWEN_TTS_AVAILABLE and _QT_MULTIMEDIA_AVAILABLE)
        )
        self._voice_feedback_enabled = bool(self._voice_feedback_available and get_voice_feedback_enabled())
        self._voice_id = get_voice_id(self._personality) if _TTS_AVAILABLE else ''
        self._continuous_voice_enabled = bool(get_continuous_voice_enabled() and _VOICE_AVAILABLE)
        self._voice_silence_seconds = get_voice_silence_seconds()
        self._voice_pace = get_voice_pace()
        self._voice_volume = get_voice_volume()
        self._neural_speech_queue = deque()
        self._neural_worker = None
        self._neural_current_text = ''
        self._neural_temp_file = None
        self._neural_tts_failed = False
        self._qwen_tts_model = get_qwen_tts_model()
        self._qwen_tts_failed = not _QWEN_TTS_AVAILABLE
        self._qwen_worker = None
        self._qwen_job_active = False
        self._qwen_current_job_id = None
        self._neural_playback_active = False
        self._neural_audio_player = QMediaPlayer(self) if _QT_MULTIMEDIA_AVAILABLE else None
        self._neural_end_of_media_status = QMediaPlayer.EndOfMedia if _QT_MULTIMEDIA_AVAILABLE else None
        self._neural_playing_state = QMediaPlayer.PlayingState if _QT_MULTIMEDIA_AVAILABLE else None
        if self._neural_audio_player is not None:
            self._neural_audio_player.mediaStatusChanged.connect(self._on_neural_media_status_changed)
            self._neural_audio_player.stateChanged.connect(self._on_neural_player_state_changed)
            self._neural_audio_player.error.connect(self._on_neural_player_error)
        self._voice_state_machine = VoiceConversationStateMachine()
        self._last_voice_transcript = ''
        self._last_voice_timestamp = 0.0
        self._queued_voice_request = None
        self._voice_conversation = []
        self._last_submitted_request = ""
        self._voice_error_occurred = False
        self._voice_preview_pending = False
        self._continuous_restart_pending = False
        self._restart_voice_timer = QTimer(self)
        self._restart_voice_timer.setSingleShot(True)
        self._restart_voice_timer.timeout.connect(self._maybe_resume_continuous_voice)
        self._busy = False
        self._history = _load_history()
        self._ai_access_allowed = False
        self._ai_blur_effect = None
        self._ai_lock_overlay = None
        self._build_ui()

    def set_ssh(self, ssh):
        self.ssh = ssh

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._ai_content = QWidget(self)
        outer.addWidget(self._ai_content)
        root = QVBoxLayout(self._ai_content)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)
        title_row = QHBoxLayout()
        title = QLabel(' AI Kubernetes Operations')
        title.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; font-size: 16px; font-weight: 700;")
        title_row.addWidget(title)
        title_row.addStretch()
        self.history_lbl = QLabel()
        self.history_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 11px;")
        title_row.addWidget(self.history_lbl)
        self.status_lbl = QLabel('Ready')
        self.status_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        title_row.addWidget(self.status_lbl)
        root.addLayout(title_row)
        description = QLabel('Describe a Kubernetes operation in plain English. Previous Ops Mind operations are remembered across app restarts.')
        description.setWordWrap(True)
        description.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        root.addWidget(description)
        examples = QLabel('Examples: scale deployment deployment-name to 5 replicas  •  scale up deployment-name by 2  •  scale down deployment-name by 1  •  scale it back  •  restart deployment deployment-name  •  repeat that')
        examples.setWordWrap(True)
        examples.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 11px;")
        root.addWidget(examples)
        initial_voice_hint = {
            "openai": "OpenAI neural voice selected · API usage may incur charges. ",
            "qwen": (
                "Qwen3-TTS selected, but its optional local runtime is missing. Install requirements-qwen-tts.txt and restart. "
                if not _QWEN_TTS_AVAILABLE
                else "Qwen3-TTS local voice selected · no per-utterance API fee; first use downloads model weights. "
            ),
            "system": "Using the local system voice. ",
        }.get(self._voice_engine, "Using the local system voice. ")
        self.voice_hint_lbl = QLabel(
            initial_voice_hint
            + "Microphone transcription uses Google Web Speech; raw recordings are not saved by KubeDock."
        )
        self.voice_hint_lbl.setWordWrap(True)
        self.voice_hint_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 10px;")
        self.voice_hint_lbl.setToolTip('Voice input sends the captured utterance to Google Web Speech for transcription. KubeDock does not save raw audio recordings.')
        root.addWidget(self.voice_hint_lbl)
        self.request_input = QLineEdit()
        self.request_input.setPlaceholderText('Ask AI to perform a Kubernetes operation…')
        self.request_input.returnPressed.connect(self._submit)
        root.addWidget(self.request_input)
        btn_row = QHBoxLayout()
        self.run_btn = icon_button(' Run with AI')
        self.run_btn.setObjectName('primary')
        self.run_btn.setFixedHeight(34)
        self.run_btn.clicked.connect(self._submit)
        btn_row.addWidget(self.run_btn)
        self.voice_btn = icon_button(' Voice')
        self.voice_btn.setFixedHeight(34)
        self.voice_btn.setToolTip('Start/stop voice command. Hotkey: Ctrl+Shift+Space')
        self.voice_btn.clicked.connect(self._toggle_voice_recording)
        btn_row.addWidget(self.voice_btn)
        self.mic_level_bar = QProgressBar()
        self.mic_level_bar.setRange(0, 100)
        self.mic_level_bar.setValue(0)
        self.mic_level_bar.setFixedWidth(120)
        self.mic_level_bar.setFixedHeight(12)
        self.mic_level_bar.setTextVisible(False)
        self.mic_level_bar.setToolTip('Live microphone input level')
        self.mic_level_bar.setStyleSheet(f"\n      QProgressBar {{\n        background: {T['BG_ITEM']};\n        border: 1px solid {T['BORDER']};\n        border-radius: 6px;\n      }}\n\n      QProgressBar::chunk {{\n        background: {T['ACCENT']};\n        border-radius: 5px;\n      }}\n      ")
        self.mic_level_label = QLabel('Mic')
        self.mic_level_label.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 10px;")
        btn_row.addWidget(self.mic_level_label)
        btn_row.addWidget(self.mic_level_bar)
        self.speaker_btn = QPushButton(' Speak' if self._voice_feedback_enabled else ' Speak')
        self.speaker_btn.setFixedHeight(34)
        self.speaker_btn.setCheckable(True)
        self.speaker_btn.setChecked(self._voice_feedback_enabled)
        self.speaker_btn.setEnabled(self._voice_feedback_available)
        self.speaker_btn.setToolTip("Enable the selected AI-generated neural voice or the offline system voice" if self._voice_feedback_available else 'Install websocket-client and add an OpenAI API key, or install pyttsx3 for offline voice narration.')
        self.speaker_btn.toggled.connect(self._toggle_voice_feedback)
        btn_row.addWidget(self.speaker_btn)
        self.voice_combo = QComboBox()
        self.voice_combo.setFixedWidth(170)
        self.voice_combo.setFixedHeight(34)
        self.voice_combo.setEnabled(_TTS_AVAILABLE)
        self.voice_combo.setToolTip("Which system voice reads Ops Mind's status narration aloud" if _TTS_AVAILABLE else 'Voice narration needs the pyttsx3 package (pip install pyttsx3)')
        self._populate_voice_combo()
        self.voice_combo.currentIndexChanged.connect(self._on_voice_combo_changed)
        self.voice_test_btn = QPushButton("Preview")
        self.voice_test_btn.setFixedSize(76, 34)
        self.voice_test_btn.setEnabled(self._voice_feedback_available)
        self.voice_test_btn.setToolTip('Preview the selected neural or offline voice')
        self.voice_test_btn.clicked.connect(self._preview_personality_voice)
        self.voice_shortcut = QShortcut(QKeySequence('Ctrl+Shift+Space'), self)
        self.voice_shortcut.activated.connect(self._toggle_voice_recording)
        clear_btn = QPushButton('Clear Output')
        clear_btn.setFixedHeight(34)
        clear_btn.clicked.connect(self._clear_output)
        btn_row.addWidget(clear_btn)
        self.clear_history_btn = QPushButton('Clear History')
        self.clear_history_btn.setFixedHeight(34)
        self.clear_history_btn.clicked.connect(self._clear_history)
        btn_row.addWidget(self.clear_history_btn)
        self.audit_btn = QPushButton('Audit Log')
        self.audit_btn.setFixedHeight(34)
        self.audit_btn.clicked.connect(self._show_audit_log)
        btn_row.addWidget(self.audit_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

        # Compact Ops Mind voice/personality controls, persisted through the
        # existing SQLite-backed application settings.
        voice_settings_row = QHBoxLayout()
        voice_settings_row.setSpacing(8)
        voice_settings_row.addWidget(QLabel("Engine"))
        self.voice_engine_combo = QComboBox()
        self.voice_engine_combo.setFixedWidth(175)
        self.voice_engine_combo.addItem("Neural · OpenAI", "openai")
        self.voice_engine_combo.addItem("Qwen3-TTS · Local", "qwen")
        self.voice_engine_combo.addItem("System · Offline", "system")
        engine_idx = self.voice_engine_combo.findData(self._voice_engine)
        self.voice_engine_combo.setCurrentIndex(engine_idx if engine_idx >= 0 else 1)
        self.voice_engine_combo.setToolTip("OpenAI: paid neural TTS. Qwen3-TTS: locally generated speech with no per-use API fee (optional model download). System: offline OS voice.")
        if not (_NEURAL_TTS_AVAILABLE and _QT_MULTIMEDIA_AVAILABLE):
            item = self.voice_engine_combo.model().item(0)
            if item is not None:
                item.setEnabled(False)
        self.voice_engine_combo.currentIndexChanged.connect(self._on_voice_engine_changed)
        voice_settings_row.addWidget(self.voice_engine_combo)

        self.qwen_model_combo = QComboBox()
        self.qwen_model_combo.setFixedWidth(190)
        self.qwen_model_combo.addItem("Voice Design · 1.7B", "voice_design")
        self.qwen_model_combo.addItem("Lower memory · 0.6B", "custom_voice")
        qwen_idx = self.qwen_model_combo.findData(self._qwen_tts_model)
        self.qwen_model_combo.setCurrentIndex(qwen_idx if qwen_idx >= 0 else 0)
        self.qwen_model_combo.setToolTip(
            "1.7B provides custom voice design for each persona and uses more memory. "
            "0.6B is lighter and uses preset speaker timbres. Model weights download on first use."
        )
        self.qwen_model_combo.currentIndexChanged.connect(self._on_qwen_model_changed)
        self.qwen_model_combo.setVisible(self._voice_engine == "qwen")
        voice_settings_row.addWidget(self.qwen_model_combo)

        voice_settings_row.addWidget(QLabel("Persona"))
        self.personality_combo = QComboBox()
        self.personality_combo.setFixedWidth(178)
        for key in ("jarvis", "friday", "edith"):
            self.personality_combo.addItem(PERSONALITY_CONFIG[key]["label"], key)
        idx = self.personality_combo.findData(self._personality)
        self.personality_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.personality_combo.currentIndexChanged.connect(self._on_personality_changed)
        self.personality_combo.setToolTip("Choose Ops Mind's conversational style and voice profile")
        voice_settings_row.addWidget(self.personality_combo)

        self.voice_combo_label = QLabel("Offline voice")
        self.voice_combo_label.setToolTip("Choose the installed system voice when System · Offline is selected")
        voice_settings_row.addWidget(self.voice_combo_label)
        self.voice_combo.setFixedWidth(165)
        self.voice_combo.setToolTip("System voice used only by the offline voice engine")
        self.voice_combo.setEnabled(self._voice_engine == "system" and _TTS_AVAILABLE)
        self.voice_combo_label.setVisible(self._voice_engine == "system")
        self.voice_combo.setVisible(self._voice_engine == "system")
        voice_settings_row.addWidget(self.voice_combo)
        voice_settings_row.addWidget(self.voice_test_btn)
        voice_settings_row.addStretch()
        root.addLayout(voice_settings_row)

        # Conversation controls get their own row so the window never clips them.
        voice_settings_row2 = QHBoxLayout()
        voice_settings_row2.setSpacing(8)

        self.continuous_voice_btn = QPushButton("Hands-free")
        self.continuous_voice_btn.setCheckable(True)
        self.continuous_voice_btn.setChecked(self._continuous_voice_enabled)
        self.continuous_voice_btn.toggled.connect(self._on_continuous_voice_toggled)
        self.continuous_voice_btn.setToolTip("Keep the conversation open and submit each utterance after silence is detected")
        self.continuous_voice_btn.setEnabled(_VOICE_AVAILABLE)
        voice_settings_row2.addWidget(self.continuous_voice_btn)

        self.interrupt_btn = QPushButton("Interrupt & Listen")
        self.interrupt_btn.setEnabled(_VOICE_AVAILABLE)
        self.interrupt_btn.clicked.connect(self._interrupt_and_listen)
        self.interrupt_btn.setToolTip("Stop the current spoken phrase and listen for a new instruction. Does not cancel an operation already running.")
        voice_settings_row2.addWidget(self.interrupt_btn)

        voice_settings_row2.addWidget(QLabel("Silence"))
        self.silence_combo = QComboBox()
        self.silence_combo.setFixedWidth(82)
        for seconds in VOICE_SILENCE_CHOICES:
            self.silence_combo.addItem(f"{seconds:.1f}s", seconds)
        silence_idx = self.silence_combo.findData(self._voice_silence_seconds)
        self.silence_combo.setCurrentIndex(silence_idx if silence_idx >= 0 else 1)
        self.silence_combo.currentIndexChanged.connect(self._on_voice_silence_changed)
        self.silence_combo.setToolTip("Silence duration that ends a hands-free utterance")
        self.silence_combo.setEnabled(_VOICE_AVAILABLE)
        voice_settings_row2.addWidget(self.silence_combo)

        voice_settings_row2.addWidget(QLabel("Pace"))
        self.voice_pace_combo = QComboBox()
        self.voice_pace_combo.setFixedWidth(95)
        for pace, label in (("slow", "Slow"), ("natural", "Natural"), ("brisk", "Brisk")):
            self.voice_pace_combo.addItem(label, pace)
        pace_idx = self.voice_pace_combo.findData(self._voice_pace)
        self.voice_pace_combo.setCurrentIndex(pace_idx if pace_idx >= 0 else 1)
        self.voice_pace_combo.currentIndexChanged.connect(self._on_voice_pace_changed)
        voice_settings_row2.addWidget(self.voice_pace_combo)

        voice_settings_row2.addWidget(QLabel("Volume"))
        self.voice_volume_slider = QSlider(Qt.Horizontal)
        self.voice_volume_slider.setRange(0, 100)
        self.voice_volume_slider.setValue(self._voice_volume)
        self.voice_volume_slider.setFixedWidth(75)
        self.voice_volume_slider.setToolTip("Speech playback volume")
        self.voice_volume_slider.valueChanged.connect(self._on_voice_volume_changed)
        self.voice_volume_slider.setEnabled(self._voice_engine in {"openai", "qwen"} or _TTS_AVAILABLE)
        voice_settings_row2.addWidget(self.voice_volume_slider)

        self.voice_state_lbl = QLabel(self._voice_state_machine.state)
        self.voice_state_lbl.setMinimumWidth(85)
        self.voice_state_lbl.setStyleSheet(f"color: {T['ACCENT']}; font-size: 10px; font-weight: 700;")
        self.voice_state_lbl.setToolTip("Voice conversation state")
        voice_settings_row2.addWidget(self.voice_state_lbl)
        voice_settings_row2.addStretch()
        root.addLayout(voice_settings_row2)

        self.output = QTextBrowser()
        self.output.setOpenExternalLinks(False)
        self.output.setFont(monospace_font(11))
        self.output.setStyleSheet(f"background: {T['BG_DARK']}; color: {T['TEXT_PRIMARY']}; border: 1px solid {T['BORDER']}; border-radius: 8px; padding: 12px;")
        root.addWidget(self.output, 1)
        self._update_history_label()
        self._write_info('Ops Mind is ready.\n\nPrevious operations are remembered and used as context for follow-up requests.\nTry: scale deployment my-app to 3 replicas\n   scale up my-app by 2\n   scale down my-app by 1\n   scale it back\n   repeat that\n')
        self._build_ai_access_overlay()
        self.refresh_ai_access()

    def _build_ai_access_overlay(self):
        """Create the blurred/locked surface shown when no AI API key exists."""
        self._ai_blur_effect = QGraphicsBlurEffect(self._ai_content)
        self._ai_blur_effect.setBlurRadius(9)
        self._ai_content.setGraphicsEffect(self._ai_blur_effect)
        self._ai_lock_overlay = QWidget(self)
        self._ai_lock_overlay.setObjectName('aiOpsLockOverlay')
        self._ai_lock_overlay.setStyleSheet(f'QWidget#aiOpsLockOverlay {{ background: rgba(0, 0, 0, 150); }}')
        overlay_layout = QVBoxLayout(self._ai_lock_overlay)
        overlay_layout.setContentsMargins(30, 30, 30, 30)
        card = QWidget(self._ai_lock_overlay)
        card.setObjectName('aiOpsLockCard')
        card.setMaximumWidth(520)
        card.setStyleSheet(f"QWidget#aiOpsLockCard {{ background: {T['BG_PANEL']}; border: 1px solid {T['BORDER']}; border-radius: 16px; }}")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(30, 26, 30, 26)
        card_layout.setSpacing(10)
        icon = QLabel('')
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet('font-size: 34px; background: transparent; border: none;')
        card_layout.addWidget(icon)
        title = QLabel('Ops Mind is locked')
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; font-size: 18px; font-weight: 700; background: transparent; border: none;")
        card_layout.addWidget(title)
        self._ai_lock_message = QLabel()
        self._ai_lock_message.setAlignment(Qt.AlignCenter)
        self._ai_lock_message.setWordWrap(True)
        self._ai_lock_message.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px; background: transparent; border: none;")
        card_layout.addWidget(self._ai_lock_message)
        overlay_layout.addStretch(1)
        overlay_layout.addWidget(card, 0, Qt.AlignCenter)
        overlay_layout.addStretch(1)
        self._ai_lock_overlay.hide()

    def _selected_ai_provider_label(self):
        provider = ai_assist.get_provider()
        return ai_assist.PROVIDERS.get(provider, {}).get('label', provider)

    def refresh_ai_access(self):
        """Enable Ops Mind only when the selected provider has an API key."""
        provider = ai_assist.get_provider()
        api_key = ai_assist.get_api_key(provider)
        allowed = bool((api_key or '').strip())
        self._ai_access_allowed = allowed
        if hasattr(self, '_ai_lock_overlay') and self._ai_lock_overlay is not None:
            if allowed:
                self._ai_lock_overlay.hide()
                self._ai_content.setGraphicsEffect(None)
                self.status_lbl.setText('Ready')
            else:
                label = self._selected_ai_provider_label()
                self._ai_lock_message.setText(f'Add an API key for <b>{self._escape_html(label)}</b> in <b>Settings → AI</b> to access Kubernetes AI Operations.')
                self._ai_content.setGraphicsEffect(self._ai_blur_effect)
                self._ai_lock_overlay.raise_()
                self._ai_lock_overlay.show()
                self.status_lbl.setText('API key required')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._ai_lock_overlay is not None:
            self._ai_lock_overlay.setGeometry(self.rect())
            self._ai_lock_overlay.raise_()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_ai_access()

    def _namespace(self) -> str:
        if self._namespace_getter:
            try:
                value = self._namespace_getter()
                return str(value or '').strip()
            except Exception:
                pass
        return ''

    def _context(self) -> str:
        if self._context_getter:
            try:
                value = self._context_getter()
                return str(value or '').strip()
            except Exception:
                pass
        return ''

    def _kube_context(self) -> str:
        """The actually-selected kubectl context (cluster), used for the
        `--context` flag. Distinct from `_context()`, which is a free-text
        UI blurb fed to the AI prompt and must never reach the shell."""
        if self._kube_context_getter:
            try:
                value = self._kube_context_getter()
                return str(value or '').strip()
            except Exception:
                pass
        return ''

    def _remember_conversation_turn(self, role: str, content: str):
        """Keep a small, in-memory, redacted transcript for conversational follow-ups."""
        role = "user" if role == "user" else "assistant"
        text = ai_assist._redact_sensitive_text(str(content or "").strip())
        if not text:
            return
        if len(text) > 1000:
            text = text[:1000].rstrip() + "…"
        turns = getattr(self, "_voice_conversation", [])
        if turns and turns[-1].get("role") == role and turns[-1].get("content") == text:
            return
        turns.append({"role": role, "content": text})
        self._voice_conversation = turns[-8:]
    def _update_history_label(self):
        count = len(self._history)
        self.history_lbl.setText(f"{count} saved operation{('s' if count != 1 else '')}")

    def _write_info(self, text: str):
        self.output.append(f"""<span style="color:{T['TEXT_DIM']}">{self._escape_html(text).replace(chr(10), '<br>')}</span>""")

    def _write_success(self, text: str):
        self.output.append(f"""<span style="color:{T['SUCCESS']}">{self._escape_html(text).replace(chr(10), '<br>')}</span>""")

    def _write_error(self, text: str):
        self.output.append(f"""<span style="color:{T['DANGER']}">{self._escape_html(text).replace(chr(10), '<br>')}</span>""")

    @staticmethod
    def _escape_html(text: str) -> str:
        return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.request_input.setEnabled(not busy and (not self._voice_recording))
        self.run_btn.setEnabled(not busy and (not self._voice_recording))
        self.voice_btn.setEnabled(not busy or self._voice_recording)
        self.clear_history_btn.setEnabled(not busy and (not self._voice_recording))
        if busy:
            apply_text_icon(self.run_btn, ' Thinking…', size=15)
            self.status_lbl.setText('AI is interpreting…')
        else:
            apply_text_icon(self.run_btn, ' Run with AI', size=15)
            self.status_lbl.setText('Ready')
            queued = getattr(self, '_queued_voice_request', None)
            if queued:
                QTimer.singleShot(0, self._run_queued_voice_request)
            elif hasattr(self, '_schedule_continuous_voice'):
                self._schedule_continuous_voice()

    def _clear_output(self):
        if self._busy:
            return
        self.output.clear()
        self._write_info('Ops Mind is ready.')

    def _clear_history(self):
        if self._busy:
            return
        if not self._history:
            self._update_history_label()
            self._write_info('\nNo saved operation history to clear.')
            return
        answer = QMessageBox.question(self, 'Clear Ops Mind History', 'Delete the saved AI Kubernetes operation history?', QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self._history = []
        _save_history(self._history)
        self._update_history_label()
        self._write_info('\n Ops Mind history cleared.')
