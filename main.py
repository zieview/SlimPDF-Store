"""SlimPDF executable entry point."""

import logging
import sys
import importlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from collections.abc import Sequence

from slimpdf.models import CompressionMode
from slimpdf.version import APP_VERSION
from slimpdf.services.execution_lock import (
    collect_context_batch,
    context_compression_lock,
    enqueue_context_request,
)
from slimpdf.utils.logging_config import configure_logging
from slimpdf.utils.paths import get_app_data_directory

LOGGER = logging.getLogger(__name__)


class LaunchAction(str, Enum):
    """User-visible operation selected by GUI or Explorer."""

    MAXIMUM = "max"
    UNLOCK = "unlock"
    OCR = "ocr"
    FIND = "find"
    CHECK = "check"


@dataclass(frozen=True, slots=True)
class LaunchOptions:
    """Command-line values controlling application startup."""

    input_paths: tuple[Path, ...]
    mode: CompressionMode
    action: LaunchAction = LaunchAction.MAXIMUM

    @property
    def input_path(self) -> Path | None:
        """Return the first path for backward-compatible single-file callers."""

        return self.input_paths[0] if self.input_paths else None


class CliArgumentError(ValueError):
    """Raised when a direct-execution argument is invalid."""


def parse_launch_options(arguments: Sequence[str]) -> LaunchOptions:
    """Parse and validate the `<pdf> [max]` command format."""

    if len(arguments) == 1:
        return LaunchOptions((), CompressionMode.MAXIMUM, LaunchAction.MAXIMUM)
    values = list(arguments[1:])
    mode_value = "max"
    requested_mode = values[-1].strip().lower()
    if requested_mode == "image":
        raise CliArgumentError("이미지 재압축 기능은 SlimPDF 2.0에서 제거되었습니다.")
    action = LaunchAction.MAXIMUM
    if requested_mode in {"max", "unlock", "ocr", "find", "check"}:
        mode_value = values.pop().strip().lower()
        action = LaunchAction(mode_value)
    if not values:
        raise CliArgumentError(
            "사용법: SlimPDF.exe <파일 경로> [<파일 경로> ...] [max|unlock|ocr|find|check]"
        )
    input_paths = tuple(Path(value) for value in values)
    allowed_suffixes = (
        {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
        if action is LaunchAction.OCR
        else {".pdf"}
    )
    for input_path in input_paths:
        if input_path.suffix.lower() not in allowed_suffixes:
            raise CliArgumentError(f"지원하지 않는 파일 형식입니다: {input_path}")
        if not input_path.is_file():
            raise CliArgumentError(f"PDF 파일을 찾을 수 없습니다: {input_path}")
    compression_mode = CompressionMode.MAXIMUM
    return LaunchOptions(input_paths, compression_mode, action)


def show_startup_error(message: str) -> None:
    """Show a direct-execution error before the main window is created."""

    import tkinter as tk
    from tkinter import messagebox

    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("SlimPDF 실행 오류", message, parent=root)
        root.destroy()
    except tk.TclError:
        LOGGER.error("Unable to display startup error: %s", message)


def run_smoke_test() -> int:
    """Validate packaged runtime imports and Ghostscript discovery without GUI."""

    try:
        from slimpdf.edition import is_news_enabled

        module_names = [
            "pikepdf",
            "pymupdf",
            "tkinterdnd2",
            "PIL.ImageTk",
            "slimpdf.core.ocr_analysis",
            "slimpdf.core.pdf_finder",
            "slimpdf.core.pdf_checker",
            "slimpdf.core.page_organizer",
            "slimpdf.ui.finder_window",
            "slimpdf.ui.checker_window",
        ]
        if is_news_enabled():
            module_names.extend(
                (
                    "holidays",
                    "slimpdf.core.news_backfill",
                    "slimpdf.core.news_scheduled_sync",
                    "slimpdf.repositories.news_backfill_receipt_repository",
                    "slimpdf.repositories.news_backfill_completion_repository",
                )
            )
        for module_name in module_names:
            LOGGER.info("Smoke test stage started: import %s", module_name)
            importlib.import_module(module_name)
            LOGGER.info("Smoke test stage completed: import %s", module_name)
        LOGGER.info("Smoke test stage started: Ghostscript discovery")
        from slimpdf.services.ghostscript_service import find_ghostscript_executable

        ghostscript = find_ghostscript_executable()
        if getattr(sys, "frozen", False) and not is_news_enabled():
            from slimpdf.utils.bundled_engines import find_bundled_engine
            if Path(ghostscript) != find_bundled_engine("engines/ghostscript/bin/gswin64c.exe"):
                raise RuntimeError("Store runtime requires bundled Ghostscript")
        LOGGER.info("Smoke test stage completed: Ghostscript discovery")
        LOGGER.info("Smoke test stage started: Tesseract discovery")
        from slimpdf.services.tesseract_service import (
            find_tesseract_executable,
            list_tesseract_languages,
        )

        tesseract = find_tesseract_executable()
        if getattr(sys, "frozen", False) and not is_news_enabled():
            if tesseract != find_bundled_engine("engines/tesseract/tesseract.exe"):
                raise RuntimeError("Store runtime requires bundled Tesseract")
        languages = set(list_tesseract_languages(tesseract))
        missing_languages = {"kor", "eng"} - languages
        if missing_languages:
            raise RuntimeError(
                "Tesseract language data missing: " + ", ".join(sorted(missing_languages))
            )
        LOGGER.info(
            "Smoke test stage completed: Tesseract discovery path=%s languages=kor+eng",
            tesseract,
        )
    except Exception:
        LOGGER.exception("Packaged runtime smoke test failed")
        return 1
    LOGGER.info("Packaged runtime smoke test passed")
    return 0


def run_application(options: LaunchOptions) -> int:
    """Build the Tk application and execute one launch request."""

    from slimpdf.ui.app import SlimPdfApp, create_root

    root, dnd_module = create_root()
    app = SlimPdfApp(root, dnd_module, options.mode)
    exit_code = 0

    if options.input_paths and options.action is LaunchAction.FIND:
        root.after(0, lambda: app.open_finder(options.input_paths))
    elif options.input_paths and options.action is LaunchAction.CHECK:

        def open_checker() -> None:
            if not app.open_checker(options.input_paths):
                root.destroy()

        root.after(0, open_checker)
    elif options.input_paths:
        def compress_cli_files() -> None:
            nonlocal exit_code
            if options.action is LaunchAction.UNLOCK:
                succeeded = app.unlock_batch_and_report(options.input_paths)
            elif options.action is LaunchAction.OCR:
                succeeded = app.ocr_batch_and_report(options.input_paths)
            else:
                succeeded = app.compress_batch_and_report(options.input_paths)
            if not succeeded:
                exit_code = 1
            root.destroy()

        root.after(0, compress_cli_files)

    root.mainloop()
    return exit_code


def run_headless_compression(arguments: Sequence[str]) -> int:
    """Run a real compression request without constructing Tk UI."""

    allow_signature_invalidation = (
        len(arguments) == 5 and arguments[4] == "--allow-signature-invalidation"
    )
    if (
        len(arguments) not in {4, 5}
        or arguments[3].strip().lower() != "max"
        or (len(arguments) == 5 and not allow_signature_invalidation)
    ):
        LOGGER.error(
            "Headless compression usage: --headless-compress <PDF> <max> "
            "[--allow-signature-invalidation]"
        )
        return 2
    try:
        options = parse_launch_options([arguments[0], arguments[2], arguments[3]])
        from slimpdf.core.compression import compress_pdf
        from slimpdf.models import CompressionRequest

        with context_compression_lock():
            result = compress_pdf(
                CompressionRequest(
                    options.input_path,
                    options.mode,
                    allow_signature_invalidation=allow_signature_invalidation,
                )
            )
        LOGGER.info("Headless compression passed: output=%s", result.output_path)
        return 0
    except Exception:
        LOGGER.exception("Headless compression failed")
        return 1


def run_headless_ocr(arguments: Sequence[str]) -> int:
    """Run OCR without Tk so packaged feature verification can inspect outputs."""

    if len(arguments) != 3:
        LOGGER.error("Headless OCR usage: --headless-ocr <PDF-or-image>")
        return 2
    try:
        from slimpdf.core.ocr_analysis import analyze_ocr
        from slimpdf.ocr_models import OcrRequest

        result = analyze_ocr(OcrRequest(Path(arguments[2])))
        LOGGER.info(
            "Headless OCR passed: pdf=%s text=%s", result.output_pdf_path, result.output_text_path
        )
        return 0
    except Exception:
        LOGGER.exception("Headless OCR failed")
        return 1


def run_headless_engineering(arguments: Sequence[str]) -> int:
    """Run read-only engineering analysis for packaged end-to-end verification."""

    if len(arguments) != 4:
        LOGGER.error(
            "Headless Engineering usage: --headless-engineering <PDF> <output-directory>"
        )
        return 2
    try:
        from slimpdf.core.engineering_analysis import analyze_engineering_pdf

        result = analyze_engineering_pdf(Path(arguments[2]), Path(arguments[3]))
        LOGGER.info(
            "Headless Engineering analysis passed: output=%s pages=%d texts=%d",
            result.output_directory,
            result.page_count,
            result.text_count,
        )
        return 0
    except Exception:
        LOGGER.exception("Headless Engineering analysis failed")
        return 1


def run_headless_find(arguments: Sequence[str]) -> int:
    """Run Finder without Tk and write machine-readable JSON/XLSX results."""

    if len(arguments) != 5:
        LOGGER.error("Headless Finder usage: --headless-find <PDF> <term> <result.json>")
        return 2
    try:
        from slimpdf.core.pdf_finder import search_pdfs
        from slimpdf.finder_models import FinderRequest
        from slimpdf.repositories.finder_xlsx_repository import export_finder_result

        result = search_pdfs(FinderRequest((Path(arguments[2]),), (arguments[3],)))
        json_path = Path(arguments[4])
        xlsx_path = export_finder_result(result, json_path.with_suffix(".xlsx"))
        payload = {
            "cancelled": result.cancelled,
            "xlsx": str(xlsx_path),
            "matches": [
                {
                    "path": str(item.input_path),
                    "page": item.page_number,
                    "terms": item.matched_terms,
                    "excerpt": item.excerpt,
                    "sentence": getattr(item, "sentence", item.excerpt),
                    "rectangle": getattr(item, "rectangle", None),
                    "used_ocr": item.used_ocr,
                }
                for item in result.matches
            ],
            "errors": [item.error for item in result.diagnostics if item.error],
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        if payload["errors"]:
            LOGGER.error("Headless Finder completed with errors: %s", payload["errors"])
            return 1
        LOGGER.info("Headless Finder passed: matches=%d", len(result.matches))
        return 0
    except Exception:
        LOGGER.exception("Headless Finder failed")
        return 1


def run_headless_check(arguments: Sequence[str]) -> int:
    """Run revision comparison without Tk for packaged validation."""

    if len(arguments) != 5:
        LOGGER.error("Headless Checker usage: --headless-check <old.pdf> <latest.pdf> <revision>")
        return 2
    try:
        from slimpdf.checker_models import CheckerRequest
        from slimpdf.core.pdf_checker import compare_pdf_revisions

        result = compare_pdf_revisions(
            CheckerRequest(Path(arguments[2]), Path(arguments[3]), arguments[4])
        )
        LOGGER.info(
            "Headless Checker passed: output=%s changes=%d",
            result.output_path,
            len(result.changes),
        )
        return 0
    except Exception:
        LOGGER.exception("Headless Checker failed")
        return 1


def run_headless_organize(arguments: Sequence[str]) -> int:
    """Run page orientation cleanup without constructing the Tk interface."""

    if len(arguments) != 4 or arguments[3] not in {"auto-text", "portrait"}:
        LOGGER.error(
            "Headless page organizer usage: --headless-organize <PDF> <auto-text|portrait>"
        )
        return 2
    try:
        from slimpdf.core.page_organizer import organize_pdf_pages
        from slimpdf.page_models import PageOrganizationMode, PageOrganizationRequest

        result = organize_pdf_pages(
            PageOrganizationRequest(Path(arguments[2]), PageOrganizationMode(arguments[3]))
        )
        LOGGER.info(
            "Headless page organizer passed: output=%s rotated_pages=%s",
            result.output_path,
            result.rotated_pages,
        )
        return 0
    except Exception:
        LOGGER.exception("Headless page organizer failed")
        return 1


def run_scheduled_news() -> int:
    """Company-only feature is unavailable in this public source."""
    return 2


def run_scheduled_news_backup(arguments: Sequence[str]) -> int:
    """Company-only feature is unavailable in this public source."""
    return 2


def run_headless_news_smoke(arguments: Sequence[str]) -> int:
    """Company-only feature is unavailable in this public source."""
    return 2


def read_build_info() -> dict[str, str]:
    """Read metadata embedded by the release build."""

    from slimpdf.edition import get_app_edition

    resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    path = resource_root / "build_info.json"
    if not path.is_file():
        result = {
            "version": APP_VERSION,
            "source_sha256": "DEVELOPMENT",
            "built_at_utc": "unknown",
        }
    else:
        result = json.loads(path.read_text(encoding="utf-8"))
    result["edition"] = get_app_edition().value
    return result


def main(arguments: Sequence[str] | None = None) -> int:
    """Configure logging and serialize Explorer-triggered compression jobs."""

    arguments = sys.argv if arguments is None else arguments
    from slimpdf.edition import is_news_enabled

    log_path = configure_logging(get_app_data_directory())
    LOGGER.info("SlimPDF starting; log=%s", log_path)
    if len(arguments) == 2 and arguments[1] == "--smoke-test":
        return run_smoke_test()
    if len(arguments) == 2 and arguments[1] in {"--version", "--build-info"}:
        print(json.dumps(read_build_info(), ensure_ascii=False))
        return 0
    if len(arguments) == 3 and arguments[1] == "--build-info-file":
        Path(arguments[2]).write_text(json.dumps(read_build_info(), ensure_ascii=False), encoding="utf-8")
        return 0
    if len(arguments) >= 2 and arguments[1] == "--headless-compress":
        return run_headless_compression(arguments)
    if len(arguments) >= 2 and arguments[1] == "--headless-ocr":
        return run_headless_ocr(arguments)
    if len(arguments) >= 2 and arguments[1] == "--headless-engineering":
        return run_headless_engineering(arguments)
    if len(arguments) >= 2 and arguments[1] == "--headless-find":
        return run_headless_find(arguments)
    if len(arguments) >= 2 and arguments[1] == "--headless-check":
        return run_headless_check(arguments)
    if len(arguments) >= 2 and arguments[1] == "--headless-organize":
        return run_headless_organize(arguments)
    news_command = len(arguments) >= 2 and arguments[1] in {
        "--headless-news-smoke",
        "--scheduled-news-sync",
        "--scheduled-news-backup",
    }
    if news_command and not is_news_enabled():
        LOGGER.error("NEWS commands are unavailable in the Microsoft Store edition")
        return 2
    if len(arguments) >= 2 and arguments[1] == "--headless-news-smoke":
        return run_headless_news_smoke(arguments)
    if len(arguments) == 2 and arguments[1] == "--scheduled-news-sync":
        return run_scheduled_news()
    if len(arguments) >= 2 and arguments[1] == "--scheduled-news-backup":
        return run_scheduled_news_backup(arguments)
    try:
        options = parse_launch_options(arguments)
    except CliArgumentError as error:
        LOGGER.error("Invalid command line: %s", error)
        show_startup_error(str(error))
        return 2
    if not options.input_paths:
        exit_code = run_application(options)
    elif options.action in {LaunchAction.FIND, LaunchAction.CHECK}:
        if len(options.input_paths) > 1:
            exit_code = run_application(options)
        else:
            request_path = enqueue_context_request(options.input_paths[0], options.action)
            with context_compression_lock():
                batch_paths = collect_context_batch(request_path, options.action)
            exit_code = (
                run_application(LaunchOptions(batch_paths, options.mode, options.action))
                if batch_paths
                else 0
            )
    else:
        if len(options.input_paths) > 1:
            with context_compression_lock():
                exit_code = run_application(options)
            LOGGER.info("SlimPDF stopped")
            return exit_code
        request_path = enqueue_context_request(options.input_paths[0], options.action)
        with context_compression_lock():
            batch_paths = collect_context_batch(request_path, options.action)
            if not batch_paths:
                exit_code = 0
            else:
                exit_code = run_application(LaunchOptions(batch_paths, options.mode, options.action))
    LOGGER.info("SlimPDF stopped")
    return exit_code


def executable_entrypoint() -> None:
    """Run the application and use Python's normal shutdown sequence."""

    raise SystemExit(main())


if __name__ == "__main__":
    executable_entrypoint()
