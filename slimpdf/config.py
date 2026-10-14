"""Application-wide configuration values."""

from pathlib import Path

from slimpdf.edition import AppEdition, get_app_edition
from slimpdf.version import APP_VERSION

APP_NAME = "SlimPDF"
APP_EDITION = get_app_edition()
WINDOWS_APP_USER_MODEL_ID = (
    f"SlimPDF.Store.{APP_VERSION}"
    if APP_EDITION is AppEdition.STORE
    else f"SlimPDF.Store.{APP_VERSION}"
)
COPYRIGHT_TEXT = (
    f"© 2026 SlimPDF | Version {APP_VERSION}"
    if APP_EDITION is AppEdition.STORE
    else f"© 2026 SlimPDF | Version {APP_VERSION}"
)
WINDOW_TITLE = APP_NAME
WINDOW_GEOMETRY = "760x980"
WINDOW_MIN_WIDTH = 680
WINDOW_MIN_HEIGHT = 900
WINDOW_BACKGROUND = "#FFFFFF"
CARD_BACKGROUND = "#FFFFFF"
STATUS_BACKGROUND = "#FFFFFF"
TEXT_PRIMARY = "#20242C"
TEXT_SECONDARY = "#667085"
HIGHLIGHT_COLOR = "#FFB6C1"

LOG_DIRECTORY_NAME = "logs"
LOG_FILE_NAME = "slimpdf.log"
LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"

LOGO_FILE_NAME = "SlimPDF_logo.png"
ICON_FILE_NAME = "SlimPDF_icon.ico"

MAX_OUTPUT_SUFFIX = "_compressed_max.pdf"
JPEG_QUALITY = 60
GHOSTSCRIPT_COMMANDS = ("gswin64c", "gswin32c", "gs")
GHOSTSCRIPT_TIMEOUT_SECONDS = 300
CONTEXT_LOCK_TIMEOUT_MS = 600_000
CONTEXT_BATCH_DIRECTORY_NAME = "context_batch"
CONTEXT_BATCH_DEBOUNCE_SECONDS = 1.0
CONTEXT_BATCH_STALE_SECONDS = 86_400
GHOSTSCRIPT_REGISTRY_KEYS = (
    r"SOFTWARE\GPL Ghostscript",
    r"SOFTWARE\WOW6432Node\GPL Ghostscript",
    r"SOFTWARE\AFPL Ghostscript",
)
GHOSTSCRIPT_ARGUMENTS = (
    "-sDEVICE=pdfwrite",
    "-dCompatibilityLevel=1.4",
    "-dPDFSETTINGS=/default",
    "-dSubsetFonts=true",
    "-dCompressFonts=true",
    "-dEmbedAllFonts=true",
    "-dAutoRotatePages=/None",
    "-dNOPAUSE",
    "-dQUIET",
    "-dBATCH",
)


def get_log_file(base_directory: Path) -> Path:
    """Return the log file path beneath an application data directory."""

    return base_directory / LOG_DIRECTORY_NAME / LOG_FILE_NAME
