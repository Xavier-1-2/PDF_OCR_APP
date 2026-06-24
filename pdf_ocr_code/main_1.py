import os
import re
import time
import logging
import difflib
from pathlib import Path
import tempfile
import ocrmypdf
import threading
import pytesseract
from pdf2image import convert_from_path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ==================================================
# CONFIG
# ==================================================

_in_progress: set = set()
_in_progress_lock = threading.Lock()


BASE_DIR = Path(__file__).resolve().parent
WATCH_FOLDER = BASE_DIR / "watch_folder"



TESSERACT_PATH = BASE_DIR / "tesseract" / "tesseract.exe"
pytesseract.pytesseract.tesseract_cmd = str(TESSERACT_PATH)

os.environ["PATH"] += os.pathsep + str(TESSERACT_PATH.parent)



POPPLER_PATH = BASE_DIR / "poppler" / "Library" / "bin"



# ==================================================
# LOGGING
# ==================================================

logging.basicConfig(
  level=logging.INFO,
  format="%(asctime)s - %(levelname)s - %(message)s"
)

# ==================================================
# NAME EXTRACTION
# ==================================================

# The exact label we're hunting for in the OCR output.
# All comparisons are done in uppercase.
_RECIPIENT_LABEL = "RECIPIENT NAME"


def _fuzzy_label_match(line: str, threshold: float = 0.80) -> bool:
    upper = line.upper()
    tlen  = len(_RECIPIENT_LABEL)

    # 1. Exact hit – skip all the work
    if _RECIPIENT_LABEL in upper:
        return True

    # 2. Sliding-window fuzzy compare
    for start in range(len(upper)):
        chunk = upper[start : start + tlen]
        if len(chunk) < tlen - 3:        # window too small to be useful
            break
        ratio = difflib.SequenceMatcher(
            None, _RECIPIENT_LABEL, chunk
        ).ratio()
        if ratio >= threshold:
            return True

    return False


def extract_name(text: str):
   
    lines = [line.replace("|", "").strip() for line in text.splitlines()]

    for i, line in enumerate(lines):
        if not line:
            continue

        # Skip lines that don't look like our label
        if not _fuzzy_label_match(line):
            continue

        # ── Same-line extraction: "Recipient Name: John Smith" ──
        colon_pos = line.find(":")
        if colon_pos != -1:
            inline = line[colon_pos + 1:].strip()
            if inline:
                cleaned = clean_name_candidate(inline)

                if cleaned:
                    logging.info("Name found on same line as label.")
                    return cleaned.title()

        # ── Next-line extraction ─────────────────────────────────
        # Walk forward, skipping blank lines, and inspect the first
        # real line we find.
        for j in range(i + 1, min(i + 5, len(lines))):
            candidate = lines[j]
            if not candidate:
                continue                       # blank — keep scanning
            cleaned = clean_name_candidate(candidate)

            if cleaned:
              logging.info("Name found on line after label.")
              return cleaned.title()
            break                              # non-empty but invalid — stop
        
        # FALLBACK
    lines = [line.replace("|", "").strip() for line in text.splitlines()]

    for i in range(len(lines) - 1):
        current = clean_name_candidate(lines[i])

        if not current:
            continue

        next_line = lines[i + 1].upper()

        job_keywords = {
            "JOB TITLE",
            "SPECIALIST",
            "ADMINISTRATOR",
            "MANAGER",
            "SUPERVISOR",
            "COORDINATOR",
            "OFFICER",
            "COURIER"
        }

        if any(word in next_line for word in job_keywords):
            logging.info("Name found using fallback.")
            return current.title()

    logging.warning("Recipient name could not be extracted.")
    return None


# ==================================================
# OCR
# ==================================================

def ocr_pdf(pdf_path):
  """
  Convert PDF pages to images and OCR them.
  """

  pages = convert_from_path(
    pdf_path,
    dpi=300,
    poppler_path=POPPLER_PATH
  )

  text = ""

  for page in pages:
    text += pytesseract.image_to_string(page)
    text += "\n"

  return text


# ==================================================
# FILE RENAMING
# ==================================================

def make_safe_filename(name):
  """
  Convert name into safe filename.
  """

  safe_name = re.sub(
    r'[^a-zA-Z0-9 _-]',
    '',
    name
  )

  safe_name = safe_name.replace(" ", "_")

  return safe_name


def get_unique_filename(folder, filename):
  """
  Prevent duplicates:
  John_Smith.pdf
  John_Smith_1.pdf
  John_Smith_2.pdf
  """

  candidate = filename
  counter = 1

  while os.path.exists(
    os.path.join(folder, candidate)
  ):
    stem = Path(filename).stem
    candidate = f"{stem}_{counter}.pdf"
    counter += 1

  return candidate


# ==================================================
# PROCESS PDF
# ==================================================

def process_pdf(pdf_path):
  """
  OCR PDF -> Extract Name -> Rename
  """

  try:

    logging.info(
      f"Processing PDF: {pdf_path}"
    )

    # Create searchable OCR PDF
    temp_pdf = tempfile.NamedTemporaryFile(
      suffix=".pdf",
      delete=False
    ).name

    ocrmypdf.ocr(
      pdf_path,
      temp_pdf,
      force_ocr=True
    )

    # Replace original PDF with OCR version
    os.remove(pdf_path)
    os.rename(temp_pdf, pdf_path)

    logging.info(
      "OCR PDF created"
    )

    # Read text for name extraction
    text = ocr_pdf(pdf_path)

    if not text.strip():
      logging.error(
        f"No OCR text found: {pdf_path}"
      )
      return
    
    print("\nOCR TEXT:")
    print("=" * 80)
    print(text)
    print("=" * 80)
    name = extract_name(text)

    if not name:
      logging.error(
        f"No name found in PDF: {pdf_path}"
      )
      return

    folder = os.path.dirname(pdf_path)

    safe_name = make_safe_filename(name)

    new_filename = f"{safe_name}.pdf"

    new_filename = get_unique_filename(
      folder,
      new_filename
    )

    new_path = os.path.join(
      folder,
      new_filename
    )

    os.rename(
      pdf_path,
      new_path
    )

    logging.info(
      f"Renamed -> {new_filename}"
    )

  except Exception as e:

    logging.exception(
      f"Failed processing {pdf_path}: {e}"
    )


# ==================================================
# WATCHDOG HANDLER
# ==================================================
def clean_name_candidate(text):
    words = re.findall(r"[A-Za-z]+", text)

    # remove common OCR garbage
    blacklist = {
        # "AS", "AE", "EA", "EE", "OE",
        "THE", "AND", "AS",
    }

    words = [
        w for w in words
        if w.upper() not in blacklist
    ]

    if 2 <= len(words) <= 4:
        return " ".join(words)

    return None

class PDFHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        file_path = event.src_path
        if not file_path.lower().endswith(".pdf"):
            return

        time.sleep(2)

        # Guard 1: file was already renamed by a prior run
        if not os.path.exists(file_path):
            logging.info(f"Skipped (already renamed): {os.path.basename(file_path)}")
            return

        # Guard 2: file is currently being processed
        with _in_progress_lock:
            if file_path in _in_progress:
                logging.info(f"Skipped (already processing): {os.path.basename(file_path)}")
                return
            _in_progress.add(file_path)

        try:
            process_pdf(file_path)
        finally:
            with _in_progress_lock:
                _in_progress.discard(file_path)

# ==================================================
# START WATCHER
# ==================================================

def process_existing_pdfs():
  """
  Process PDFs already present
  when script starts.
  """

  for file in os.listdir(WATCH_FOLDER):

    if file.lower().endswith(".pdf"):

        path = os.path.join(
                WATCH_FOLDER,
                file
            )

        process_pdf(path)


def main():

  Path(WATCH_FOLDER).mkdir(
    parents=True,
    exist_ok=True
  )

  logging.info(
    f"Watching folder:\n{WATCH_FOLDER}"
  )

  # Process anything already there
  process_existing_pdfs()

  event_handler = PDFHandler()

  observer = Observer()

  observer.schedule(
    event_handler,
    WATCH_FOLDER,
    recursive=False
  )

  observer.start()

  try:

    while True:
      time.sleep(1)

  except KeyboardInterrupt:

    observer.stop()

    observer.join()


if __name__ == "__main__":
    main()