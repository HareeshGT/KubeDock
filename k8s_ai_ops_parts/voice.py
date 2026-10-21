import os
import tempfile
import time
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
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl
from .ui import _QT_MULTIMEDIA_AVAILABLE, QMediaContent
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QGraphicsBlurEffect

from ui_icons import set_icon, apply_text_icon, icon_button
from themes import T, load_settings, save_settings
from utils import monospace_font
from .core import *
from .core import _NarrationThread, _OpenAINeuralTTSWorker, _Qwen3TTSWorker, _TTS_AVAILABLE, _VOICE_AVAILABLE, _NEURAL_TTS_AVAILABLE, _QWEN_TTS_AVAILABLE


class K8sAIOpsVoiceMixin:
    def _toggle_voice_feedback(self, checked: bool):
        self._voice_feedback_enabled = bool(checked and self._voice_feedback_available)
        save_voice_feedback_enabled(self._voice_feedback_enabled)
        if not self._voice_feedback_enabled:
            if self._narrator is not None:
                self._narrator.stop()
                self._narrator = None
            self._stop_neural_voice(clear_queue=True)

    def _populate_voice_combo(self):
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        self.voice_combo.addItem("System Default", "")
        for voice in list_voices():
            self.voice_combo.addItem(voice["name"], voice["id"])
        idx = self.voice_combo.findData(self._voice_id)
        self.voice_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.voice_combo.blockSignals(False)

    def _on_voice_engine_changed(self, index: int):
        requested = str(self.voice_engine_combo.itemData(index) or "system").lower()
        engine = requested
        preserve_openai_choice = False
        if engine == "openai":
            if not (_NEURAL_TTS_AVAILABLE and _QT_MULTIMEDIA_AVAILABLE):
                QMessageBox.information(self, "OpenAI Voice Unavailable", "Qt audio playback is unavailable in this KubeDock build. Select Qwen3-TTS · Local or System · Offline instead.")
                engine = "system"
            elif not ai_assist.get_api_key("openai"):
                QMessageBox.information(self, "OpenAI API Key Required", "Neural voice uses paid OpenAI API credits. Add a key under Settings → AI, or choose Qwen3-TTS · Local for local synthesis without per-use API charges.")
                engine = "system"
                preserve_openai_choice = True
        elif engine == "qwen" and not _QT_MULTIMEDIA_AVAILABLE:
            QMessageBox.information(self, "Qwen3-TTS Playback Unavailable", "This KubeDock build does not include Qt Multimedia audio playback.")
            engine = "system"
        self._voice_engine = engine
        save_voice_engine("openai" if preserve_openai_choice else engine)
        self.voice_engine_combo.blockSignals(True)
        self.voice_engine_combo.setCurrentIndex(max(0, self.voice_engine_combo.findData(engine)))
        self.voice_engine_combo.blockSignals(False)
        qwen_selected = engine == "qwen"
        self.qwen_model_combo.setVisible(qwen_selected)
        self.voice_combo.setEnabled(engine == "system" and _TTS_AVAILABLE)
        self.voice_combo_label.setVisible(engine == "system")
        self.voice_combo.setVisible(engine == "system")
        self._neural_tts_failed = False
        self._qwen_tts_failed = bool(qwen_selected and not _QWEN_TTS_AVAILABLE)
        self._stop_neural_voice(clear_queue=True)
        if self._narrator is not None:
            self._narrator.stop()
            self._narrator = None
        if engine == "openai":
            hint = "OpenAI neural voice selected · API usage may incur charges. This is a character-inspired voice direction, not a voice clone."
            notice = "OpenAI neural voice enabled. Text spoken by Ops Mind is sent to OpenAI; generated audio is temporarily written to a restricted WAV file and deleted after playback."
        elif engine == "qwen":
            if not _QWEN_TTS_AVAILABLE:
                hint = "Qwen3-TTS selected, but the local model package is not installed. Install requirements-qwen-tts.txt in KubeDock's Python environment, then restart."
                notice = "Qwen3-TTS was selected, but its optional local runtime is missing. Install the optional Qwen dependencies and restart KubeDock. No OpenAI speech API requests will be made."
            else:
                hint = "Qwen3-TTS local voice selected · no per-utterance API fee. The first use downloads model weights; speech generation runs locally."
                notice = "Qwen3-TTS local speech enabled. First use downloads public model weights; later synthesis runs locally. No OpenAI TTS API calls are made."
        else:
            hint = "Offline system voice enabled. Speech synthesis runs locally."
            notice = "Offline system voice enabled. Speech synthesis runs locally."
        self.voice_hint_lbl.setText(hint)
        self.voice_hint_lbl.setToolTip(notice)
        self._write_info("\n" + notice)
        if engine == "qwen" and not _QWEN_TTS_AVAILABLE:
            self._write_info("\nInstall command: python -m pip install -r requirements-qwen-tts.txt (use the Python environment that launches KubeDock).")
    def _stop_neural_voice(self, clear_queue: bool = True):
        if clear_queue:
            self._neural_speech_queue.clear()
        player = getattr(self, "_neural_audio_player", None)
        if player is not None:
            try: player.stop()
            except Exception: pass
            try: player.setMedia(QMediaContent())
            except Exception: pass
        self._neural_playback_active = False
        self._cleanup_neural_temp_file()
        worker = getattr(self, "_neural_worker", None)
        if worker is not None and worker.isRunning():
            worker.cancel()
        self._neural_current_text = ""

    def _cleanup_neural_temp_file(self):
        path = getattr(self, "_neural_temp_file", None)
        self._neural_temp_file = None
        if path:
            try: os.unlink(path)
            except FileNotFoundError: pass
            except OSError: pass

    def _process_neural_speech_queue(self):
        if self._voice_state_machine.state == "CLOSED":
            return
        if self._voice_engine != "openai" or self._neural_tts_failed:
            while self._neural_speech_queue:
                self._speak_system(self._neural_speech_queue.popleft())
            return
        worker = self._neural_worker
        if worker is not None and worker.isRunning():
            return
        if self._neural_temp_file or self._neural_playback_active:
            return
        if not self._neural_speech_queue:
            if self._continuous_voice_enabled and self._continuous_restart_pending and not self._busy:
                self._continuous_restart_pending = False
                self._schedule_continuous_voice(delay_ms=350)
            elif self._voice_state_machine.state == "SPEAKING":
                self._set_voice_state("IDLE")
                self.status_lbl.setText("Ready")
            return
        api_key = ai_assist.get_api_key("openai")
        if not api_key:
            self._on_neural_tts_error("OpenAI API key is not configured.")
            return
        self._neural_current_text = self._neural_speech_queue.popleft()
        worker = _OpenAINeuralTTSWorker(
            api_key, self._neural_current_text, self._personality,
            self._voice_pace, parent=self,
        )
        worker.audio_ready.connect(self._on_neural_audio_ready)
        worker.error.connect(self._on_neural_tts_error)
        worker.finished.connect(self._on_neural_worker_finished)
        self._neural_worker = worker
        track_worker(self._workers, worker)
        worker.start()

    def _on_neural_audio_ready(self, wav_bytes: bytes):
        if not self._neural_current_text or self._voice_engine != "openai" or self._neural_tts_failed:
            return
        try:
            with tempfile.NamedTemporaryFile(prefix="kubedock-ops-mind-", suffix=".wav", delete=False) as handle:
                self._neural_temp_file = handle.name
                handle.write(wav_bytes)
            try: os.chmod(self._neural_temp_file, 0o600)
            except OSError: pass
            player = self._neural_audio_player
            if player is None:
                raise RuntimeError("Qt audio playback is unavailable.")
            player.setVolume(self._voice_volume)
            player.setMedia(QMediaContent(QUrl.fromLocalFile(self._neural_temp_file)))
            self._neural_playback_active = True
            player.play()
        except Exception as exc:
            self._on_neural_tts_error(str(exc))

    def _on_neural_worker_finished(self):
        self._neural_worker = None
        if self._neural_tts_failed:
            self._process_neural_speech_queue()
        elif not self._neural_temp_file and not self._neural_playback_active:
            self._process_neural_speech_queue()

    def _on_neural_media_status_changed(self, status):
        if status != self._neural_end_of_media_status:
            return
        player = self._neural_audio_player
        if player is not None:
            try: player.setMedia(QMediaContent())
            except Exception: pass
        self._neural_playback_active = False
        self._neural_current_text = ""
        self._cleanup_neural_temp_file()
        if self._neural_speech_queue:
            self._process_neural_speech_queue()
            return
        self._on_narration_phrase_finished()
        self._on_narrator_idle()

    def _on_neural_player_state_changed(self, state):
        if state == self._neural_playing_state:
            self._set_voice_state("SPEAKING")

    def _on_neural_player_error(self, _error=None):
        if self._neural_temp_file or self._neural_playback_active:
            message = self._neural_audio_player.errorString() if self._neural_audio_player is not None else "Audio playback failed."
            self._on_neural_tts_error(message or "Audio playback failed.")

    def _on_neural_tts_error(self, message: str):
        text = self._neural_current_text
        if self._neural_tts_failed:
            if text:
                self._speak_system(text)
                self._neural_current_text = ""
            return
        self._neural_tts_failed = True
        player = self._neural_audio_player
        if player is not None:
            try: player.stop()
            except Exception: pass
            try: player.setMedia(QMediaContent())
            except Exception: pass
        self._neural_playback_active = False
        self._cleanup_neural_temp_file()
        detail = str(message or "unknown error")[:500]
        quota_exhausted = "api credits are exhausted" in detail.lower() or "insufficient_quota" in detail.lower() or "no credits remaining" in detail.lower()
        if quota_exhausted:
            notice = (
                "Neural voice is paused because your OpenAI API credits are exhausted. "
                "This reply is falling back to your local system voice. Add credits at "
                "https://platform.openai.com/settings/organization/billing/ and click Preview after billing is active. "
                "KubeDock will not retry neural speech automatically during this session.\n"
            )
            hint = "Neural voice paused · OpenAI API credits exhausted. Add credits, then click Preview to retry."
        else:
            notice = (
                "Neural voice failed; this utterance is falling back to the local system voice. "
                "Your Neural · OpenAI selection is preserved so you can retry after checking API access or network. "
                + detail + "\n"
            )
            hint = "Neural voice unavailable for this session; using local system voice. Check Settings → AI, then use Preview to retry."
        self._write_info("\n" + notice)
        if hasattr(self, "voice_hint_lbl"):
            self.voice_hint_lbl.setText(hint)
            self.voice_hint_lbl.setToolTip(notice.strip())
        if text:
            self._speak_system(text)
        self._neural_current_text = ""
        if self._neural_worker is None or not self._neural_worker.isRunning():
            self._process_neural_speech_queue()
    def _speak_system(self, text: str):
        if not _TTS_AVAILABLE:
            self._write_info("\nNo offline voice engine is installed; spoken output is unavailable.")
            return
        if self._narrator is None or not self._narrator.isRunning():
            self._narrator = _NarrationThread(
                voice_id=self._voice_id,
                rate=self._narrator_rate(),
                volume=self._voice_volume / 100.0,
                parent=self,
            )
            self._narrator.speaking_started.connect(lambda: self._set_voice_state("SPEAKING"))
            self._narrator.speaking_finished.connect(self._on_narration_phrase_finished)
            self._narrator.idle.connect(self._on_narrator_idle)
            self._narrator.start()
        self._narrator.speak(
            text, rate=self._narrator_rate(),
            volume=self._voice_volume / 100.0, voice_id=self._voice_id,
        )

    def _on_personality_changed(self, index: int):
        key = str(self.personality_combo.itemData(index) or "jarvis").lower()
        if key not in PERSONALITY_CONFIG or key == self._personality:
            return
        save_voice_id(self._voice_id, self._personality)
        self._personality = key
        save_assistant_personality(key)
        self._voice_id = get_voice_id(key) if _TTS_AVAILABLE else ""
        self._populate_voice_combo()
        if self._narrator is not None:
            self._narrator.stop()
            self._narrator = None
        self._stop_neural_voice(clear_queue=True)
        self._write_info(f"\nAssistant personality changed to {PERSONALITY_CONFIG[key]['label']}.")
        if self._voice_engine == "openai":
            self.voice_hint_lbl.setText(
                f"{PERSONALITY_CONFIG[key]['label']} · neural OpenAI voice · "
                "Voice input sends captured audio to Google Web Speech; KubeDock does not save raw recordings."
            )
        else:
            self.voice_hint_lbl.setText(
                f"{PERSONALITY_CONFIG[key]['label']} · local system voice · "
                "Voice input sends captured audio to Google Web Speech; KubeDock does not save raw recordings."
            )

    def _on_voice_combo_changed(self, index: int):
        voice_id = self.voice_combo.itemData(index) or ""
        if voice_id == self._voice_id:
            return
        self._voice_id = voice_id
        save_voice_id(voice_id, self._personality)
        if self._narrator is not None:
            self._narrator.stop()
            self._narrator = None

    def _on_voice_silence_changed(self, index: int):
        seconds = self.silence_combo.itemData(index)
        if seconds is None:
            return
        self._voice_silence_seconds = float(seconds)
        save_voice_silence_seconds(self._voice_silence_seconds)

    def _on_voice_pace_changed(self, index: int):
        pace = str(self.voice_pace_combo.itemData(index) or "natural")
        self._voice_pace = pace
        save_voice_pace(pace)
        if self._narrator is not None:
            self._narrator.stop()
            self._narrator = None
        if self._voice_engine == "openai":
            self._stop_neural_voice(clear_queue=True)

    def _on_voice_volume_changed(self, volume: int):
        self._voice_volume = max(0, min(100, int(volume)))
        save_voice_volume(self._voice_volume)
        if self._neural_audio_player is not None and self._neural_playback_active:
            self._neural_audio_player.setVolume(self._voice_volume)
        # The local engine reads volume before starting its next phrase.
        if self._narrator is not None and not self._narrator.is_speaking and not self._narrator.has_pending:
            self._narrator.stop()
            self._narrator = None

    def _set_voice_state(self, state: str):
        state = str(state or "IDLE").upper()
        controller = getattr(self, "_voice_state_machine", None)
        if controller is not None and not controller.transition(state):
            # The UI can receive late worker signals during shutdown; do not
            # let an impossible transition break the active operation.
            if state == "CLOSED":
                controller.state = "CLOSED"
            elif controller.state != "CLOSED":
                controller.state = state
        if hasattr(self, "voice_state_lbl"):
            self.voice_state_lbl.setText(state)
        if hasattr(self, "status_lbl") and state in {"LISTENING", "TRANSCRIBING", "THINKING", "SPEAKING", "ERROR"}:
            labels = {
                "LISTENING": "Listening…",
                "TRANSCRIBING": "Transcribing…",
                "THINKING": "Thinking…",
                "SPEAKING": "Speaking…",
                "ERROR": "Voice unavailable",
            }
            self.status_lbl.setText(labels[state])

    def _narrator_rate(self) -> int:
        base = PERSONALITY_CONFIG.get(self._personality, PERSONALITY_CONFIG["jarvis"])["rate"]
        factor = VOICE_PACE_FACTORS.get(self._voice_pace, 1.0)
        return int(base * factor)

    def _speak(self, text: str, force: bool = False):
        """Speak concise text using the selected neural or offline engine."""
        if not self._voice_feedback_enabled and not force:
            return
        styled_text = text if force else personalize_spoken_phrase(text, self._personality)
        if self._voice_engine == "openai" and not self._neural_tts_failed:
            if not (_NEURAL_TTS_AVAILABLE and _QT_MULTIMEDIA_AVAILABLE):
                self._on_neural_tts_error("Neural voice dependencies are unavailable.")
                if styled_text and self._voice_engine == "system":
                    self._speak_system(styled_text)
                return
            self._neural_speech_queue.append(styled_text)
            self._process_neural_speech_queue()
            return
        self._speak_system(styled_text)
    def _preview_personality_voice(self):
        if self._busy:
            self._write_info("\n Wait until the current Kubernetes operation finishes before previewing a voice.")
            return
        worker = self._voice_worker
        if worker is not None:
            if not self._continuous_voice_enabled:
                self._write_info("\n Stop the current push-to-talk capture before previewing the assistant voice.")
                return
            # Hands-free capture is paused, not disabled or forgotten. The
            # selected voice previews as soon as the microphone worker exits;
            # normal narration completion will resume hands-free listening.
            self._voice_preview_pending = True
            self._voice_error_occurred = False
            self._restart_voice_timer.stop()
            self._continuous_restart_pending = False
            worker.cancel()
            self._write_info("\n Pausing hands-free capture to preview the selected assistant voice…")
            return
        self._restart_voice_timer.stop()
        self._play_personality_voice_preview()

    def _play_personality_voice_preview(self):
        if self._busy:
            self._write_info("\n Wait until the current Kubernetes operation finishes before previewing a voice.")
            return
        if self._narrator is not None:
            self._narrator.interrupt()
        self._stop_neural_voice(clear_queue=True)
        if self._voice_engine == "openai":
            self._neural_tts_failed = False
        preview = PERSONALITY_CONFIG.get(self._personality, PERSONALITY_CONFIG["jarvis"])["preview"]
        self._speak(preview, force=True)
    def _on_narration_phrase_finished(self):
        if self._continuous_voice_enabled and not self._busy:
            self._continuous_restart_pending = True

    def _on_narrator_idle(self):
        if self._voice_state_machine.state == "CLOSED":
            return
        if self._continuous_voice_enabled and self._continuous_restart_pending:
            self._continuous_restart_pending = False
            self._schedule_continuous_voice(delay_ms=350)
        elif self._voice_state_machine.state == "SPEAKING":
            self._set_voice_state("IDLE")

    def _toggle_voice_recording(self):
        """Push-to-talk entry point; hands-free mode uses silence-based VAD."""
        if not self._ai_access_allowed:
            self.refresh_ai_access()
            return
        if self._voice_recording:
            self._stop_voice_recording()
            return
        if self._busy:
            return
        if not _VOICE_AVAILABLE:
            QMessageBox.information(
                self, "Voice Input Unavailable",
                "Install optional voice input dependencies:\n\n"
                "python3 -m pip install -r requirements-voice.txt\n\n"
                "Voice input sends captured audio to Google Web Speech for transcription."
            )
            return
        if self._ai_worker is not None or self._operation_worker is not None:
            self._write_info("\nPlease wait for the current AI/Kubernetes operation to finish.")
            return
        self._start_voice_recording()

    def _start_voice_recording(self, force: bool = False):
        if self._voice_worker is not None or self._voice_recording:
            return
        if self._busy and not force:
            return
        if not _VOICE_AVAILABLE:
            self._set_voice_state("ERROR")
            return
        self._voice_error_occurred = False
        self._voice_recording = True
        self.voice_btn.setEnabled(True)
        self._set_voice_state("LISTENING")
        self.voice_btn.setText("⏹ Stop Voice")
        self.voice_btn.setToolTip("Stop capture and transcribe this utterance. Ctrl+Shift+Space")
        self.request_input.clear()
        self.request_input.setPlaceholderText("Listening… speak naturally")
        self.request_input.setEnabled(False)
        self.run_btn.setEnabled(False)
        self.clear_history_btn.setEnabled(False)
        self._write_info(
            "\n Listening… speak your request and pause. "
            "Hands-free mode submits after silence; push-to-talk uses Stop Voice."
        )
        worker = K8sGoogleSpeechWorker(
            language=GOOGLE_STT_LANGUAGE,
            max_seconds=VOICE_MAX_SECONDS,
            auto_stop_on_silence=self._continuous_voice_enabled,
            silence_seconds=self._voice_silence_seconds,
        )
        worker.listening.connect(self._on_voice_listening)
        worker.stopped.connect(self._on_voice_stopped)
        worker.level.connect(self._on_voice_level)
        worker.done.connect(self._on_voice_done)
        worker.error.connect(self._on_voice_error)
        worker.finished.connect(self._on_voice_finished)
        self._voice_worker = worker
        track_worker(self._workers, worker)
        worker.start()

    def _stop_voice_recording(self):
        worker = self._voice_worker
        if worker is None:
            return
        self._set_voice_state("TRANSCRIBING")
        self.voice_btn.setText("⌛ Transcribing…")
        self.voice_btn.setEnabled(False)
        worker.stop()

    def _on_voice_level(self, level: int):
        if not hasattr(self, "mic_level_bar"):
            return
        level = max(0, min(100, int(level)))
        self.mic_level_bar.setValue(level)
        if level >= 75:
            self.mic_level_label.setText("Mic • high")
        elif level >= 35:
            self.mic_level_label.setText("Mic • medium")
        elif level >= 5:
            self.mic_level_label.setText("Mic • low")
        else:
            self.mic_level_label.setText("Mic")

    def _on_voice_listening(self):
        if self._voice_recording:
            self._set_voice_state("LISTENING")

    def _on_voice_stopped(self):
        if self._voice_recording:
            self._set_voice_state("TRANSCRIBING")

    def _on_voice_done(self, text: str):
        text = (text or "").strip()
        if not text:
            self._on_voice_error("The voice transcription was empty.")
            return
        now = time.monotonic()
        if is_duplicate_voice_transcript(
            text, self._last_voice_transcript, now, self._last_voice_timestamp
        ):
            self._voice_recording = False
            self._write_info("\n Duplicate voice transcript ignored to prevent repeating an operation.")
            self.request_input.clear()
            self._set_voice_state("IDLE")
            return
        self._last_voice_transcript = text
        self._last_voice_timestamp = now
        self.request_input.setPlaceholderText("Ask AI to perform a Kubernetes operation…")
        self.request_input.setText(text)
        self._write_info(f'\n You said: "{self._escape_html(text)}"')
        self._voice_recording = False
        self.voice_btn.setEnabled(True)
        apply_text_icon(self.voice_btn, " Voice", size=15)
        self.voice_btn.setToolTip("Push-to-talk. Ctrl+Shift+Space")
        self.request_input.setEnabled(not self._busy)
        self._set_voice_state("THINKING")
        if self._busy:
            self._queued_voice_request = text
            self._write_info("\n I will handle that request after the current operation finishes; it will not cancel the running operation.")
            return
        self._submit()

    def _on_voice_error(self, message: str):
        self._voice_error_occurred = True
        self._write_error("\n Voice input error:\n" + str(message))
        self._voice_recording = False
        self.request_input.setPlaceholderText("Ask AI to perform a Kubernetes operation…")
        self.request_input.setEnabled(not self._busy)
        self.voice_btn.setEnabled(not self._busy)
        apply_text_icon(self.voice_btn, " Voice", size=15)
        self.voice_btn.setToolTip("Push-to-talk. Ctrl+Shift+Space")
        self._set_voice_state("ERROR")

    def _on_voice_finished(self):
        self._voice_worker = None
        if self._voice_recording:
            self._voice_recording = False
        self.voice_btn.setEnabled(not self._busy)
        apply_text_icon(self.voice_btn, " Voice", size=15)
        self.voice_btn.setToolTip("Push-to-talk. Ctrl+Shift+Space")
        self.request_input.setPlaceholderText("Ask AI to perform a Kubernetes operation…")
        self.request_input.setEnabled(not self._busy)
        if hasattr(self, "mic_level_bar"):
            self.mic_level_bar.setValue(0)
        if hasattr(self, "mic_level_label"):
            self.mic_level_label.setText("Mic")
        if self._voice_preview_pending:
            self._voice_preview_pending = False
            self._set_voice_state("IDLE")
            QTimer.singleShot(180, self._play_personality_voice_preview)
            return
        if self._busy:
            self._set_voice_state("THINKING")
        elif self._voice_error_occurred:
            self._set_voice_state("ERROR")
        elif self._continuous_voice_enabled:
            self._set_voice_state("IDLE")
            self._schedule_continuous_voice()
        else:
            self._set_voice_state("IDLE")
            self.status_lbl.setText("Ready")

    def _on_continuous_voice_toggled(self, checked: bool):
        if checked and not _VOICE_AVAILABLE:
            self.continuous_voice_btn.blockSignals(True)
            self.continuous_voice_btn.setChecked(False)
            self.continuous_voice_btn.blockSignals(False)
            QMessageBox.information(
                self, "Hands-free Voice Unavailable",
                "Install optional voice dependencies with:\npython3 -m pip install -r requirements-voice.txt"
            )
            return
        if checked and not self._ai_access_allowed:
            self.continuous_voice_btn.blockSignals(True)
            self.continuous_voice_btn.setChecked(False)
            self.continuous_voice_btn.blockSignals(False)
            self.refresh_ai_access()
            return
        self._continuous_voice_enabled = bool(checked)
        save_continuous_voice_enabled(self._continuous_voice_enabled)
        self.continuous_voice_btn.setText("Hands-free ON" if checked else "Hands-free")
        if checked:
            self._write_info("\n Hands-free conversation enabled. Audio is sent to Google Web Speech for transcription; raw recordings are not saved by KubeDock.")
            if not self._busy:
                self._start_voice_recording()
        else:
            self._restart_voice_timer.stop()
            self._continuous_restart_pending = False
            worker = self._voice_worker
            if worker is not None:
                worker.cancel()
            self._voice_recording = False
            self._set_voice_state("IDLE")

    def _on_voice_silence_changed(self, index: int):
        seconds = self.silence_combo.itemData(index)
        if seconds is not None:
            self._voice_silence_seconds = float(seconds)
            save_voice_silence_seconds(self._voice_silence_seconds)

    def _on_voice_pace_changed(self, index: int):
        pace = str(self.voice_pace_combo.itemData(index) or "natural")
        self._voice_pace = pace
        save_voice_pace(pace)
        self._replace_narrator()

    def _on_voice_volume_changed(self, volume: int):
        self._voice_volume = max(0, min(100, int(volume)))
        save_voice_volume(self._voice_volume)
        if self._neural_audio_player is not None and self._neural_playback_active:
            self._neural_audio_player.setVolume(self._voice_volume)
        if self._narrator is not None and not self._narrator.is_speaking and not self._narrator.has_pending:
            self._replace_narrator()

    def _replace_narrator(self):
        if self._narrator is not None:
            self._narrator.stop()
            self._narrator = None

    def _schedule_continuous_voice(self, delay_ms: int = 500):
        if not self._continuous_voice_enabled or self._voice_state_machine.state == "CLOSED":
            return
        if self._busy or self._voice_recording or self._voice_worker is not None:
            return
        narrator = self._narrator
        if narrator is not None and (narrator.is_speaking or narrator.has_pending):
            self._continuous_restart_pending = True
            return
        self._restart_voice_timer.start(max(0, int(delay_ms)))

    def _maybe_resume_continuous_voice(self):
        if not self._continuous_voice_enabled or self._busy:
            return
        if self._voice_recording or self._voice_worker is not None:
            return
        if self._narrator is not None and (self._narrator.is_speaking or self._narrator.has_pending):
            self._continuous_restart_pending = True
            return
        self._start_voice_recording()

    def _run_queued_voice_request(self):
        request = getattr(self, "_queued_voice_request", None)
        if not request or self._busy:
            return
        self._queued_voice_request = None
        self.request_input.setText(request)
        self._set_voice_state("THINKING")
        self._submit()
        if not self._busy:
            self._schedule_continuous_voice()
    def _interrupt_and_listen(self):
        if not _VOICE_AVAILABLE:
            QMessageBox.information(self, "Voice Input Unavailable", "Install PyAudio and SpeechRecognition to use voice input.")
            return
        if self._voice_worker is not None:
            self._write_info("\n Already listening; stop the current capture before interrupting.")
            return
        narrator = self._narrator
        if narrator is not None:
            narrator.interrupt()
        self._stop_neural_voice(clear_queue=True)
        self._set_voice_state("INTERRUPTED")
        if self._busy:
            self._write_info("\n Speech stopped. The current Kubernetes operation will continue; your next request will be queued until it finishes.")
            self._start_voice_recording(force=True)
        else:
            self._start_voice_recording()


