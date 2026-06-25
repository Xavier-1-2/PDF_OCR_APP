# -*- coding: utf-8 -*-
"""
PDF Folder Watcher — GUI Edition
Original: Shristi | Modified by: Najay Green

Watches a folder for incoming PDFs. Each PDF is OCR'd to extract:
  - Recipient Name
Then the file is moved into a subfolder named after the recipient.
"""

import os
import re
import sys
import time
import shutil
import tempfile
import threading

import fitz
import pytesseract
from PIL import Image, ImageFilter
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QScrollArea,
    QFrame
)
from PyQt5.QtCore import Qt, pyqtSignal, QThread

# ── Config ────────────────────────────────────────────────────────────────────
TESSERACT_PATH = r"C:\Tesseract\tesseract.exe"
pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

OCR_LANG = "eng"
MAX_PAGES_TO_SCAN = 1
FILE_SETTLE_DELAY = 2

RECIPIENT_PATTERN = re.compile(r"Recipient\s*Name\s*:\s*(.+)", re.IGNORECASE)
# ─────────────────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
#  OCR + FILE LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def pdf_page_to_image(doc, page_num: int) -> Image.Image:
    page = doc.load_page(page_num)
    # Reverted back to 300 DPI (400 is often overkill and slows it down)
    pix  = page.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72), alpha=False)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(pix.tobytes())
        tmp_path = f.name
        
    image = Image.open(tmp_path).convert("L")
    os.unlink(tmp_path)
    
    # Removed the aggressive ImageEnhance.Contrast step
    image = image.filter(ImageFilter.SHARPEN)
    
    return image


def extract_recipient(pdf_path: str):
    """
    Returns the recipient name, or None if not found.
    """
    try:
        doc = fitz.open(pdf_path)
        total_pages  = len(doc)
        pages_to_check = total_pages if MAX_PAGES_TO_SCAN == 0 else min(MAX_PAGES_TO_SCAN, total_pages)

        recipient = None

        for page_num in range(pages_to_check):
            image = pdf_page_to_image(doc, page_num)
            text  = pytesseract.image_to_string(image, lang=OCR_LANG, config="-c preserve_interword_spaces=1 --psm 6 --oem 3")

            if not recipient:
                m = RECIPIENT_PATTERN.search(text)
                if m:
                    val = re.sub(r'[<>:"/\\|?*]', "", m.group(1)).strip()
                    recipient = val or None

            if recipient:
                break

        doc.close()
        return recipient

    except Exception as exc:
        print(f"OCR Error: {exc}")
        return None


def sanitise(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip()


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    counter = 1
    while os.path.exists(path):
        path = f"{base}_{counter}{ext}"
        counter += 1
    return path


def process_pdf(pdf_path: str, watch_root: str, emit):
    """
    Full pipeline for one PDF. `emit` is a callable(step, status).
    status: 'running' | 'ok' | 'warn' | 'error'
    """
    filename = os.path.basename(pdf_path)
    emit(f"📄 Detected: {filename}", "running")

    # ── OCR ──────────────────────────────────────────────────────────────────
    emit("🔍 Running OCR…", "running")
    recipient = extract_recipient(pdf_path)

    # ── Validate ─────────────────────────────────────────────────────────────
    if not recipient:
        emit("⚠ Could not find: Recipient Name — file left in place.", "warn")
        return

    emit(f"✓ Name: {recipient}", "ok")

    # ── Build destination ─────────────────────────────────────────────────────
    folder_name = sanitise(recipient)
    target_folder = os.path.join(watch_root, folder_name)
    os.makedirs(target_folder, exist_ok=True)

    # Keep original filename but ensure it's unique to prevent overwriting
    dest_path = unique_path(os.path.join(target_folder, f"{folder_name}_UserForm.pdf"))

    # ── Move ──────────────────────────────────────────────────────────────────
    emit(f"📁 Moving to folder: {folder_name}", "running")
    shutil.move(pdf_path, dest_path)
    emit(f"✅ Done → {os.path.basename(dest_path)}", "ok")


# ══════════════════════════════════════════════════════════════════════════════
#  WATCHDOG
# ══════════════════════════════════════════════════════════════════════════════

class PDFHandler(FileSystemEventHandler):
    def __init__(self, watch_root: str, emit):
        super().__init__()
        self.watch_root = watch_root
        self.emit       = emit
        self._seen      = set()
        self._lock      = threading.Lock()

    def on_created(self, event):
        if event.is_directory or not event.src_path.lower().endswith(".pdf"):
            return

        path = event.src_path
        with self._lock:
            if path in self._seen:
                return
            self._seen.add(path)

        self.emit(f"📂 New file spotted: {os.path.basename(path)}", "running")
        time.sleep(FILE_SETTLE_DELAY)

        prev_size = -1
        for _ in range(10):
            try:
                curr_size = os.path.getsize(path)
            except FileNotFoundError:
                return
            if curr_size == prev_size:
                break
            prev_size = curr_size
            time.sleep(1)

        process_pdf(path, self.watch_root, self.emit)

        with self._lock:
            self._seen.discard(path)


class WatcherThread(QThread):
    log_signal = pyqtSignal(str, str)   # (message, status)

    def __init__(self, watch_root: str):
        super().__init__()
        self.watch_root = watch_root
        self._observer  = None
        self._stop_flag = False

    def run(self):
        handler  = PDFHandler(self.watch_root, lambda m, s: self.log_signal.emit(m, s))
        observer = Observer()
        observer.schedule(handler, self.watch_root, recursive=False)
        observer.start()
        self._observer = observer
        self.log_signal.emit(f"👁 Watching: {self.watch_root}", "ok")
        while not self._stop_flag:
            time.sleep(0.5)
        observer.stop()
        observer.join()
        self.log_signal.emit("🛑 Watcher stopped.", "warn")

    def stop(self):
        self._stop_flag = True


# ══════════════════════════════════════════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════════════════════════════════════════

DARK_BG     = "#0F1117"
PANEL_BG    = "#1A1D27"
BORDER      = "#2A2D3A"
ACCENT      = "#6C63FF"
ACCENT_HOVER= "#857DFF"
TEXT_PRI    = "#E8E8F0"
TEXT_SEC    = "#7A7A9A"
GREEN       = "#4ADE80"
AMBER       = "#FBBF24"
RED         = "#F87171"
RUNNING_COL = "#60A5FA"

STATUS_COLORS = {
    "ok":      GREEN,
    "warn":    AMBER,
    "error":   RED,
    "running": RUNNING_COL,
}

STATUS_ICONS = {
    "ok":      "●",
    "warn":    "●",
    "error":   "●",
    "running": "●",
}


class LogRow(QFrame):
    def __init__(self, message: str, status: str):
        super().__init__()
        self.setStyleSheet(f"""
            QFrame {{
                background: {PANEL_BG};
                border: 1px solid {BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(10)

        dot = QLabel(STATUS_ICONS.get(status, "●"))
        dot.setStyleSheet(f"color: {STATUS_COLORS.get(status, TEXT_SEC)}; font-size: 10px;")
        dot.setFixedWidth(14)
        layout.addWidget(dot)

        msg = QLabel(message)
        msg.setStyleSheet(f"color: {TEXT_PRI}; font-size: 13px; background: transparent; border: none;")
        msg.setWordWrap(True)
        layout.addWidget(msg, 1)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF Watcher")
        self.setMinimumSize(700, 560)
        self._watcher_thread = None
        self._build_ui()

    def _build_ui(self):
        self.setStyleSheet(f"QMainWindow {{ background: {DARK_BG}; }}")

        root = QWidget()
        root.setStyleSheet(f"background: {DARK_BG};")
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(28, 28, 28, 28)
        outer.setSpacing(20)

        # ── Header ────────────────────────────────────────────────────────────
        title = QLabel("PDF Watcher")
        title.setStyleSheet(f"color: {TEXT_PRI}; font-size: 22px; font-weight: 700;")
        sub = QLabel("Automatically OCR, rename, and sort incoming PDFs.")
        sub.setStyleSheet(f"color: {TEXT_SEC}; font-size: 13px;")
        outer.addWidget(title)
        outer.addWidget(sub)

        # ── Folder picker ─────────────────────────────────────────────────────
        folder_row = QHBoxLayout()
        folder_row.setSpacing(10)

        self.folder_input = QLineEdit()
        self.folder_input.setPlaceholderText("Select a folder to watch…")
        self.folder_input.setStyleSheet(f"""
            QLineEdit {{
                background: {PANEL_BG};
                border: 1px solid {BORDER};
                border-radius: 8px;
                color: {TEXT_PRI};
                font-size: 13px;
                padding: 10px 14px;
            }}
            QLineEdit:focus {{
                border-color: {ACCENT};
            }}
        """)

        browse_btn = QPushButton("Browse")
        browse_btn.setFixedHeight(40)
        browse_btn.setCursor(Qt.PointingHandCursor)
        browse_btn.setStyleSheet(f"""
            QPushButton {{
                background: {PANEL_BG};
                border: 1px solid {BORDER};
                border-radius: 8px;
                color: {TEXT_SEC};
                font-size: 13px;
                padding: 0 18px;
            }}
            QPushButton:hover {{
                border-color: {ACCENT};
                color: {TEXT_PRI};
            }}
        """)
        browse_btn.clicked.connect(self._browse)

        folder_row.addWidget(self.folder_input, 1)
        folder_row.addWidget(browse_btn)
        outer.addLayout(folder_row)

        # ── Controls ──────────────────────────────────────────────────────────
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(10)

        self.listen_btn = QPushButton("▶  Start Listening")
        self.listen_btn.setFixedHeight(44)
        self.listen_btn.setCursor(Qt.PointingHandCursor)
        self.listen_btn.setStyleSheet(self._btn_style(ACCENT, ACCENT_HOVER))
        self.listen_btn.clicked.connect(self._toggle_watcher)

        self.clear_btn = QPushButton("Clear Log")
        self.clear_btn.setFixedHeight(44)
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid {BORDER};
                border-radius: 8px;
                color: {TEXT_SEC};
                font-size: 13px;
                padding: 0 18px;
            }}
            QPushButton:hover {{
                color: {TEXT_PRI};
                border-color: {TEXT_SEC};
            }}
        """)
        self.clear_btn.clicked.connect(self._clear_log)

        ctrl_row.addWidget(self.listen_btn, 1)
        ctrl_row.addWidget(self.clear_btn)
        outer.addLayout(ctrl_row)

        # ── Status pill ───────────────────────────────────────────────────────
        self.status_label = QLabel("● Idle")
        self.status_label.setStyleSheet(f"color: {TEXT_SEC}; font-size: 12px;")
        outer.addWidget(self.status_label)

        # ── Log area ──────────────────────────────────────────────────────────
        log_label = QLabel("Activity Log")
        log_label.setStyleSheet(f"color: {TEXT_SEC}; font-size: 11px; font-weight: 600; letter-spacing: 1px;")
        outer.addWidget(log_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                border: 1px solid {BORDER};
                border-radius: 10px;
                background: {DARK_BG};
            }}
            QScrollBar:vertical {{
                background: {DARK_BG};
                width: 6px;
                border-radius: 3px;
            }}
            QScrollBar::handle:vertical {{
                background: {BORDER};
                border-radius: 3px;
            }}
        """)

        self.log_container = QWidget()
        self.log_container.setStyleSheet(f"background: {DARK_BG};")
        self.log_layout = QVBoxLayout(self.log_container)
        self.log_layout.setContentsMargins(10, 10, 10, 10)
        self.log_layout.setSpacing(6)
        self.log_layout.addStretch()

        scroll.setWidget(self.log_container)
        outer.addWidget(scroll, 1)

        self._scroll = scroll

    def _btn_style(self, bg, hover):
        return f"""
            QPushButton {{
                background: {bg};
                border: none;
                border-radius: 8px;
                color: white;
                font-size: 14px;
                font-weight: 600;
                padding: 0 24px;
            }}
            QPushButton:hover {{
                background: {hover};
            }}
            QPushButton:disabled {{
                background: {BORDER};
                color: {TEXT_SEC};
            }}
        """

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Watch Folder")
        if folder:
            self.folder_input.setText(folder)

    def _toggle_watcher(self):
        if self._watcher_thread and self._watcher_thread.isRunning():
            self._stop_watcher()
        else:
            self._start_watcher()

    def _start_watcher(self):
        watch_root = self.folder_input.text().strip()
        if not watch_root or not os.path.isdir(watch_root):
            self._add_log("⚠ Please select a valid folder first.", "warn")
            return

        self._watcher_thread = WatcherThread(watch_root)
        self._watcher_thread.log_signal.connect(self._add_log)
        self._watcher_thread.start()

        self.listen_btn.setText("⏹  Stop Listening")
        self.listen_btn.setStyleSheet(self._btn_style("#3B3B5A", "#4A4A6A"))
        self.status_label.setText(f"● Listening")
        self.status_label.setStyleSheet(f"color: {GREEN}; font-size: 12px;")

    def _stop_watcher(self):
        if self._watcher_thread:
            self._watcher_thread.stop()
            self._watcher_thread.wait()
            self._watcher_thread = None

        self.listen_btn.setText("▶  Start Listening")
        self.listen_btn.setStyleSheet(self._btn_style(ACCENT, ACCENT_HOVER))
        self.status_label.setText("● Idle")
        self.status_label.setStyleSheet(f"color: {TEXT_SEC}; font-size: 12px;")

    def _add_log(self, message: str, status: str = "running"):
        row = LogRow(message, status)
        # Insert before the trailing stretch
        count = self.log_layout.count()
        self.log_layout.insertWidget(count - 1, row)
        # Auto-scroll to bottom
        QApplication.processEvents()
        self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()
        )

    def _clear_log(self):
        while self.log_layout.count() > 1:
            item = self.log_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def closeEvent(self, event):
        self._stop_watcher()
        event.accept()


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())