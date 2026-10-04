# Path: dialogs_parts/media.py
from .common import *

class MediaPlayerDialog(QDialog):
  """Remote audio/video player with streaming, codec fallback and transport tools.

  Supported controls include play/pause, stop, variable playback speed, seek,
  repeat, mute/volume, fullscreen, video snapshot, aspect-ratio selection,
  keyboard shortcuts and media information. Direct playback is preferred;
  audio-only FFmpeg decoding is used when QtMultimedia cannot decode the
  remote codec.
  """

  # Strong references to open (non-modal) players.
  _open_players = set()

  def __init__(self, parent, sftp, ssh, remote_path: str, kind: str, sudo_user=None):
    # macOS: the main window runs in a native fullscreen Space. Any separate
    # top-level window (dialog, tool window, sheet) can be placed on a
    # different Space, which makes macOS switch desktops and the app "loses
    # focus". So when we have a host window the player is created as a plain
    # child widget (Qt.Widget) that floats *inside* the main window - no
    # second native window, no Space switch.
    host = parent.window() if parent is not None else None
    self._host = host
    self._embedded = host is not None
    if self._embedded:
      super().__init__(host)
      # QDialog's constructor forces the Qt.Dialog window type even when
      # Qt.Widget is passed, which would still create a separate native
      # window. Clearing the flags afterwards turns it into a real child
      # widget of the host (isWindow() == False).
      self.setWindowFlags(Qt.Widget)
      self.setAttribute(Qt.WA_StyledBackground, True)
      self.setAutoFillBackground(True)
      self.setFocusPolicy(Qt.StrongFocus)
      host.installEventFilter(self)
    else:
      super().__init__(parent)
    self.setAttribute(Qt.WA_DeleteOnClose, True)
    self._embed_fullscreen = False
    self._sftp = sftp
    self._ssh = ssh
    self._remote = remote_path
    self._kind = kind
    self._sudo_user = sudo_user
    self._stream_server = None
    self._start_worker = None
    self._transcode_worker = None
    self._temp_media_path = None
    self._fallback_attempted = False
    self._seeking = False
    self._was_fullscreen = False

    fname = os.path.basename(remote_path)
    self.setWindowTitle("Play - {}".format(fname))
    self.resize(920, 620 if kind == "video" else 300)
    apply_qss_to(self)

    lay = QVBoxLayout(self)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    # Header
    header = QWidget()
    header.setFixedHeight(40)
    header.setStyleSheet(
      "background: {bg}; border-bottom: 1px solid {bd};".format(
        bg=T['BG_PANEL'], bd=T['BORDER'])
    )
    h = QHBoxLayout(header)
    h.setContentsMargins(12, 0, 12, 0)
    name_lbl = QLabel(fname)
    name_lbl.setStyleSheet(
      "color: {}; font-size: 13px; font-weight: 600;".format(T['TEXT_PRIMARY']))
    h.addWidget(name_lbl, 1)

    self._fullscreen_btn = QPushButton()
    self._fullscreen_btn.setFixedSize(30, 28)
    self._fullscreen_btn.setToolTip("Fullscreen (F)")
    set_icon(self._fullscreen_btn, "fullscreen", color=T['TEXT_PRIMARY'], size=15)
    self._fullscreen_btn.clicked.connect(self._toggle_fullscreen)
    h.addWidget(self._fullscreen_btn)
    if self._embedded:
      self._close_btn = QPushButton("\u2715")
      self._close_btn.setFixedSize(30, 28)
      self._close_btn.setToolTip("Close (Esc)")
      self._close_btn.setCursor(Qt.PointingHandCursor)
      self._close_btn.clicked.connect(self.close)
      h.addWidget(self._close_btn)
    lay.addWidget(header)

    # Media surface
    body = QWidget()
    body.setStyleSheet(
      "background: #000000;" if kind == "video"
      else "background: {};".format(T['BG_DARK'])
    )
    b_lay = QVBoxLayout(body)
    b_lay.setContentsMargins(0, 0, 0, 0)

    self._video_widget = None
    self._audio_icon = None
    if _MULTIMEDIA_AVAILABLE and kind == "video":
      self._video_widget = QVideoWidget()
      b_lay.addWidget(self._video_widget, 1)
    else:
      audio_wrap = QWidget()
      audio_lay = QVBoxLayout(audio_wrap)
      audio_lay.setContentsMargins(20, 20, 20, 20)
      audio_lay.setSpacing(10)
      self._audio_icon = QLabel()
      self._audio_icon.setPixmap(icon_pixmap(
        "audio" if kind == "audio" else "warning",
        color=T['ACCENT'], size=72))
      self._audio_icon.setAlignment(Qt.AlignCenter)
      audio_lay.addWidget(self._audio_icon, 1)
      if kind == "audio":
        type_lbl = QLabel("AUDIO")
        type_lbl.setAlignment(Qt.AlignCenter)
        type_lbl.setStyleSheet(
          "color: {dim}; font-size: 11px; font-weight: 700; letter-spacing: 2px;"
          .format(dim=T['TEXT_DIM']))
        audio_lay.addWidget(type_lbl)
      b_lay.addWidget(audio_wrap, 1)
    lay.addWidget(body, 1)

    # Status
    self._status_lbl = QLabel("Preparing...")
    self._status_lbl.setStyleSheet(
      "color: {}; font-size: 12px; padding: 6px 12px;".format(T['TEXT_DIM']))
    self._status_lbl.setWordWrap(True)
    lay.addWidget(self._status_lbl)

    self._dl_bar = QProgressBar()
    self._dl_bar.setRange(0, 0)
    self._dl_bar.setFixedHeight(4)
    self._dl_bar.setTextVisible(False)
    lay.addWidget(self._dl_bar)

    # Transport
    controls = QWidget()
    controls.setStyleSheet(
      "background: {bg}; border-top: 1px solid {bd};".format(
        bg=T['BG_PANEL'], bd=T['BORDER']))
    c = QVBoxLayout(controls)
    c.setContentsMargins(12, 6, 12, 7)
    c.setSpacing(5)

    seek_row = QHBoxLayout()
    self._pos_lbl = QLabel("0:00")
    self._pos_lbl.setFixedWidth(46)
    self._pos_lbl.setStyleSheet("color: {}; font-size: 11px;".format(T['TEXT_DIM']))
    seek_row.addWidget(self._pos_lbl)

    self._seek_slider = QSlider(Qt.Horizontal)
    self._seek_slider.setRange(0, 0)
    self._seek_slider.setToolTip("Seek")
    self._seek_slider.sliderPressed.connect(self._on_seek_start)
    self._seek_slider.sliderReleased.connect(self._on_seek_end)
    seek_row.addWidget(self._seek_slider, 1)

    self._dur_lbl = QLabel("0:00")
    self._dur_lbl.setFixedWidth(46)
    self._dur_lbl.setAlignment(Qt.AlignRight)
    self._dur_lbl.setStyleSheet("color: {}; font-size: 11px;".format(T['TEXT_DIM']))
    seek_row.addWidget(self._dur_lbl)
    c.addLayout(seek_row)

    btn_row = QHBoxLayout()
    btn_row.setContentsMargins(0, 0, 0, 0)
    btn_row.setSpacing(7)

    def _mkbtn(tooltip, icon_name=None, width=36, primary=False):
      b = QPushButton()
      b.setFixedSize(width, 34)
      b.setToolTip(tooltip)
      b.setCursor(Qt.PointingHandCursor)
      if primary:
        b.setObjectName("primary")
      if icon_name:
        set_icon(b, icon_name, color=T['TEXT_PRIMARY'], size=16)
      return b

    self._back_btn = _mkbtn("Back 10 seconds (←)", "back")
    self._back_btn.clicked.connect(lambda: self._skip(-10000))
    btn_row.addWidget(self._back_btn)

    self._play_btn = _mkbtn("Play / Pause (Space)", "play", width=42, primary=True)
    self._play_btn.clicked.connect(self._toggle_play)
    btn_row.addWidget(self._play_btn)

    self._fwd_btn = _mkbtn("Forward 10 seconds (→)", "forward")
    self._fwd_btn.clicked.connect(lambda: self._skip(10000))
    btn_row.addWidget(self._fwd_btn)

    self._stop_btn = _mkbtn("Stop", "stop")
    self._stop_btn.clicked.connect(self._stop)
    btn_row.addWidget(self._stop_btn)

    btn_row.addSpacing(5)

    self._mute_btn = _mkbtn("Mute (M)", "volume_high")
    self._mute_btn.clicked.connect(self._toggle_mute)
    btn_row.addWidget(self._mute_btn)

    self._vol_slider = QSlider(Qt.Horizontal)
    self._vol_slider.setFixedWidth(90)
    self._vol_slider.setRange(0, 100)
    self._vol_slider.setValue(80)
    self._vol_slider.setToolTip("Volume (↑/↓)")
    btn_row.addWidget(self._vol_slider)

    btn_row.addSpacing(5)

    speed_lbl = QLabel("Speed")
    speed_lbl.setStyleSheet("color: {}; font-size: 11px;".format(T['TEXT_DIM']))
    btn_row.addWidget(speed_lbl)

    self._speed_combo = QComboBox()
    self._speed_combo.setFixedWidth(72)
    self._speed_combo.addItems(["0.25x", "0.5x", "0.75x", "1x", "1.25x", "1.5x", "2x"])
    self._speed_combo.setCurrentText("1x")
    self._speed_combo.setToolTip("Playback speed")
    self._speed_combo.currentTextChanged.connect(self._set_playback_speed)
    btn_row.addWidget(self._speed_combo)

    self._repeat_check = QCheckBox("Repeat")
    self._repeat_check.setToolTip("Repeat this file when it reaches the end")
    btn_row.addWidget(self._repeat_check)

    btn_row.addStretch(1)

    self._snapshot_btn = _mkbtn("Save video snapshot", "camera")
    self._snapshot_btn.clicked.connect(self._save_snapshot)
    self._snapshot_btn.setVisible(kind == "video")
    btn_row.addWidget(self._snapshot_btn)

    self._aspect_combo = QComboBox()
    self._aspect_combo.setFixedWidth(86)
    self._aspect_combo.addItems(["Fit", "Fill", "Original"])
    self._aspect_combo.setToolTip("Video aspect ratio")
    self._aspect_combo.currentTextChanged.connect(self._set_aspect_ratio)
    self._aspect_combo.setVisible(kind == "video")
    btn_row.addWidget(self._aspect_combo)

    self._info_btn = _mkbtn("Media information (I)", "info")
    self._info_btn.clicked.connect(self._show_media_info)
    btn_row.addWidget(self._info_btn)

    c.addLayout(btn_row)
    lay.addWidget(controls)

    self._player = None
    if _MULTIMEDIA_AVAILABLE:
      self._player = QMediaPlayer(self)
      if self._video_widget is not None:
        self._player.setVideoOutput(self._video_widget)
      self._player.setVolume(80)
      self._player.positionChanged.connect(self._on_position_changed)
      self._player.durationChanged.connect(self._on_duration_changed)
      self._player.stateChanged.connect(self._on_state_changed)
      self._player.bufferStatusChanged.connect(self._on_buffer_status)
      self._player.mediaStatusChanged.connect(self._on_media_status)
      self._player.error.connect(self._on_player_error)
      self._vol_slider.valueChanged.connect(self._player.setVolume)
      try:
        self._player.setPlaybackRate(1.0)
      except Exception:
        pass

    if not _MULTIMEDIA_AVAILABLE:
      self._status_lbl.setText(
        "Media playback isn't available - this Qt install is missing "
        "QtMultimedia. You can still download the file instead.")
      self._status_lbl.setStyleSheet(
        "color: {}; font-size: 12px; padding: 6px 12px;".format(T['WARNING']))
      self._dl_bar.hide()

    self._set_controls_enabled(False)
    if kind == "video":
      self._set_aspect_ratio("Fit")

    if self._embedded:
      self._place()

    if _MULTIMEDIA_AVAILABLE:
      self._start_stream()

  # ── Embedded-overlay helpers ───────────────────────────────────────
  def _is_fs(self):
    return self._embed_fullscreen if self._embedded else self.isFullScreen()

  def _place(self):
    """Centre the player in the host window (or fill it in fullscreen)."""
    if not self._embedded or self._host is None:
      return
    try:
      hr = self._host.rect()
      if self._embed_fullscreen:
        self.setGeometry(hr)
      else:
        w = min(920, max(480, hr.width() - 40))
        hgt = min(620 if self._kind == "video" else 300, max(240, hr.height() - 40))
        self.setGeometry((hr.width() - w) // 2, (hr.height() - hgt) // 2, w, hgt)
      self.raise_()
    except RuntimeError:
      pass

  def eventFilter(self, obj, event):
    if obj is self._host and event.type() == QEvent.Resize:
      self._place()
    return False

  def _start_stream(self):
    self._status_lbl.setText("Connecting...")
    self._stream_server = MediaStreamServer(
      self._ssh, self._sftp, self._remote, sudo_user=self._sudo_user)
    self._start_worker = _StreamServerStartWorker(self._stream_server)
    self._start_worker.ready.connect(self._on_stream_ready)
    self._start_worker.error.connect(self._on_stream_error)
    self._start_worker.start()

  def _on_stream_ready(self, url):
    try:
      if self._stream_server.supports_range:
        self._status_lbl.setText("Streaming")
      else:
        self._status_lbl.setText("Streaming (seeking unavailable for this source)")
      self._dl_bar.hide()
      self._set_controls_enabled(True)
      if self._player:
        self._player.setMedia(QMediaContent(QUrl(url)))
        self._player.play()
    except RuntimeError:
      pass

  def _on_stream_error(self, msg):
    try:
      self._dl_bar.hide()
      self._status_lbl.setText("Streaming failed: {}".format(msg))
      self._status_lbl.setStyleSheet(
        "color: {}; font-size: 12px; padding: 6px 12px;".format(T['DANGER']))
    except RuntimeError:
      pass

  def _on_buffer_status(self, percent):
    try:
      if percent < 100:
        self._dl_bar.show()
        self._dl_bar.setRange(0, 100)
        self._dl_bar.setValue(percent)
        self._status_lbl.setText("Buffering... {}%".format(percent))
      else:
        self._dl_bar.hide()
        if self._stream_server and self._stream_server.supports_range:
          self._status_lbl.setText("Streaming")
    except RuntimeError:
      pass

  def _on_media_status(self, status):
    if not self._player:
      return
    try:
      if status == QMediaPlayer.EndOfMedia:
        if self._repeat_check.isChecked():
          self._player.setPosition(0)
          self._player.play()
        else:
          self._status_lbl.setText("Finished")
    except RuntimeError:
      pass

  def _set_controls_enabled(self, enabled: bool):
    for w in (
      self._back_btn, self._play_btn, self._fwd_btn, self._stop_btn,
      self._mute_btn, self._vol_slider, self._seek_slider,
      self._speed_combo, self._repeat_check, self._info_btn
    ):
      w.setEnabled(enabled)
    self._fullscreen_btn.setEnabled(enabled)
    self._snapshot_btn.setEnabled(enabled and self._kind == "video")
    self._aspect_combo.setEnabled(enabled and self._kind == "video")

  def _toggle_play(self):
    if not self._player:
      return
    if self._player.state() == QMediaPlayer.PlayingState:
      self._player.pause()
    else:
      self._player.play()

  def _stop(self):
    if self._player:
      self._player.stop()
      self._player.setPosition(0)

  def _skip(self, delta_ms: int):
    if not self._player:
      return
    duration = max(0, self._player.duration())
    new_pos = max(0, min(duration, self._player.position() + delta_ms))
    self._player.setPosition(new_pos)

  def _toggle_mute(self):
    if not self._player:
      return
    muted = not self._player.isMuted()
    self._player.setMuted(muted)
    set_icon(self._mute_btn, "volume_mute" if muted else "volume_high", size=16)

  def _set_playback_speed(self, text):
    if not self._player:
      return
    try:
      rate = float(text.rstrip("x"))
      self._player.setPlaybackRate(rate)
      self._status_lbl.setText("Playing at {}x".format(text))
    except Exception:
      pass

  def _set_aspect_ratio(self, mode):
    if not self._video_widget:
      return
    mapping = {
      "Fit": Qt.KeepAspectRatio,
      "Fill": Qt.KeepAspectRatioByExpanding,
      "Original": Qt.IgnoreAspectRatio,
    }
    try:
      self._video_widget.setAspectRatioMode(mapping.get(mode, Qt.KeepAspectRatio))
    except Exception:
      pass

  def _toggle_fullscreen(self):
    if self._is_fs():
      if self._embedded:
        self._embed_fullscreen = False
        self._place()
      else:
        self.showNormal()
      self._was_fullscreen = False
      set_icon(self._fullscreen_btn, "fullscreen", color=T['TEXT_PRIMARY'], size=15)
      self._fullscreen_btn.setToolTip("Fullscreen (F)")
    else:
      if self._embedded:
        self._embed_fullscreen = True
        self._place()
      else:
        self.showFullScreen()
      self._was_fullscreen = True
      set_icon(self._fullscreen_btn, "fullscreen_exit", color=T['TEXT_PRIMARY'], size=15)
      self._fullscreen_btn.setToolTip("Exit fullscreen (F)")

  def _save_snapshot(self):
    if self._kind != "video" or not self._video_widget:
      return
    try:
      pixmap = self._video_widget.grab()
      if pixmap.isNull():
        QMessageBox.information(self, "Snapshot", "No video frame is available yet.")
        return
      default_name = os.path.splitext(os.path.basename(self._remote))[0] + "-snapshot.png"
      path, _ = QFileDialog.getSaveFileName(
        self, "Save Video Snapshot", default_name,
        "PNG Image (*.png);;JPEG Image (*.jpg *.jpeg)")
      if path:
        if not pixmap.save(path):
          QMessageBox.warning(self, "Snapshot", "Could not save the video frame.")
    except Exception as e:
      QMessageBox.warning(self, "Snapshot", "Could not capture frame: {}".format(e))

  def _show_media_info(self):
    if not self._player:
      return
    try:
      duration = self._fmt_time(max(0, self._player.duration()))
      position = self._fmt_time(max(0, self._player.position()))
      rate = self._speed_combo.currentText()
      muted = "Yes" if self._player.isMuted() else "No"
      volume = self._player.volume()
      source = "SFTP/SSH" if not hasattr(self._sftp, "_ftp") else "FTP/FTPS"
      seek = "Available" if self._stream_server and self._stream_server.supports_range else "Unavailable"
      lines = [
        "File: {}".format(os.path.basename(self._remote)),
        "Type: {}".format("Video" if self._kind == "video" else "Audio"),
        "Remote path: {}".format(self._remote),
        "Transport: {}".format(source),
        "Duration: {}".format(duration),
        "Position: {}".format(position),
        "Playback speed: {}".format(rate),
        "Volume: {}%".format(volume),
        "Muted: {}".format(muted),
        "Remote seeking: {}".format(seek),
        "Repeat: {}".format("On" if self._repeat_check.isChecked() else "Off"),
        "Decoder: {}".format(
          "FFmpeg fallback" if self._fallback_attempted else "QtMultimedia"
        ),
      ]
      QMessageBox.information(self, "Media Information", "\n".join(lines))
    except RuntimeError:
      pass

  @staticmethod
  def _fmt_time(ms: int) -> str:
    """Format Qt media milliseconds as H:MM:SS or M:SS."""
    total_seconds = max(0, int(ms)) // 1000
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
      return "{}:{:02d}:{:02d}".format(hours, minutes, seconds)
    return "{}:{:02d}".format(minutes, seconds)

  def _on_seek_start(self):
    self._seeking = True

  def _on_seek_end(self):
    if self._player:
      self._player.setPosition(self._seek_slider.value())
    self._seeking = False

  def _on_position_changed(self, pos):
    if not self._seeking:
      self._seek_slider.setValue(pos)
    self._pos_lbl.setText(self._fmt_time(pos))

  def _on_duration_changed(self, dur):
    self._seek_slider.setRange(0, max(0, dur))
    self._dur_lbl.setText(self._fmt_time(max(0, dur)))

  def _on_state_changed(self, state):
    if state == QMediaPlayer.PlayingState:
      set_icon(self._play_btn, "pause", color=T['TEXT_PRIMARY'], size=17)
      self._play_btn.setToolTip("Pause (Space)")
    else:
      set_icon(self._play_btn, "play", color=T['TEXT_PRIMARY'], size=17)
      self._play_btn.setToolTip("Play / Pause (Space)")

  def _find_ffmpeg(self):
    candidates = [
      shutil.which("ffmpeg"),
      "/opt/homebrew/bin/ffmpeg",
      "/usr/local/bin/ffmpeg",
      os.path.join(getattr(sys, "_MEIPASS", ""), "ffmpeg"),
      os.path.join(getattr(sys, "_MEIPASS", ""), "bin", "ffmpeg"),
      os.path.join(os.path.dirname(sys.executable), "ffmpeg"),
      os.path.join(os.path.dirname(sys.executable), "bin", "ffmpeg"),
    ]
    for path in candidates:
      if path and os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return None

  def _start_video_fallback(self):
    if self._fallback_attempted or self._kind != "video" or not self._stream_server:
      return
    self._fallback_attempted = True
    ffmpeg = self._find_ffmpeg()
    if not ffmpeg:
      self._on_stream_error(
        "This video codec/container isn't supported by QtMultimedia and ffmpeg "
        "wasn't found. Install ffmpeg and reopen the file (macOS: brew install ffmpeg).")
      return
    try:
      if self._player:
        self._player.stop()
        self._player.setMedia(QMediaContent())
      fd, path = tempfile.mkstemp(prefix="kubedeck-video-", suffix=".mp4")
      os.close(fd)
      self._temp_media_path = path
      self._status_lbl.setText("Converting video for playback compatibility...")
      self._status_lbl.setStyleSheet(
        "color: {}; font-size: 12px; padding: 6px 12px;".format(T['WARNING']))
      self._dl_bar.show()
      self._dl_bar.setRange(0, 0)
      self._set_controls_enabled(False)
      worker = VideoTranscodeWorker(self._stream_server.url, path, ffmpeg)
      worker.ready.connect(self._on_video_fallback_ready)
      worker.error.connect(self._on_video_fallback_error)
      worker.finished.connect(lambda w=worker: self._clear_transcode_worker(w))
      self._transcode_worker = worker
      worker.start()
    except Exception as e:
      self._on_video_fallback_error(str(e))

  def _on_video_fallback_ready(self, path):
    try:
      if not self._player:
        return
      self._dl_bar.hide()
      self._status_lbl.setText("Playing (decoded for compatibility)")
      self._status_lbl.setStyleSheet(
        "color: {}; font-size: 12px; padding: 6px 12px;".format(T['TEXT_DIM']))
      self._set_controls_enabled(True)
      self._player.setMedia(QMediaContent(QUrl.fromLocalFile(path)))
      self._player.play()
    except RuntimeError:
      pass

  def _on_video_fallback_error(self, msg):
    try:
      self._dl_bar.hide()
      self._status_lbl.setText("Video playback failed: {}".format(msg))
      self._status_lbl.setStyleSheet(
        "color: {}; font-size: 12px; padding: 6px 12px;".format(T['DANGER']))
      self._set_controls_enabled(False)
    except RuntimeError:
      pass

  def _start_audio_fallback(self):
    if self._fallback_attempted or self._kind != "audio" or not self._stream_server:
      return
    self._fallback_attempted = True
    ffmpeg = self._find_ffmpeg()
    if not ffmpeg:
      self._on_stream_error(
        "This audio codec isn't supported by QtMultimedia. "
        "Install ffmpeg and reopen the file (macOS: brew install ffmpeg).")
      return
    try:
      fd, path = tempfile.mkstemp(prefix="kubedeck-audio-", suffix=".wav")
      os.close(fd)
      self._temp_media_path = path
      self._status_lbl.setText("Converting audio for playback...")
      self._status_lbl.setStyleSheet(
        "color: {}; font-size: 12px; padding: 6px 12px;".format(T['WARNING']))
      self._dl_bar.show()
      self._dl_bar.setRange(0, 0)
      self._set_controls_enabled(False)
      worker = AudioTranscodeWorker(self._stream_server.url, path, ffmpeg)
      worker.ready.connect(self._on_audio_fallback_ready)
      worker.error.connect(self._on_audio_fallback_error)
      worker.finished.connect(lambda w=worker: self._clear_transcode_worker(w))
      self._transcode_worker = worker
      worker.start()
    except Exception as e:
      self._on_audio_fallback_error(str(e))

  def _on_audio_fallback_ready(self, path):
    try:
      if not self._player:
        return
      self._dl_bar.hide()
      self._status_lbl.setText("Playing (decoded for compatibility)")
      self._set_controls_enabled(True)
      self._player.setMedia(QMediaContent(QUrl.fromLocalFile(path)))
      self._player.play()
    except RuntimeError:
      pass

  def _on_audio_fallback_error(self, msg):
    try:
      self._dl_bar.hide()
      self._status_lbl.setText("Audio playback failed: {}".format(msg))
      self._status_lbl.setStyleSheet(
        "color: {}; font-size: 12px; padding: 6px 12px;".format(T['DANGER']))
      self._set_controls_enabled(False)
    except RuntimeError:
      pass

  def _clear_transcode_worker(self, worker):
    if self._transcode_worker is worker:
      self._transcode_worker = None

  def _on_player_error(self, _err):
    if not self._player:
      return
    if self._kind == "video" and not self._fallback_attempted:
      self._start_video_fallback()
      return
    if self._kind == "audio" and not self._fallback_attempted:
      self._start_audio_fallback()
      return
    try:
      msg = self._player.errorString() or "Unsupported or unreadable media"
      self._status_lbl.setText("Playback error: {}".format(msg))
      self._status_lbl.setStyleSheet(
        "color: {}; font-size: 12px; padding: 6px 12px;".format(T['DANGER']))
      self._set_controls_enabled(False)
    except RuntimeError:
      pass

  def keyPressEvent(self, event):
    key = event.key()
    mods = event.modifiers()

    if key == Qt.Key_Space:
      self._toggle_play()
      event.accept()
      return
    if key == Qt.Key_F:
      self._toggle_fullscreen()
      event.accept()
      return
    if key == Qt.Key_M:
      self._toggle_mute()
      event.accept()
      return
    if key == Qt.Key_I:
      self._show_media_info()
      event.accept()
      return
    if key == Qt.Key_Escape and self._is_fs():
      self._toggle_fullscreen()
      event.accept()
      return
    if key == Qt.Key_Escape and self._embedded:
      self.close()
      event.accept()
      return
    if key == Qt.Key_Left:
      self._skip(-30000 if mods & Qt.ShiftModifier else -10000)
      event.accept()
      return
    if key == Qt.Key_Right:
      self._skip(30000 if mods & Qt.ShiftModifier else 10000)
      event.accept()
      return
    if key == Qt.Key_Up:
      if self._player:
        self._vol_slider.setValue(min(100, self._vol_slider.value() + 5))
      event.accept()
      return
    if key == Qt.Key_Down:
      if self._player:
        self._vol_slider.setValue(max(0, self._vol_slider.value() - 5))
      event.accept()
      return
    if key == Qt.Key_S:
      if self._kind == "video":
        self._save_snapshot()
        event.accept()
        return
    super().keyPressEvent(event)

  def closeEvent(self, event):
    if self._embedded and self._host is not None:
      try:
        self._host.removeEventFilter(self)
      except RuntimeError:
        pass
    if self._player:
      for sig, slot in (
        (self._player.positionChanged, self._on_position_changed),
        (self._player.durationChanged, self._on_duration_changed),
        (self._player.stateChanged, self._on_state_changed),
        (self._player.bufferStatusChanged, self._on_buffer_status),
        (self._player.mediaStatusChanged, self._on_media_status),
        (self._player.error, self._on_player_error),
      ):
        try:
          sig.disconnect(slot)
        except Exception:
          pass
      self._player.stop()
      self._player.setMedia(QMediaContent())

    worker = self._start_worker
    self._start_worker = None
    if worker is not None:
      try:
        try:
          worker.ready.disconnect(self._on_stream_ready)
        except (TypeError, RuntimeError):
          pass
        try:
          worker.error.disconnect(self._on_stream_error)
        except (TypeError, RuntimeError):
          pass
        try:
          worker.requestInterruption()
        except RuntimeError:
          worker = None
        if worker is not None:
          try:
            worker.wait(1500)
          except RuntimeError:
            pass
      except RuntimeError:
        pass

    if self._transcode_worker is not None:
      try:
        self._transcode_worker.stop()
      except Exception:
        pass
      self._transcode_worker = None

    if self._stream_server:
      try:
        self._stream_server.stop()
      except Exception:
        pass

    if self._temp_media_path:
      try:
        os.unlink(self._temp_media_path)
      except OSError:
        pass
      self._temp_media_path = None

    event.accept()

  def reject(self):
    # reject() only hides a QDialog and skips closeEvent, which would leave
    # the stream server / player running. Route it through close().
    self.close()

  @classmethod
  def open_remote(cls, parent, sftp, ssh, remote_path: str, kind: str, sudo_user=None):
    dlg = cls(parent, sftp, ssh, remote_path, kind, sudo_user=sudo_user)
    if not dlg._embedded:
      dlg.exec_()
      return dlg
    cls._open_players.add(dlg)
    dlg.destroyed.connect(lambda *_a, d=dlg: cls._open_players.discard(d))
    dlg.show()
    dlg._place()
    dlg.setFocus()
    return dlg