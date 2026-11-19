import os
import re
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QSize
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QSplitter, QTextEdit, QComboBox, QSpinBox,
    QCheckBox, QGroupBox, QProgressBar, QMessageBox, QDialog,
    QDialogButtonBox, QFormLayout, QFrame, QAbstractItemView, QInputDialog,
    QRadioButton, QButtonGroup, QSizePolicy
)

from subduck.models import VideoFileInfo, CensorConfig
from subduck.sub_parser import extract_subtitles_from_video, probe_video
from subduck.matcher import scan_subtitles_for_triggers
from subduck.processor import BatchProcessWorker, get_default_season_mirror_dir
from subduck.profiles import load_all_profiles, save_user_profile, delete_user_profile
from subduck.kio_resolver import resolve_network_url, get_kio_fuse_smb_root, ensure_kio_fuse_running
from subduck.whisper_engine import transcribe_video_to_subtitles, is_whisper_available
from subduck.power_manager import SleepInhibitor

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".m4v"}


def _make_btn(label: str, callback, tooltip: str = "") -> QPushButton:
    btn = QPushButton(label)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.clicked.connect(callback)
    return btn


def _make_chk(label: str, checked: bool = False, tooltip: str = "") -> QCheckBox:
    chk = QCheckBox(label)
    chk.setChecked(checked)
    if tooltip:
        chk.setToolTip(tooltip)
    return chk


class ResizablePanel(QWidget):
    def minimumSizeHint(self) -> QSize:
        return QSize(80, 80)


class FolderScanWorker(QThread):
    progress = Signal(int, int, str)
    file_found = Signal(object)
    scan_finished = Signal(list)

    def __init__(self, folder: Path, config: CensorConfig):
        super().__init__()
        self.folder = folder
        self.config = config
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        video_files = []
        for root, _, files in os.walk(self.folder):
            for f in sorted(files):
                p = Path(root) / f
                if p.suffix.lower() in VIDEO_EXTENSIONS:
                    video_files.append(p)

        total = len(video_files)
        results = []

        for i, v_path in enumerate(video_files, 1):
            if self._is_cancelled:
                break

            self.progress.emit(i, total, v_path.name)

            file_size = v_path.stat().st_size if v_path.exists() else 0
            probe_meta = probe_video(v_path)
            duration = float(probe_meta.get("format", {}).get("duration", 0.0) or 0.0)

            info = VideoFileInfo(
                path=v_path,
                filename=v_path.name,
                file_size=file_size,
                duration_sec=duration
            )

            sub_entries, sub_desc, has_bitmap, bitmap_codec = extract_subtitles_from_video(
                v_path, self.config.subtitle_lang
            )
            info.subtitle_entries = sub_entries
            info.subtitle_source = sub_desc
            info.has_subtitles = len(sub_entries) > 0
            info.has_bitmap_subtitles = has_bitmap
            info.bitmap_codec = bitmap_codec

            if info.has_subtitles and self.config.trigger_words:
                matches = scan_subtitles_for_triggers(sub_entries, self.config)
                info.matches = matches
                info.status = f"Flagged ({info.trigger_count})" if matches else "Clean"
            elif info.has_subtitles:
                info.status = "Clean"
            elif info.has_bitmap_subtitles:
                info.status = f"Bitmap ({bitmap_codec})"
            else:
                info.status = "No Subtitles"

            results.append(info)
            self.file_found.emit(info)

        self.scan_finished.emit(results)


class WhisperTranscribeWorker(QThread):
    file_started = Signal(str, int, int)
    file_progress = Signal(str, int, float, float)
    file_completed = Signal(object, bool, str, int, int)
    all_completed = Signal(int, int)
    log_message = Signal(str)

    def __init__(self, target_items: list[VideoFileInfo], config: CensorConfig):
        super().__init__()
        self.target_items = target_items
        self.config = config
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        total = len(self.target_items)
        succeeded = 0
        failed = 0

        for idx, item in enumerate(self.target_items, 1):
            if self._is_cancelled:
                self.log_message.emit("Transcription cancelled by user.")
                break

            self.file_started.emit(item.filename, idx, total)
            self.log_message.emit(
                f"Transcribing ({idx}/{total}): {item.filename} with Whisper ({self.config.whisper_model})..."
            )

            try:
                def prog_cb(pct, cur_s, tot_s, fname=item.filename):
                    self.file_progress.emit(fname, pct, cur_s, tot_s)

                entries, raw_srt = transcribe_video_to_subtitles(
                    item.path,
                    model_size=self.config.whisper_model,
                    device=self.config.whisper_device,
                    progress_callback=prog_cb,
                    cancel_check=lambda: self._is_cancelled,
                    log_callback=self.log_message.emit
                )

                if self._is_cancelled:
                    break

                item.subtitle_entries = entries
                item.has_subtitles = len(entries) > 0
                item.whisper_generated = True
                item.subtitle_source = f"Whisper ({self.config.whisper_model})"

                if self.config.whisper_save_to_source and raw_srt:
                    src_srt = item.path.parent / f"{item.path.stem}.srt"
                    if not src_srt.exists():
                        src_srt.write_text(raw_srt, encoding="utf-8")
                        self.log_message.emit(f"Saved generated subtitle to: {src_srt.name}")

                if self.config.trigger_words:
                    item.matches = scan_subtitles_for_triggers(entries, self.config)
                    item.status = f"Flagged ({item.trigger_count})" if item.matches else "Clean"
                else:
                    item.status = "Clean"

                succeeded += 1
                self.file_completed.emit(item, True, "Transcription complete", idx, total)

            except RuntimeError as e:
                failed += 1
                self.log_message.emit(f"Error transcribing {item.filename}: {e}")
                self.file_completed.emit(item, False, str(e), idx, total)

        self.all_completed.emit(succeeded, failed)


class TriggerEditorDialog(QDialog):
    def __init__(self, current_words: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure Trigger Words")
        self.resize(520, 380)
        self.words = list(current_words)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        notice_box = QFrame()
        notice_box.setFrameShape(QFrame.StyledPanel)
        nb_layout = QVBoxLayout(notice_box)
        nb_layout.setContentsMargins(8, 6, 8, 6)
        notice_lbl = QLabel(
            "<b>Sensitive Content Notice</b><br>"
            "This dialog lists the trigger words that SubDuck will attenuate in audio and redact in subtitles. "
            "Trigger words remain hidden on the main window by default."
        )
        notice_lbl.setWordWrap(True)
        nb_layout.addWidget(notice_lbl)
        layout.addWidget(notice_box)

        self.chk_reveal = _make_chk("Reveal trigger words for editing", checked=True)
        self.chk_reveal.toggled.connect(self.on_toggle_reveal)
        layout.addWidget(self.chk_reveal)

        layout.addWidget(QLabel("Trigger words (comma or newline separated):"))

        self.edit_words = QTextEdit()
        self.edit_words.setPlaceholderText("Enter words or phrases, separated by commas or newlines...")
        self.edit_words.setPlainText(", ".join(self.words))
        layout.addWidget(self.edit_words)

        self.lbl_masked = QLabel("<i>[Trigger list hidden. Enable the checkbox above to view or edit.]</i>")
        self.lbl_masked.setAlignment(Qt.AlignCenter)
        self.lbl_masked.setVisible(False)
        layout.addWidget(self.lbl_masked)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def on_toggle_reveal(self, checked: bool):
        self.edit_words.setVisible(checked)
        self.lbl_masked.setVisible(not checked)

    def on_accept(self):
        raw = self.edit_words.toPlainText()
        seen = set()
        cleaned = []
        for token in re.split(r"[,\n\r]+", raw):
            w = token.strip().lower()
            if w and w not in seen:
                seen.add(w)
                cleaned.append(w)
        self.words = cleaned
        self.accept()

    def get_words(self) -> list[str]:
        return self.words


class SettingsDialog(QDialog):
    def __init__(self, config: CensorConfig, source_dir: Path | None = None, parent=None):
        super().__init__(parent)
        self.config = config
        self.source_dir = source_dir
        self.setWindowTitle("Output Modes & Processing Settings")
        self.setMinimumWidth(560)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        mode_group = QGroupBox("Output & Library Integration Mode")
        mode_layout = QVBoxLayout(mode_group)

        self.btn_group_mode = QButtonGroup(self)

        self.rb_season = QRadioButton("Mirror Complete Folder / Season to New Directory")
        self.btn_group_mode.addButton(self.rb_season, 0)
        mode_layout.addWidget(self.rb_season)

        self.season_subframe = QFrame()
        season_form = QFormLayout(self.season_subframe)
        season_form.setContentsMargins(20, 2, 0, 8)

        dest_h = QHBoxLayout()
        self.default_mirror = str(get_default_season_mirror_dir(self.source_dir)) if self.source_dir else ""
        self.edit_mirror_dir = QLineEdit(self.config.season_mirror_folder)
        self.edit_mirror_dir.setPlaceholderText(self.default_mirror or "Auto: Series [Trigger-Clean]/Season X")
        dest_h.addWidget(self.edit_mirror_dir)
        dest_h.addWidget(_make_btn("Browse...", self.browse_mirror))
        season_form.addRow("Target Mirror Folder:", dest_h)

        self.chk_copy_clean = _make_chk("Copy clean videos automatically", self.config.copy_clean_files)
        season_form.addRow(self.chk_copy_clean)

        self.chk_copy_art = _make_chk("Copy thumbnails, posters, and NFO metadata", self.config.copy_artwork)
        season_form.addRow(self.chk_copy_art)
        mode_layout.addWidget(self.season_subframe)

        self.rb_inplace = QRadioButton("Replace Original Files In-Place")
        self.btn_group_mode.addButton(self.rb_inplace, 1)
        mode_layout.addWidget(self.rb_inplace)

        self.inplace_subframe = QFrame()
        inplace_layout = QVBoxLayout(self.inplace_subframe)
        inplace_layout.setContentsMargins(20, 2, 0, 8)
        self.chk_backup = _make_chk(
            "Back up original video to '_originals/' subfolder before replacing",
            self.config.backup_originals
        )
        inplace_layout.addWidget(self.chk_backup)
        mode_layout.addWidget(self.inplace_subframe)

        self.rb_dual = QRadioButton("Dual-Audio Track in Single File (Original + Trigger-Muted Track)")
        self.btn_group_mode.addButton(self.rb_dual, 2)
        mode_layout.addWidget(self.rb_dual)

        self.rb_suffix = QRadioButton("Save Alongside with Suffix (e.g. '<Name> trigger.mkv')")
        self.btn_group_mode.addButton(self.rb_suffix, 3)
        mode_layout.addWidget(self.rb_suffix)

        self.suffix_subframe = QFrame()
        suffix_form = QFormLayout(self.suffix_subframe)
        suffix_form.setContentsMargins(20, 2, 0, 8)
        self.edit_suffix = QLineEdit(self.config.output_suffix)
        suffix_form.addRow("Filename Suffix:", self.edit_suffix)
        mode_layout.addWidget(self.suffix_subframe)

        mode_map = {
            "season_mirror": self.rb_season,
            "inplace_replace": self.rb_inplace,
            "dual_audio": self.rb_dual,
        }
        mode_map.get(self.config.output_mode, self.rb_suffix).setChecked(True)
        layout.addWidget(mode_group)

        audio_group = QGroupBox("Audio Ducking & Safety Buffers")
        form = QFormLayout(audio_group)

        self.spin_before = QSpinBox()
        self.spin_before.setRange(0, 3000)
        self.spin_before.setSingleStep(50)
        self.spin_before.setSuffix(" ms")
        self.spin_before.setValue(self.config.safety_before_ms)
        form.addRow("Lead-in Safety Buffer:", self.spin_before)

        self.spin_after = QSpinBox()
        self.spin_after.setRange(0, 3000)
        self.spin_after.setSingleStep(50)
        self.spin_after.setSuffix(" ms")
        self.spin_after.setValue(self.config.safety_after_ms)
        form.addRow("Lead-out Safety Buffer:", self.spin_after)

        self.combo_ducking = QComboBox()
        for label, val in (
            ("Complete Silence (Mute to 0%)", 0.0),
            ("Volume Ducking (-20 dB / 10% volume)", 0.1),
            ("Mild Ducking (-12 dB / 25% volume)", 0.25),
        ):
            self.combo_ducking.addItem(label, val)
        if abs(self.config.ducking_level - 0.1) < 0.01:
            self.combo_ducking.setCurrentIndex(1)
        elif abs(self.config.ducking_level - 0.25) < 0.01:
            self.combo_ducking.setCurrentIndex(2)
        form.addRow("Audio Ducking Level:", self.combo_ducking)

        self.combo_style = QComboBox()
        for label, key in (
            ("Full Asterisks (******)", "asterisks"),
            ("Inner Asterisks (c****r)", "inner_asterisks"),
            ("Censored Tag ([censored])", "tag"),
            ("Redacted Block (████)", "block"),
            ("Remove Word Completely", "remove"),
        ):
            self.combo_style.addItem(label, key)
        idx_style = self.combo_style.findData(self.config.censor_style)
        if idx_style >= 0:
            self.combo_style.setCurrentIndex(idx_style)
        form.addRow("Subtitle Redaction Style:", self.combo_style)

        self.chk_strict = _make_chk("Strict whole-word boundary (handles hyphenated compounds)", self.config.strict_matching)
        form.addRow("Word Matching:", self.chk_strict)

        self.chk_save_srt = _make_chk("Save external .srt subtitle file", self.config.save_srt)
        form.addRow("External Subtitles:", self.chk_save_srt)

        self.chk_embed_sub = _make_chk("Embed redacted subtitle track into output container", self.config.embed_sub)
        form.addRow("Embedded Subtitles:", self.chk_embed_sub)
        layout.addWidget(audio_group)

        whisper_group = QGroupBox("Speech-to-Text Subtitle Generation (faster-whisper)")
        w_form = QFormLayout(whisper_group)

        self.combo_whisper_mode = QComboBox()
        for label, key in (
            ("Generate if missing or bitmap-only", "if_missing"),
            ("Always generate with Whisper", "always"),
            ("Disabled (use existing text subtitles only)", "disabled"),
        ):
            self.combo_whisper_mode.addItem(label, key)
        w_mode_idx = self.combo_whisper_mode.findData(self.config.whisper_mode)
        if w_mode_idx >= 0:
            self.combo_whisper_mode.setCurrentIndex(w_mode_idx)
        w_form.addRow("Whisper Mode:", self.combo_whisper_mode)

        self.combo_whisper_model = QComboBox()
        for label, key in (
            ("base.en (~150 MB)", "base.en"),
            ("tiny.en (~75 MB)", "tiny.en"),
            ("small.en (~500 MB)", "small.en"),
            ("medium.en (~1.5 GB)", "medium.en"),
            ("large-v3 (~3 GB)", "large-v3"),
        ):
            self.combo_whisper_model.addItem(label, key)
        w_model_idx = self.combo_whisper_model.findData(self.config.whisper_model)
        if w_model_idx >= 0:
            self.combo_whisper_model.setCurrentIndex(w_model_idx)
        w_form.addRow("Whisper Model:", self.combo_whisper_model)

        self.combo_whisper_device = QComboBox()
        for label, key in (
            ("Auto-detect (CUDA GPU preferred)", "auto"),
            ("NVIDIA CUDA GPU (float16)", "cuda"),
            ("CPU (int8)", "cpu"),
        ):
            self.combo_whisper_device.addItem(label, key)
        w_dev_idx = self.combo_whisper_device.findData(self.config.whisper_device)
        if w_dev_idx >= 0:
            self.combo_whisper_device.setCurrentIndex(w_dev_idx)
        w_form.addRow("Inference Device:", self.combo_whisper_device)

        self.chk_whisper_source = _make_chk(
            "Save generated .srt alongside source video (if writable)",
            self.config.whisper_save_to_source
        )
        w_form.addRow("Save to Source Folder:", self.chk_whisper_source)

        self.chk_whisper_dest = _make_chk("Save generated .srt to output folder", self.config.whisper_save_srt)
        w_form.addRow("Save to Output Folder:", self.chk_whisper_dest)
        layout.addWidget(whisper_group)

        power_group = QGroupBox("System Power Management")
        p_layout = QVBoxLayout(power_group)
        self.chk_inhibit_sleep = _make_chk(
            "Inhibit system sleep and screen lock while batch jobs are running",
            self.config.inhibit_sleep
        )
        p_layout.addWidget(self.chk_inhibit_sleep)
        layout.addWidget(power_group)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def browse_mirror(self):
        start_dir = self.edit_mirror_dir.text() or self.default_mirror or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Select Season Mirror Target Directory", start_dir)
        if folder:
            self.edit_mirror_dir.setText(folder)

    def save_and_accept(self):
        if self.rb_season.isChecked():
            self.config.output_mode = "season_mirror"
        elif self.rb_inplace.isChecked():
            self.config.output_mode = "inplace_replace"
        elif self.rb_dual.isChecked():
            self.config.output_mode = "dual_audio"
        else:
            self.config.output_mode = "separate_suffix"

        entered_mirror = self.edit_mirror_dir.text().strip()
        self.config.season_mirror_folder = "" if entered_mirror == self.default_mirror else entered_mirror
        self.config.copy_clean_files = self.chk_copy_clean.isChecked()
        self.config.copy_artwork = self.chk_copy_art.isChecked()
        self.config.backup_originals = self.chk_backup.isChecked()
        self.config.output_suffix = self.edit_suffix.text()

        self.config.safety_before_ms = self.spin_before.value()
        self.config.safety_after_ms = self.spin_after.value()
        self.config.ducking_level = self.combo_ducking.currentData()
        self.config.censor_style = self.combo_style.currentData()
        self.config.strict_matching = self.chk_strict.isChecked()
        self.config.save_srt = self.chk_save_srt.isChecked()
        self.config.embed_sub = self.chk_embed_sub.isChecked()

        self.config.whisper_mode = self.combo_whisper_mode.currentData()
        self.config.whisper_model = self.combo_whisper_model.currentData()
        self.config.whisper_device = self.combo_whisper_device.currentData()
        self.config.whisper_save_to_source = self.chk_whisper_source.isChecked()
        self.config.whisper_save_srt = self.chk_whisper_dest.isChecked()
        self.config.inhibit_sleep = self.chk_inhibit_sleep.isChecked()
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SubDuck")
        self.resize(1220, 800)
        self.setAcceptDrops(True)

        self.config = CensorConfig()
        self.profiles = load_all_profiles()
        self.video_items: list[VideoFileInfo] = []
        self.resolved_folder_path = ""

        self.sleep_inhibitor = SleepInhibitor()
        self.scan_worker = None
        self.process_thread = None
        self.process_worker = None
        self.transcribe_thread = None
        self.transcribe_worker = None

        self._batch_current_file = ""
        self._batch_current_index = 0
        self._batch_total_items = 0

        ensure_kio_fuse_running()
        self.setup_ui()
        self.load_initial_profile()

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)

        top_group = QGroupBox("Video Directory")
        top_layout = QHBoxLayout(top_group)

        self.edit_folder = QLineEdit()
        self.edit_folder.setPlaceholderText("Select directory or paste network share URL (e.g. smb://nas.local/media/...)...")
        self.edit_folder.returnPressed.connect(self.start_scan)

        self.btn_browse = _make_btn("Browse...", self.browse_folder)
        self.btn_scan = _make_btn("Scan Folder", self.start_scan)

        top_layout.addWidget(self.edit_folder)
        top_layout.addWidget(self.btn_browse)
        top_layout.addWidget(self.btn_scan)
        root_layout.addWidget(top_group)

        trig_group = QGroupBox("Trigger Profile & Output Mode")
        trig_layout = QVBoxLayout(trig_group)
        trig_row1 = QHBoxLayout()

        trig_row1.addWidget(QLabel("Preset Profile:"))
        self.combo_profile = QComboBox()
        self.combo_profile.currentIndexChanged.connect(self.on_profile_changed)
        trig_row1.addWidget(self.combo_profile, 1)

        self.btn_save_profile = _make_btn("Save Preset...", self.save_current_profile)
        self.btn_del_profile = _make_btn("Delete", self.delete_current_profile)
        trig_row1.addWidget(self.btn_save_profile)
        trig_row1.addWidget(self.btn_del_profile)

        trig_row1.addSpacing(15)
        trig_row1.addWidget(QLabel("Output Mode:"))
        self.combo_mode = QComboBox()
        for label, key in (
            ("Complete Folder Mirror", "season_mirror"),
            ("In-Place Replace (with backup)", "inplace_replace"),
            ("Dual-Audio Track in Single File", "dual_audio"),
            ("Save Alongside with Suffix", "separate_suffix"),
        ):
            self.combo_mode.addItem(label, key)
        self.combo_mode.currentIndexChanged.connect(self.on_mode_combo_changed)
        trig_row1.addWidget(self.combo_mode, 1)

        self.btn_settings = _make_btn("Options...", self.open_settings)
        trig_row1.addWidget(self.btn_settings)
        trig_layout.addLayout(trig_row1)

        trig_row2 = QHBoxLayout()
        self.lbl_trigger_summary = QLabel("Active Triggers: 0 words configured (hidden)")
        trig_row2.addWidget(self.lbl_trigger_summary, 1)

        self.btn_edit_triggers = _make_btn(
            "Edit Trigger Words...",
            self.open_trigger_editor,
            "Open modal dialog to view or edit trigger words"
        )
        trig_row2.addWidget(self.btn_edit_triggers)
        trig_layout.addLayout(trig_row2)
        root_layout.addWidget(trig_group)

        self.lbl_stats = QLabel("No files loaded. Select a local or network folder, or drag and drop a folder into the window.")
        root_layout.addWidget(self.lbl_stats)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)

        table_container = ResizablePanel()
        t_layout = QVBoxLayout(table_container)
        t_layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Select", "Video File", "Subtitle Source", "Trigger Matches", "Status"])
        hdr = self.table.horizontalHeader()
        for col, mode in enumerate((
            QHeaderView.ResizeToContents,
            QHeaderView.Stretch,
            QHeaderView.ResizeToContents,
            QHeaderView.ResizeToContents,
            QHeaderView.ResizeToContents,
        )):
            hdr.setSectionResizeMode(col, mode)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.itemSelectionChanged.connect(self.on_table_row_selected)
        t_layout.addWidget(self.table)
        self.splitter.addWidget(table_container)

        insp_container = ResizablePanel()
        insp_layout = QVBoxLayout(insp_container)
        insp_layout.setContentsMargins(0, 0, 0, 0)

        insp_header = QHBoxLayout()
        self.lbl_insp_title = QLabel("Trigger Moments Inspector")
        self.lbl_insp_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        insp_header.addWidget(self.lbl_insp_title, 1)

        self.chk_reveal_inspector = _make_chk(
            "Reveal Uncensored",
            checked=False,
            tooltip="Show raw uncensored trigger words and subtitle lines"
        )
        self.chk_reveal_inspector.toggled.connect(self.on_table_row_selected)
        insp_header.addWidget(self.chk_reveal_inspector)
        insp_layout.addLayout(insp_header)

        self.insp_table = QTableWidget(0, 3)
        self.insp_table.setHorizontalHeaderLabels(["Timestamp", "Words", "Subtitle Line Preview"])
        ihdr = self.insp_table.horizontalHeader()
        ihdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        ihdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        ihdr.setSectionResizeMode(2, QHeaderView.Stretch)
        self.insp_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        insp_layout.addWidget(self.insp_table)

        self.splitter.addWidget(insp_container)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([700, 480])
        root_layout.addWidget(self.splitter, 1)

        bottom_layout = QHBoxLayout()
        self.btn_select_all = _make_btn("Select All", lambda: self._set_rows_checked(lambda _: True))
        self.btn_select_flagged = _make_btn("Select Flagged", lambda: self._set_rows_checked(lambda v: bool(v.matches)))
        self.btn_select_no_subs = _make_btn("Without Subtitles", lambda: self._set_rows_checked(lambda v: not v.has_subtitles))
        self.btn_select_with_subs = _make_btn("With Subtitles", lambda: self._set_rows_checked(lambda v: v.has_subtitles))
        self.btn_select_none = _make_btn("Clear Selection", lambda: self._set_rows_checked(lambda _: False))
        self.btn_transcribe = _make_btn(
            "Transcribe with Whisper",
            self.start_whisper_transcription,
            "Generate text subtitles from video audio using local faster-whisper"
        )

        for b in (
            self.btn_select_all,
            self.btn_select_flagged,
            self.btn_select_no_subs,
            self.btn_select_with_subs,
            self.btn_select_none,
            self.btn_transcribe,
        ):
            bottom_layout.addWidget(b)

        bottom_layout.addStretch()

        self.btn_process_selected = _make_btn(
            "Process Selected",
            lambda: self.start_batch_process(process_all=False),
            "Process only checked video files"
        )
        self.btn_process_folder = _make_btn(
            "Process Complete Folder",
            lambda: self.start_batch_process(process_all=True),
            "Process all videos in the current folder"
        )
        self.btn_stop = _make_btn("Stop Processing", self.stop_current_operation)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setVisible(False)

        bottom_layout.addWidget(self.btn_process_selected)
        bottom_layout.addWidget(self.btn_process_folder)
        bottom_layout.addWidget(self.btn_stop)
        root_layout.addLayout(bottom_layout)

        prog_layout = QVBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        prog_layout.addWidget(self.progress_bar)

        self.lbl_status = QLabel("Ready")
        prog_layout.addWidget(self.lbl_status)
        root_layout.addLayout(prog_layout)

    def _set_rows_checked(self, predicate):
        for row in range(self.table.rowCount()):
            if row < len(self.video_items):
                item = self.table.item(row, 0)
                if item:
                    item.setCheckState(Qt.Checked if predicate(self.video_items[row]) else Qt.Unchecked)

    def on_mode_combo_changed(self):
        mode = self.combo_mode.currentData()
        self.config.output_mode = mode
        labels = {
            "season_mirror": ("Process Selected (Mirror)", "Process Complete Folder"),
            "inplace_replace": ("Replace Selected In-Place", "Replace All in Folder"),
            "dual_audio": ("Create Dual-Audio (Selected)", "Create Dual-Audio for All"),
        }
        sel_text, all_text = labels.get(mode, ("Process Selected Videos", "Process All in Folder"))
        self.btn_process_selected.setText(sel_text)
        self.btn_process_folder.setText(all_text)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls:
                folder_path = urls[0].toLocalFile() if urls[0].isLocalFile() else urls[0].toString()
                self.edit_folder.setText(folder_path)
                self.start_scan()
                event.acceptProposedAction()

    def load_initial_profile(self):
        self.combo_profile.clear()
        for name in sorted(self.profiles.keys()):
            self.combo_profile.addItem(name)
        if "Medical & Illness Triggers" in self.profiles:
            self.combo_profile.setCurrentText("Medical & Illness Triggers")
        else:
            self.update_trigger_summary_label()

    def on_profile_changed(self):
        name = self.combo_profile.currentText()
        if name in self.profiles:
            p_data = self.profiles[name]
            self.config.trigger_words = list(p_data.get("trigger_words", []))
            self.update_trigger_summary_label()
            self.config.safety_before_ms = p_data.get("safety_before_ms", 200)
            self.config.safety_after_ms = p_data.get("safety_after_ms", 200)
            self.config.ducking_level = p_data.get("ducking_level", 0.0)
            self.config.censor_style = p_data.get("censor_style", "asterisks")
            self.config.strict_matching = p_data.get("strict_matching", True)

    def update_trigger_summary_label(self):
        count = len(self.config.trigger_words)
        self.lbl_trigger_summary.setText(f"Active Triggers: {count} word(s) configured (hidden from main view)")

    def open_trigger_editor(self):
        dlg = TriggerEditorDialog(self.config.trigger_words, parent=self)
        if dlg.exec():
            self.config.trigger_words = dlg.get_words()
            self.update_trigger_summary_label()
            if self.video_items:
                for item in self.video_items:
                    if item.subtitle_entries:
                        item.matches = scan_subtitles_for_triggers(item.subtitle_entries, self.config)
                self.refresh_table()

    def _update_stats_banner(self, items: list[VideoFileInfo]):
        total = len(items)
        with_subs = sum(1 for r in items if r.has_subtitles)
        with_bitmap = sum(1 for r in items if r.has_bitmap_subtitles and not r.has_subtitles)
        with_triggers = sum(1 for r in items if r.matches)
        total_triggers = sum(r.trigger_count for r in items)
        stat_msg = f"Found {total} video file(s) | {with_subs} with text subtitles"
        if with_bitmap > 0:
            stat_msg += f" | {with_bitmap} with bitmap subtitles (Whisper ready)"
        stat_msg += f" | {with_triggers} with trigger matches ({total_triggers} total instances)"
        self.lbl_stats.setText(stat_msg)

    def refresh_table(self):
        self.table.setRowCount(0)
        items_copy = list(self.video_items)
        self.video_items = []
        for info in items_copy:
            self.on_file_found(info)
        self._update_stats_banner(self.video_items)
        if self.table.rowCount() > 0:
            self.table.selectRow(0)

    def save_current_profile(self):
        name, ok = QInputDialog.getText(
            self, "Save Preset Profile", "Profile Name:", text=self.combo_profile.currentText()
        )
        if ok and name.strip():
            save_user_profile(name.strip(), self.config)
            self.profiles = load_all_profiles()
            self.load_initial_profile()
            self.combo_profile.setCurrentText(name.strip())
            QMessageBox.information(self, "Preset Saved", f"Profile '{name.strip()}' saved.")

    def delete_current_profile(self):
        name = self.combo_profile.currentText()
        confirm = QMessageBox.question(
            self, "Delete Profile", f"Are you sure you want to delete profile '{name}'?"
        )
        if confirm == QMessageBox.Yes:
            delete_user_profile(name)
            self.profiles = load_all_profiles()
            self.load_initial_profile()

    def open_settings(self):
        src_path = Path(self.resolved_folder_path) if self.resolved_folder_path else None
        dlg = SettingsDialog(self.config, source_dir=src_path, parent=self)
        if dlg.exec():
            idx = self.combo_mode.findData(self.config.output_mode)
            if idx >= 0:
                self.combo_mode.setCurrentIndex(idx)
            self.on_mode_combo_changed()

    def browse_folder(self):
        start_dir = self.edit_folder.text() or str(Path.home())
        if not os.path.isdir(start_dir):
            smb_root = get_kio_fuse_smb_root()
            start_dir = smb_root if (smb_root and os.path.isdir(smb_root)) else str(Path.home())

        folder = QFileDialog.getExistingDirectory(self, "Select Video Folder", start_dir)
        if folder:
            self.edit_folder.setText(folder)

    def start_scan(self):
        folder_str = self.edit_folder.text().strip()
        if not folder_str:
            QMessageBox.warning(self, "No Folder Specified", "Please enter or select a directory.")
            return

        self.lbl_status.setText("Resolving path...")
        QApplication.processEvents()

        resolved = resolve_network_url(folder_str)
        if not os.path.isdir(resolved):
            QMessageBox.warning(
                self,
                "Directory Not Accessible",
                f"Could not open directory:\n{folder_str}\n\n"
                f"If using SMB, verify that the network share is reachable."
            )
            return

        self.resolved_folder_path = resolved
        self.table.setRowCount(0)
        self.insp_table.setRowCount(0)
        self.video_items.clear()

        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.lbl_status.setText("Scanning folder for video files and extracting subtitles...")
        self.btn_scan.setEnabled(False)

        self.scan_worker = FolderScanWorker(Path(resolved), self.config)
        self.scan_worker.progress.connect(self.on_scan_progress)
        self.scan_worker.file_found.connect(self.on_file_found)
        self.scan_worker.scan_finished.connect(self.on_scan_finished)
        self.scan_worker.start()

    def on_scan_progress(self, current: int, total: int, filename: str):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.lbl_status.setText(f"Scanning ({current}/{total}): {filename}")

    def on_file_found(self, info: VideoFileInfo):
        self.video_items.append(info)
        row = self.table.rowCount()
        self.table.insertRow(row)

        chk_item = QTableWidgetItem()
        chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        chk_item.setCheckState(Qt.Checked if info.matches else Qt.Unchecked)
        self.table.setItem(row, 0, chk_item)

        self.table.setItem(row, 1, QTableWidgetItem(info.filename))

        sub_item = QTableWidgetItem()
        if info.has_subtitles:
            sub_item.setText(info.subtitle_source)
        elif info.has_bitmap_subtitles:
            sub_item.setText(f"{info.subtitle_source} (Whisper Ready)")
            sub_item.setToolTip("Image bitmap subtitles cannot be parsed as text directly. Use Whisper to transcribe.")
        else:
            sub_item.setText("No Subtitles")
        self.table.setItem(row, 2, sub_item)

        trig_item = QTableWidgetItem()
        if info.matches:
            trig_item.setText(f"{info.trigger_count} match(es)")
            font = trig_item.font()
            font.setBold(True)
            trig_item.setFont(font)
        elif info.has_subtitles:
            trig_item.setText("Clean")
        elif info.has_bitmap_subtitles:
            trig_item.setText("Needs Transcription")
        else:
            trig_item.setText("-")
        self.table.setItem(row, 3, trig_item)
        self.table.setItem(row, 4, QTableWidgetItem(info.duration_str))

    def on_scan_finished(self, results: list[VideoFileInfo]):
        self.btn_scan.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.lbl_status.setText("Scan completed.")
        self._update_stats_banner(results)
        if self.table.rowCount() > 0:
            self.table.selectRow(0)

    def on_table_row_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.insp_table.setRowCount(0)
            self.lbl_insp_title.setText("Trigger Moments Inspector")
            self.lbl_insp_title.setToolTip("")
            return

        idx = rows[0].row()
        if 0 <= idx < len(self.video_items):
            info = self.video_items[idx]
            title_text = f"Trigger Moments: {info.filename} ({len(info.matches)} detected)"
            self.lbl_insp_title.setText(title_text)
            self.lbl_insp_title.setToolTip(title_text)
            self.insp_table.setRowCount(0)
            reveal = self.chk_reveal_inspector.isChecked()

            for m in info.matches:
                i_row = self.insp_table.rowCount()
                self.insp_table.insertRow(i_row)
                self.insp_table.setItem(i_row, 0, QTableWidgetItem(f"{m.entry.start_str} --> {m.entry.end_str}"))
                w_text = ", ".join(m.matched_words) if reveal else "[Hidden]"
                self.insp_table.setItem(i_row, 1, QTableWidgetItem(w_text))
                prev_text = f"{m.entry.text}\n-> {m.censored_text}" if reveal else m.censored_text
                self.insp_table.setItem(i_row, 2, QTableWidgetItem(prev_text))

    def _set_busy_ui(self, busy: bool, stop_label: str = "Stop Processing"):
        self.btn_process_selected.setVisible(not busy)
        self.btn_process_selected.setEnabled(not busy)
        self.btn_process_folder.setVisible(not busy)
        self.btn_process_folder.setEnabled(not busy)
        self.btn_transcribe.setEnabled(not busy)
        self.btn_scan.setEnabled(not busy)
        self.btn_stop.setVisible(busy)
        self.btn_stop.setEnabled(busy)
        self.btn_stop.setText(stop_label)
        self.progress_bar.setVisible(busy)

    def stop_current_operation(self):
        confirm = QMessageBox.question(
            self, "Confirm Stop", "Stop the active batch job?", QMessageBox.Yes | QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return

        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("Stopping...")
        self.lbl_status.setText("Stopping current job...")
        if self.process_worker:
            self.process_worker.cancel()
        if self.transcribe_worker:
            self.transcribe_worker.cancel()

    def start_batch_process(self, process_all: bool = False):
        if not self.video_items:
            QMessageBox.information(self, "No Videos Loaded", "Scan a folder containing video files first.")
            return

        selected_items = [
            self.video_items[row]
            for row in range(self.table.rowCount())
            if row < len(self.video_items) and self.table.item(row, 0) and self.table.item(row, 0).checkState() == Qt.Checked
        ]

        if process_all:
            count = len(self.video_items)
            flagged_in_all = sum(1 for v in self.video_items if v.matches)
            if self.config.output_mode == "season_mirror":
                msg = (
                    f"Mirror all {count} video(s) in this folder?\n\n"
                    f"- Flagged videos ({flagged_in_all}) will be filtered.\n"
                    f"- Clean videos and artwork will be copied to the mirror folder.\n\n"
                    f"Proceed?"
                )
            else:
                msg = f"Process all {count} video(s) in this folder using '{self.config.output_mode}' mode?"
        else:
            if not selected_items:
                rows = self.table.selectionModel().selectedRows()
                if rows and rows[0].row() < len(self.video_items):
                    selected_items = [self.video_items[rows[0].row()]]
                else:
                    QMessageBox.information(
                        self, "No Videos Selected", "Check at least one video in the table before starting."
                    )
                    return
            count = len(selected_items)
            msg = f"Process {count} selected video(s) using '{self.config.output_mode}' mode?"

        if QMessageBox.question(self, "Confirm Processing", msg, QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return

        self._set_busy_ui(True, "Stop Processing")
        self.progress_bar.setMaximum(count * 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Preparing files... (0%)")

        sleep_tag = " [Sleep Inhibited]" if self.config.inhibit_sleep else ""
        self.lbl_status.setText(f"Starting batch processing...{sleep_tag}")

        if self.config.inhibit_sleep:
            self.sleep_inhibitor.acquire("SubDuck: Batch processing videos")

        self.process_thread = QThread()
        self.process_worker = BatchProcessWorker(
            video_items=selected_items,
            all_scanned_items=self.video_items,
            config=self.config,
            source_dir=Path(self.resolved_folder_path),
            process_all=process_all
        )
        self.process_worker.moveToThread(self.process_thread)

        self.process_thread.started.connect(self.process_worker.run)
        self.process_worker.file_started.connect(self.on_process_file_started)
        self.process_worker.file_finished.connect(self.on_process_file_finished)
        self.process_worker.all_finished.connect(self.on_process_all_finished)
        self.process_worker.whisper_progress.connect(self.on_process_whisper_progress)
        self.process_worker.copy_progress.connect(self.on_process_copy_progress)
        self.process_worker.status_update.connect(self.on_process_status_update)
        self.process_thread.start()

    def on_process_status_update(self, msg: str):
        sleep_tag = " [Sleep Inhibited]" if self.sleep_inhibitor.is_active else ""
        self.lbl_status.setText(f"{msg}{sleep_tag}")
        self.progress_bar.setFormat(msg)

    def on_process_copy_progress(self, filename: str, copied: int, total_b: int, pct: int):
        total_items = max(1, self._batch_total_items)
        current_idx = max(1, self._batch_current_index)
        self.progress_bar.setValue((current_idx - 1) * 100 + int(pct))

        copied_mb = copied / (1024 * 1024)
        total_mb = total_b / (1024 * 1024)
        if total_mb >= 1024:
            size_str = f"{copied_mb / 1024:.2f} GB / {total_mb / 1024:.2f} GB"
        else:
            size_str = f"{copied_mb:.1f} MB / {total_mb:.1f} MB"

        self.progress_bar.setFormat(f"Copying {current_idx} of {total_items}: {pct}% ({size_str})")
        sleep_tag = " [Sleep Inhibited]" if self.sleep_inhibitor.is_active else ""
        self.lbl_status.setText(f"Copying clean video ({current_idx}/{total_items}): {size_str} ({pct}%)...{sleep_tag}")

    def on_process_whisper_progress(self, pct: int, cur_s: float, tot_s: float):
        total_items = max(1, self._batch_total_items)
        current_idx = max(1, self._batch_current_index)
        self.progress_bar.setValue((current_idx - 1) * 100 + int(pct))

        cur_min, cur_sec = divmod(int(cur_s), 60)
        tot_min, tot_sec = divmod(int(tot_s), 60)
        self.progress_bar.setFormat(
            f"Whisper ({current_idx}/{total_items}): {pct}% ({cur_min}:{cur_sec:02d} / {tot_min}:{tot_sec:02d})"
        )

    def start_whisper_transcription(self):
        if not is_whisper_available():
            QMessageBox.critical(
                self, "Whisper Unavailable", "faster-whisper is not installed in the active Python environment."
            )
            return

        checked_items = [
            self.video_items[row]
            for row in range(self.table.rowCount())
            if row < len(self.video_items) and self.table.item(row, 0) and self.table.item(row, 0).checkState() == Qt.Checked
        ]

        if checked_items:
            target_items = checked_items
        else:
            missing_items = [v for v in self.video_items if not v.has_subtitles]
            if missing_items:
                target_items = missing_items
            else:
                rows = self.table.selectionModel().selectedRows()
                if rows and rows[0].row() < len(self.video_items):
                    target_items = [self.video_items[rows[0].row()]]
                else:
                    QMessageBox.information(
                        self,
                        "Subtitles Present",
                        "All loaded videos already have text subtitles.\n"
                        "Check specific rows in the table to force re-transcription."
                    )
                    return

        count = len(target_items)
        confirm = QMessageBox.question(
            self,
            "Confirm Whisper Transcription",
            f"Transcribe {count} video file(s) with faster-whisper?\n\n"
            f"- Model: {self.config.whisper_model}\n"
            f"- Device: {self.config.whisper_device}",
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return

        self._set_busy_ui(True, "Stop Transcription")
        self.progress_bar.setMaximum(count * 100)
        self.progress_bar.setValue(0)

        if self.config.inhibit_sleep:
            self.sleep_inhibitor.acquire("SubDuck: Transcribing audio with faster-whisper")

        self.lbl_status.setText(f"Starting Whisper transcription for {count} video(s)...")

        self.transcribe_thread = QThread()
        self.transcribe_worker = WhisperTranscribeWorker(target_items, self.config)
        self.transcribe_worker.moveToThread(self.transcribe_thread)

        self.transcribe_thread.started.connect(self.transcribe_worker.run)
        self.transcribe_worker.file_started.connect(self.on_transcribe_file_started)
        self.transcribe_worker.file_progress.connect(self.on_transcribe_chunk_progress)
        self.transcribe_worker.file_completed.connect(self.on_transcribe_file_completed)
        self.transcribe_worker.all_completed.connect(self.on_transcribe_all_completed)
        self.transcribe_thread.start()

    def on_transcribe_file_started(self, filename: str, current: int, total: int):
        sleep_tag = " [Sleep Inhibited]" if self.sleep_inhibitor.is_active else ""
        self.lbl_status.setText(f"Transcribing ({current}/{total}): {filename}...{sleep_tag}")
        self.progress_bar.setMaximum(total * 100)
        self.progress_bar.setValue((current - 1) * 100)
        self.progress_bar.setFormat(f"Transcribing {current} of {total} - {filename} (0%)")

    def on_transcribe_chunk_progress(self, filename: str, pct: int, cur_s: float, tot_s: float):
        total_files = len(self.transcribe_worker.target_items) if self.transcribe_worker else 1
        current_file_idx = 1
        if self.transcribe_worker:
            for idx, item in enumerate(self.transcribe_worker.target_items, 1):
                if item.filename == filename:
                    current_file_idx = idx
                    break
        self.progress_bar.setValue(int((current_file_idx - 1) * 100 + pct))
        cur_min, cur_sec = divmod(int(cur_s), 60)
        tot_min, tot_sec = divmod(int(tot_s), 60)
        self.progress_bar.setFormat(
            f"Transcribing {current_file_idx} of {total_files}: {pct}% ({cur_min}:{cur_sec:02d}/{tot_min}:{tot_sec:02d})"
        )

    def on_transcribe_file_completed(self, info: VideoFileInfo, success: bool, msg: str, current: int, total: int):
        self.progress_bar.setValue(current * 100)
        for row in range(self.table.rowCount()):
            item_f = self.table.item(row, 1)
            if item_f and item_f.text() == info.filename:
                chk = self.table.item(row, 0)
                if chk:
                    chk.setCheckState(Qt.Checked if info.matches else Qt.Unchecked)
                sub_item = self.table.item(row, 2)
                if sub_item:
                    sub_item.setText(info.subtitle_source)
                trig_item = self.table.item(row, 3)
                if trig_item:
                    if info.matches:
                        trig_item.setText(f"{info.trigger_count} match(es)")
                        f = trig_item.font()
                        f.setBold(True)
                        trig_item.setFont(f)
                    else:
                        trig_item.setText("Clean")
                break

    def on_transcribe_all_completed(self, succeeded: int, failed: int):
        self.sleep_inhibitor.release()
        if self.transcribe_thread:
            self.transcribe_thread.quit()
            self.transcribe_thread.wait()
        self._set_busy_ui(False)
        self.lbl_status.setText(f"Whisper transcription finished: {succeeded} succeeded, {failed} failed.")
        self._update_stats_banner(self.video_items)
        self.on_table_row_selected()

        if failed > 0:
            QMessageBox.warning(
                self,
                "Transcription Finished with Warnings",
                f"Transcribed {succeeded} file(s), {failed} file(s) failed."
            )
        else:
            QMessageBox.information(
                self, "Whisper Transcription Complete", f"Generated subtitles for {succeeded} video file(s)."
            )

    def on_process_file_started(self, filename: str, current: int, total: int):
        self._batch_current_file = filename
        self._batch_current_index = current
        self._batch_total_items = total
        sleep_tag = " [Sleep Inhibited]" if self.sleep_inhibitor.is_active else ""
        self.lbl_status.setText(f"Processing ({current}/{total}): {filename}...{sleep_tag}")
        self.progress_bar.setMaximum(total * 100)
        base_val = (current - 1) * 100
        self.progress_bar.setValue(base_val)
        overall_pct = int((base_val / (total * 100)) * 100) if total > 0 else 0
        self.progress_bar.setFormat(f"Processing {current} of {total} ({overall_pct}%) - {filename}")

    def on_process_file_finished(self, filename: str, success: bool, msg: str, current: int, total: int):
        self.progress_bar.setValue(current * 100)
        overall_pct = int((current / total) * 100) if total > 0 else 100
        self.progress_bar.setFormat(f"Completed {current} of {total} ({overall_pct}%)")
        for row in range(self.table.rowCount()):
            item_f = self.table.item(row, 1)
            if item_f and item_f.text() == filename:
                status_item = self.table.item(row, 4)
                if status_item:
                    status_item.setText("Done" if success else "Error")
                    status_item.setForeground(QColor("#27ae60" if success else "#c0392b"))
                    if not success:
                        status_item.setToolTip(f"Error: {msg}")

    def on_process_all_finished(self, success_count: int, fail_count: int, error_list: list):
        self.sleep_inhibitor.release()
        if self.process_thread:
            self.process_thread.quit()
            self.process_thread.wait()
        self._set_busy_ui(False)
        self.lbl_status.setText(f"Batch completed: {success_count} succeeded, {fail_count} failed.")

        if fail_count > 0:
            err_text = "\n".join(f"- {e}" for e in error_list[:5])
            QMessageBox.warning(
                self,
                "Processing Complete (With Errors)",
                f"Succeeded: {success_count}\nFailed: {fail_count}\n\nDetails:\n{err_text}"
            )
        else:
            if self.config.output_mode == "season_mirror":
                dest = self.config.season_mirror_folder or (
                    str(get_default_season_mirror_dir(Path(self.resolved_folder_path)))
                    if self.resolved_folder_path else "Mirror Directory"
                )
                QMessageBox.information(
                    self,
                    "Folder Mirror Complete",
                    f"Processed {success_count} video(s) into mirror folder:\n{dest}"
                )
            else:
                QMessageBox.information(
                    self, "Processing Complete", f"Processed and saved {success_count} video file(s)."
                )

    def closeEvent(self, event):
        self.sleep_inhibitor.release()
        if self.process_worker:
            self.process_worker.cancel()
        if self.transcribe_worker:
            self.transcribe_worker.cancel()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
