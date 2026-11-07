import os
import sys
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QThread, Signal, Slot, QUrl
from PySide6.QtGui import QColor, QFont, QIcon, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QSplitter, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox,
    QCheckBox, QGroupBox, QProgressBar, QMessageBox, QTabWidget, QDialog,
    QDialogButtonBox, QFormLayout, QFrame, QAbstractItemView, QInputDialog,
    QRadioButton, QButtonGroup
)

from subduck.models import VideoFileInfo, CensorConfig, TriggerMatch
from subduck.sub_parser import extract_subtitles_from_video, probe_video
from subduck.matcher import scan_subtitles_for_triggers
from subduck.processor import BatchProcessWorker, get_default_season_mirror_dir
from subduck.profiles import load_all_profiles, save_user_profile, delete_user_profile
from subduck.kio_resolver import resolve_network_url, get_kio_fuse_smb_root, ensure_kio_fuse_running

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".m4v"}

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
        try:
            for root, _, files in os.walk(self.folder):
                for f in sorted(files):
                    p = Path(root) / f
                    if p.suffix.lower() in VIDEO_EXTENSIONS:
                        video_files.append(p)
        except Exception:
            pass

        total = len(video_files)
        results = []

        for i, v_path in enumerate(video_files, 1):
            if self._is_cancelled:
                break

            self.progress.emit(i, total, v_path.name)

            file_size = v_path.stat().st_size if v_path.exists() else 0
            probe_meta = probe_video(v_path)
            duration = float(probe_meta.get("format", {}).get("duration", 0.0))

            info = VideoFileInfo(
                path=v_path,
                filename=v_path.name,
                file_size=file_size,
                duration_sec=duration
            )

            sub_entries, sub_desc = extract_subtitles_from_video(v_path, self.config.subtitle_lang)
            info.subtitle_entries = sub_entries
            info.subtitle_source = sub_desc
            info.has_subtitles = len(sub_entries) > 0

            if info.has_subtitles and self.config.trigger_words:
                matches = scan_subtitles_for_triggers(sub_entries, self.config)
                info.matches = matches
                if matches:
                    info.status = f"Flagged ({info.trigger_count})"
                else:
                    info.status = "Clean"
            elif not info.has_subtitles:
                info.status = "No Subtitles"
            else:
                info.status = "Clean"

            results.append(info)
            self.file_found.emit(info)

        self.scan_finished.emit(results)

class SettingsDialog(QDialog):
    def __init__(self, config: CensorConfig, source_dir: Optional[Path] = None, parent=None):
        super().__init__(parent)
        self.config = config
        self.source_dir = source_dir
        self.setWindowTitle("SubDuck Output Modes & Processing Settings")
        self.setMinimumWidth(560)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        # Output Mode Section
        mode_group = QGroupBox("Output & Library Integration Mode")
        mode_layout = QVBoxLayout(mode_group)

        self.btn_group_mode = QButtonGroup(self)

        self.rb_season = QRadioButton("Mirror Complete Season to New Folder (Recommended for TV Shows & Jellyfin)")
        self.rb_season.setToolTip("Creates a clean season folder with standard episode names, ducking flagged episodes and copying clean ones so Jellyfin autoplay works flawlessly.")
        self.btn_group_mode.addButton(self.rb_season, 0)
        mode_layout.addWidget(self.rb_season)

        # Season options
        self.season_subframe = QFrame()
        season_form = QFormLayout(self.season_subframe)
        season_form.setContentsMargins(20, 2, 0, 8)

        dest_h = QHBoxLayout()
        default_mirror = str(get_default_season_mirror_dir(self.source_dir)) if self.source_dir else ""
        self.edit_mirror_dir = QLineEdit(self.config.season_mirror_folder or default_mirror)
        self.btn_mirror_browse = QPushButton("Browse...")
        self.btn_mirror_browse.clicked.connect(self.browse_mirror)
        dest_h.addWidget(self.edit_mirror_dir)
        dest_h.addWidget(self.btn_mirror_browse)
        season_form.addRow("Season Target Folder:", dest_h)

        self.chk_copy_clean = QCheckBox("Copy clean episodes automatically (ensures complete season)")
        self.chk_copy_clean.setChecked(self.config.copy_clean_files)
        season_form.addRow(self.chk_copy_clean)

        self.chk_copy_art = QCheckBox("Copy thumbnails, posters, and NFO metadata")
        self.chk_copy_art.setChecked(self.config.copy_artwork)
        season_form.addRow(self.chk_copy_art)

        mode_layout.addWidget(self.season_subframe)

        # Mode 2: In-place replace
        self.rb_inplace = QRadioButton("Replace Original Files In-Place (Saves Storage)")
        self.btn_group_mode.addButton(self.rb_inplace, 1)
        mode_layout.addWidget(self.rb_inplace)

        self.inplace_subframe = QFrame()
        inplace_layout = QVBoxLayout(self.inplace_subframe)
        inplace_layout.setContentsMargins(20, 2, 0, 8)
        self.chk_backup = QCheckBox("Backup original video to '_originals/' folder before overwriting")
        self.chk_backup.setChecked(self.config.backup_originals)
        inplace_layout.addWidget(self.chk_backup)
        mode_layout.addWidget(self.inplace_subframe)

        # Mode 3: Dual-audio
        self.rb_dual = QRadioButton("Dual-Audio Track in Single File (Original Audio + Trigger-Muted Track)")
        self.rb_dual.setToolTip("Embeds both the original audio and the ducked audio in the same file, setting the ducked track as default.")
        self.btn_group_mode.addButton(self.rb_dual, 2)
        mode_layout.addWidget(self.rb_dual)

        # Mode 4: Separate suffix
        self.rb_suffix = QRadioButton("Save Alongside with Suffix (e.g. '... trigger.mkv' - Best for Single Movies)")
        self.btn_group_mode.addButton(self.rb_suffix, 3)
        mode_layout.addWidget(self.rb_suffix)

        self.suffix_subframe = QFrame()
        suffix_form = QFormLayout(self.suffix_subframe)
        suffix_form.setContentsMargins(20, 2, 0, 8)
        self.edit_suffix = QLineEdit(self.config.output_suffix)
        suffix_form.addRow("Filename Suffix:", self.edit_suffix)
        mode_layout.addWidget(self.suffix_subframe)

        # Set active radio
        if self.config.output_mode == "season_mirror":
            self.rb_season.setChecked(True)
        elif self.config.output_mode == "inplace_replace":
            self.rb_inplace.setChecked(True)
        elif self.config.output_mode == "dual_audio":
            self.rb_dual.setChecked(True)
        else:
            self.rb_suffix.setChecked(True)

        layout.addWidget(mode_group)

        # Audio & Timing Settings
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
        self.combo_ducking.addItem("Complete Silence (Mute to 0%)", 0.0)
        self.combo_ducking.addItem("Volume Ducking (-20 dB / 10% volume)", 0.1)
        self.combo_ducking.addItem("Mild Ducking (-12 dB / 25% volume)", 0.25)
        idx = 0
        if abs(self.config.ducking_level - 0.1) < 0.01:
            idx = 1
        elif abs(self.config.ducking_level - 0.25) < 0.01:
            idx = 2
        self.combo_ducking.setCurrentIndex(idx)
        form.addRow("Audio Ducking Level:", self.combo_ducking)

        self.combo_style = QComboBox()
        self.combo_style.addItem("Full Asterisks (e.g. ****** )", "asterisks")
        self.combo_style.addItem("Inner Asterisks (e.g. c****r )", "inner_asterisks")
        self.combo_style.addItem("Censored Tag (e.g. [censored] )", "tag")
        self.combo_style.addItem("Redacted Block (e.g. ████ )", "block")
        self.combo_style.addItem("Remove Word Completely", "remove")
        idx_style = self.combo_style.findData(self.config.censor_style)
        if idx_style >= 0:
            self.combo_style.setCurrentIndex(idx_style)
        form.addRow("Subtitle Censorship Style:", self.combo_style)

        self.chk_strict = QCheckBox("Strict whole-word boundary (allows hyphens like cancer-sign)")
        self.chk_strict.setChecked(self.config.strict_matching)
        form.addRow("Word Matching:", self.chk_strict)

        self.chk_save_srt = QCheckBox("Save external .srt subtitle file")
        self.chk_save_srt.setChecked(self.config.save_srt)
        form.addRow("External Subtitles:", self.chk_save_srt)

        self.chk_embed_sub = QCheckBox("Embed censored subtitle track into output container")
        self.chk_embed_sub.setChecked(self.config.embed_sub)
        form.addRow("Embedded Subtitles:", self.chk_embed_sub)

        layout.addWidget(audio_group)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def browse_mirror(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Season Mirror Target Directory", self.edit_mirror_dir.text())
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

        self.config.season_mirror_folder = self.edit_mirror_dir.text().strip()
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
        self.accept()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SubDuck - Video Trigger Word & Audio Ducking Studio")
        self.resize(1220, 800)
        self.setAcceptDrops(True)

        self.config = CensorConfig()
        self.profiles = load_all_profiles()
        self.video_items: List[VideoFileInfo] = []
        self.resolved_folder_path: str = ""

        self.scan_worker: Optional[FolderScanWorker] = None
        self.process_thread: Optional[QThread] = None
        self.process_worker: Optional[BatchProcessWorker] = None

        ensure_kio_fuse_running()
        self.setup_ui()
        self.load_initial_profile()

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)

        # Top Section: Directory Picker
        top_group = QGroupBox("Target Video Directory")
        top_layout = QHBoxLayout(top_group)

        self.edit_folder = QLineEdit()
        self.edit_folder.setPlaceholderText("Select folder or paste SMB URL (e.g. smb://nas.local/media/...)...")
        self.edit_folder.returnPressed.connect(self.start_scan)

        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.clicked.connect(self.browse_folder)

        self.btn_smb = QPushButton("SMB / Network...")
        self.btn_smb.setStyleSheet("font-weight: 500;")
        self.btn_smb.clicked.connect(self.open_smb_dialog)

        self.btn_scan = QPushButton("Scan Folder")
        self.btn_scan.setStyleSheet("font-weight: bold; padding: 6px 14px; background-color: #2980b9; color: white;")
        self.btn_scan.clicked.connect(self.start_scan)

        top_layout.addWidget(self.edit_folder)
        top_layout.addWidget(self.btn_browse)
        top_layout.addWidget(self.btn_smb)
        top_layout.addWidget(self.btn_scan)
        root_layout.addWidget(top_group)

        # Trigger Configuration & Output Mode Bar
        trig_group = QGroupBox("Trigger Profile & Output Settings")
        trig_layout = QVBoxLayout(trig_group)
        trig_row1 = QHBoxLayout()

        trig_row1.addWidget(QLabel("Preset Profile:"))
        self.combo_profile = QComboBox()
        self.combo_profile.currentIndexChanged.connect(self.on_profile_changed)
        trig_row1.addWidget(self.combo_profile, 1)

        self.btn_save_profile = QPushButton("Save Preset...")
        self.btn_save_profile.clicked.connect(self.save_current_profile)
        trig_row1.addWidget(self.btn_save_profile)

        self.btn_del_profile = QPushButton("Delete")
        self.btn_del_profile.clicked.connect(self.delete_current_profile)
        trig_row1.addWidget(self.btn_del_profile)

        trig_row1.addSpacing(15)
        trig_row1.addWidget(QLabel("Output Mode:"))
        self.combo_mode = QComboBox()
        self.combo_mode.addItem("Season Mirror (Jellyfin Autoplay Ready)", "season_mirror")
        self.combo_mode.addItem("In-Place Replace (with backup)", "inplace_replace")
        self.combo_mode.addItem("Dual-Audio Track in Single File", "dual_audio")
        self.combo_mode.addItem("Save Alongside with Suffix", "separate_suffix")
        self.combo_mode.currentIndexChanged.connect(self.on_mode_combo_changed)
        trig_row1.addWidget(self.combo_mode, 1)

        self.btn_settings = QPushButton("Options...")
        self.btn_settings.clicked.connect(self.open_settings)
        trig_row1.addWidget(self.btn_settings)

        trig_layout.addLayout(trig_row1)

        trig_row2 = QHBoxLayout()
        trig_row2.addWidget(QLabel("Trigger Words (comma or newline separated):"))
        trig_layout.addLayout(trig_row2)

        self.edit_triggers = QTextEdit()
        self.edit_triggers.setMaximumHeight(55)
        self.edit_triggers.setPlaceholderText("e.g. cancer, tumor, chemo, blood, gore")
        self.edit_triggers.textChanged.connect(self.on_triggers_edited)
        trig_layout.addWidget(self.edit_triggers)

        root_layout.addWidget(trig_group)

        # Stats banner
        self.lbl_stats = QLabel("No files loaded. Point at a local or SMB folder, or drag-and-drop a folder from Dolphin.")
        self.lbl_stats.setStyleSheet("color: #7f8c8d; font-weight: bold; padding: 2px 4px;")
        root_layout.addWidget(self.lbl_stats)

        # Main Splitter: Video Table (Left) + Inspector Panel (Right)
        splitter = QSplitter(Qt.Horizontal)

        table_container = QWidget()
        t_layout = QVBoxLayout(table_container)
        t_layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Select", "Video File", "Subtitles Found", "Trigger Matches", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.itemSelectionChanged.connect(self.on_table_row_selected)
        t_layout.addWidget(self.table)
        splitter.addWidget(table_container)

        insp_container = QWidget()
        insp_layout = QVBoxLayout(insp_container)
        insp_layout.setContentsMargins(0, 0, 0, 0)

        self.lbl_insp_title = QLabel("Trigger Moments Inspector")
        self.lbl_insp_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        insp_layout.addWidget(self.lbl_insp_title)

        self.insp_table = QTableWidget(0, 3)
        self.insp_table.setHorizontalHeaderLabels(["Timestamp", "Words", "Subtitle Line Preview"])
        self.insp_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.insp_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.insp_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.insp_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        insp_layout.addWidget(self.insp_table)

        splitter.addWidget(insp_container)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root_layout.addWidget(splitter, 1)

        # Bottom Action Bar
        bottom_layout = QHBoxLayout()

        self.btn_select_flagged = QPushButton("Select All Flagged")
        self.btn_select_flagged.clicked.connect(self.select_all_flagged)
        bottom_layout.addWidget(self.btn_select_flagged)

        self.btn_select_none = QPushButton("Clear Selection")
        self.btn_select_none.clicked.connect(self.select_none)
        bottom_layout.addWidget(self.btn_select_none)

        bottom_layout.addStretch()

        self.btn_process = QPushButton("Process Complete Season")
        self.btn_process.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold; padding: 7px 18px;")
        self.btn_process.clicked.connect(self.start_batch_process)
        bottom_layout.addWidget(self.btn_process)

        root_layout.addLayout(bottom_layout)

        # Progress Bar and Status
        prog_layout = QVBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        prog_layout.addWidget(self.progress_bar)

        self.lbl_status = QLabel("Ready")
        self.lbl_status.setStyleSheet("color: #7f8c8d;")
        prog_layout.addWidget(self.lbl_status)

        root_layout.addLayout(prog_layout)

    def on_mode_combo_changed(self):
        mode = self.combo_mode.currentData()
        self.config.output_mode = mode
        if mode == "season_mirror":
            self.btn_process.setText("Process Complete Season")
            self.btn_process.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold; padding: 7px 18px;")
        elif mode == "inplace_replace":
            self.btn_process.setText("Replace In-Place (Selected)")
            self.btn_process.setStyleSheet("background-color: #d35400; color: white; font-weight: bold; padding: 7px 18px;")
        elif mode == "dual_audio":
            self.btn_process.setText("Create Dual-Audio Files")
            self.btn_process.setStyleSheet("background-color: #8e44ad; color: white; font-weight: bold; padding: 7px 18px;")
        else:
            self.btn_process.setText("Process Selected Videos")
            self.btn_process.setStyleSheet("background-color: #2980b9; color: white; font-weight: bold; padding: 7px 18px;")

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls:
                raw_url = urls[0].toString()
                if urls[0].isLocalFile():
                    folder_path = urls[0].toLocalFile()
                else:
                    folder_path = raw_url
                self.edit_folder.setText(folder_path)
                self.start_scan()
                event.acceptProposedAction()

    def load_initial_profile(self):
        self.combo_profile.clear()
        for name in sorted(self.profiles.keys()):
            self.combo_profile.addItem(name)
        if "Medical & Illness Triggers" in self.profiles:
            self.combo_profile.setCurrentText("Medical & Illness Triggers")

    def on_profile_changed(self):
        name = self.combo_profile.currentText()
        if name in self.profiles:
            p_data = self.profiles[name]
            words = p_data.get("trigger_words", [])
            self.edit_triggers.setPlainText(", ".join(words))
            self.config.trigger_words = words
            self.config.safety_before_ms = p_data.get("safety_before_ms", 200)
            self.config.safety_after_ms = p_data.get("safety_after_ms", 200)
            self.config.ducking_level = p_data.get("ducking_level", 0.0)
            self.config.censor_style = p_data.get("censor_style", "asterisks")
            self.config.strict_matching = p_data.get("strict_matching", True)

    def on_triggers_edited(self):
        raw = self.edit_triggers.toPlainText()
        tokens = [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]
        self.config.trigger_words = tokens

    def save_current_profile(self):
        name, ok = QInputDialog.getText(self, "Save Preset Profile", "Profile Name:", text=self.combo_profile.currentText())
        if ok and name.strip():
            save_user_profile(name.strip(), self.config)
            self.profiles = load_all_profiles()
            self.load_initial_profile()
            self.combo_profile.setCurrentText(name.strip())
            QMessageBox.information(self, "Preset Saved", f"Profile '{name.strip()}' saved successfully.")

    def delete_current_profile(self):
        name = self.combo_profile.currentText()
        confirm = QMessageBox.question(self, "Delete Profile", f"Are you sure you want to delete profile '{name}'?")
        if confirm == QMessageBox.Yes:
            delete_user_profile(name)
            self.profiles = load_all_profiles()
            self.load_initial_profile()

    def open_settings(self):
        src_path = Path(self.resolved_folder_path) if self.resolved_folder_path else None
        dlg = SettingsDialog(self.config, source_dir=src_path, parent=self)
        if dlg.exec():
            # Update mode combo to reflect dialog selection
            idx = self.combo_mode.findData(self.config.output_mode)
            if idx >= 0:
                self.combo_mode.setCurrentIndex(idx)
            self.on_mode_combo_changed()

    def browse_folder(self):
        start_dir = self.edit_folder.text() or str(Path.home())
        if not os.path.isdir(start_dir):
            smb_root = get_kio_fuse_smb_root()
            if smb_root and os.path.isdir(smb_root):
                start_dir = smb_root
            else:
                start_dir = str(Path.home())

        folder = QFileDialog.getExistingDirectory(self, "Select Video Folder", start_dir)
        if folder:
            self.edit_folder.setText(folder)

    def open_smb_dialog(self):
        default_url = self.edit_folder.text()
        if not default_url.startswith("smb://"):
            default_url = "smb://nas.local/media/Apocalypse (2026)/Season 1"

        url, ok = QInputDialog.getText(
            self,
            "Connect to SMB / Network Share",
            "Enter SMB Network URL (from Dolphin or Unraid):\n"
            "e.g. smb://nas.local/media/...",
            text=default_url
        )
        if ok and url.strip():
            self.edit_folder.setText(url.strip())
            self.start_scan()

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
                f"If using SMB, ensure the server is online and reachable."
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

        file_item = QTableWidgetItem(info.filename)
        self.table.setItem(row, 1, file_item)

        sub_item = QTableWidgetItem()
        if info.has_subtitles:
            sub_item.setText(info.subtitle_source)
            sub_item.setForeground(QColor("#27ae60"))
        else:
            sub_item.setText("No Subtitles")
            sub_item.setForeground(QColor("#e74c3c"))
        self.table.setItem(row, 2, sub_item)

        trig_item = QTableWidgetItem()
        if info.matches:
            trig_item.setText(f"{info.trigger_count} trigger match(es)")
            trig_item.setForeground(QColor("#e67e22"))
            font = trig_item.font()
            font.setBold(True)
            trig_item.setFont(font)
        elif info.has_subtitles:
            trig_item.setText("Clean")
            trig_item.setForeground(QColor("#2ecc71"))
        else:
            trig_item.setText("-")
            trig_item.setForeground(QColor("#95a5a6"))
        self.table.setItem(row, 3, trig_item)

        dur_item = QTableWidgetItem(info.duration_str)
        self.table.setItem(row, 4, dur_item)

    def on_scan_finished(self, results: List[VideoFileInfo]):
        self.btn_scan.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.lbl_status.setText("Scan completed.")

        total = len(results)
        with_subs = sum(1 for r in results if r.has_subtitles)
        with_triggers = sum(1 for r in results if r.matches)
        total_triggers = sum(r.trigger_count for r in results)

        self.lbl_stats.setText(
            f"Found {total} Video File(s)  |  {with_subs} with Subtitles  |  "
            f"{with_triggers} with Trigger Words ({total_triggers} total instances detected)"
        )

        if self.table.rowCount() > 0:
            self.table.selectRow(0)

    def on_table_row_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.insp_table.setRowCount(0)
            self.lbl_insp_title.setText("Trigger Moments Inspector")
            return

        idx = rows[0].row()
        if 0 <= idx < len(self.video_items):
            info = self.video_items[idx]
            self.lbl_insp_title.setText(f"Trigger Moments: {info.filename} ({len(info.matches)} detected)")
            self.insp_table.setRowCount(0)

            for m in info.matches:
                i_row = self.insp_table.rowCount()
                self.insp_table.insertRow(i_row)

                ts_item = QTableWidgetItem(f"{m.entry.start_str} --> {m.entry.end_str}")
                self.insp_table.setItem(i_row, 0, ts_item)

                w_item = QTableWidgetItem(", ".join(m.matched_words))
                w_item.setForeground(QColor("#e67e22"))
                self.insp_table.setItem(i_row, 1, w_item)

                prev_item = QTableWidgetItem(f"{m.entry.text}\n-> {m.censored_text}")
                self.insp_table.setItem(i_row, 2, prev_item)

    def select_all_flagged(self):
        for row in range(self.table.rowCount()):
            if row < len(self.video_items):
                has_matches = len(self.video_items[row].matches) > 0
                item = self.table.item(row, 0)
                if item:
                    item.setCheckState(Qt.Checked if has_matches else Qt.Unchecked)

    def select_none(self):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(Qt.Unchecked)

    def start_batch_process(self):
        selected_items = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.Checked and row < len(self.video_items):
                selected_items.append(self.video_items[row])

        if self.config.output_mode == "season_mirror":
            # In season mode, we process all items in the season
            count = len(self.video_items)
            msg = (
                f"Season Mirror Mode will process all {count} episode(s) in this season:\n\n"
                f"• Flagged episodes ({len(selected_items)}) will be ducked/censored with standard clean names.\n"
                f"• Clean episodes will be copied directly to ensure the season is 100% complete.\n"
                f"• Artwork and posters will be copied over for Jellyfin.\n\n"
                f"Proceed?"
            )
        else:
            if not selected_items:
                QMessageBox.information(self, "No Videos Selected", "Please select at least one video to process.")
                return
            count = len(selected_items)
            msg = f"Ready to process {count} video(s) using '{self.config.output_mode}' mode?"

        confirm = QMessageBox.question(self, "Confirm Processing", msg, QMessageBox.Yes | QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return

        self.btn_process.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(count)
        self.progress_bar.setValue(0)

        src_dir = Path(self.resolved_folder_path)

        self.process_thread = QThread()
        self.process_worker = BatchProcessWorker(
            video_items=selected_items,
            all_scanned_items=self.video_items,
            config=self.config,
            source_dir=src_dir
        )
        self.process_worker.moveToThread(self.process_thread)

        self.process_thread.started.connect(self.process_worker.run)
        self.process_worker.file_started.connect(self.on_process_file_started)
        self.process_worker.file_finished.connect(self.on_process_file_finished)
        self.process_worker.all_finished.connect(self.on_process_all_finished)

        self.process_thread.start()

    def on_process_file_started(self, filename: str):
        self.lbl_status.setText(f"Processing: {filename}...")

    def on_process_file_finished(self, filename: str, success: bool, msg: str):
        val = self.progress_bar.value() + 1
        self.progress_bar.setValue(val)
        for row in range(self.table.rowCount()):
            item_f = self.table.item(row, 1)
            if item_f and item_f.text() == filename:
                status_item = self.table.item(row, 4)
                if status_item:
                    status_item.setText("Done" if success else "Error")
                    status_item.setForeground(QColor("#27ae60" if success else "#e74c3c"))
                    if not success:
                        status_item.setToolTip(f"Error: {msg}")

    def on_process_all_finished(self, success_count: int, fail_count: int, error_list: list):
        self.process_thread.quit()
        self.process_thread.wait()
        self.btn_process.setEnabled(True)
        self.btn_scan.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.lbl_status.setText(f"Batch completed: {success_count} succeeded, {fail_count} failed.")

        if fail_count > 0:
            err_text = "\n".join(f"• {e}" for e in error_list[:5])
            QMessageBox.warning(
                self,
                "Processing Complete (With Errors)",
                f"Finished processing with issues:\n\n"
                f"• Succeeded: {success_count}\n"
                f"• Failed: {fail_count}\n\n"
                f"Error details:\n{err_text}"
            )
        else:
            if self.config.output_mode == "season_mirror":
                dest = self.config.season_mirror_folder or "Mirror Directory"
                QMessageBox.information(
                    self,
                    "Season Mirror Complete",
                    f"Complete Season Mirror is ready!\n\n"
                    f"• All {success_count} episodes processed/copied\n"
                    f"• Clean standard episode filenames for Jellyfin autoplay\n"
                    f"• Artwork and posters preserved\n\n"
                    f"Location: {dest}"
                )
            else:
                QMessageBox.information(
                    self,
                    "Processing Complete",
                    f"Successfully processed and saved {success_count} video file(s)!"
                )

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
