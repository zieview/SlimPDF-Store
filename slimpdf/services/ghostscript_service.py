"""Ghostscript discovery and PDF compression integration."""

import logging
import os
import shutil
import subprocess
import uuid
import sys
from pathlib import Path

from slimpdf.config import (
    GHOSTSCRIPT_ARGUMENTS,
    GHOSTSCRIPT_COMMANDS,
    GHOSTSCRIPT_REGISTRY_KEYS,
    GHOSTSCRIPT_TIMEOUT_SECONDS,
)
from slimpdf.services.pdf_service import (
    compress_preserving_pdf_objects,
    create_clean_pdf,
    pdf_requires_object_preserving_compression,
)
from slimpdf.utils.temp_paths import managed_temporary_directory
from slimpdf.models import CompressionDiagnostics

LOGGER = logging.getLogger(__name__)
from slimpdf.utils.bundled_engines import find_bundled_engine


class GhostscriptNotFoundError(EnvironmentError):
    """Raised when no supported Ghostscript executable is available."""


class GhostscriptExecutionError(RuntimeError):
    """Raised when Ghostscript fails to create the requested PDF."""


def find_ghostscript_executable() -> str:
    """Find Ghostscript through PATH, registry, or standard install folders."""

    bundled = find_bundled_engine("engines/ghostscript/bin/gswin64c.exe")
    if bundled is not None:
        LOGGER.info("Bundled Ghostscript executable: %s", bundled)
        return str(bundled)
    for command in GHOSTSCRIPT_COMMANDS:
        executable = shutil.which(command)
        if executable:
            LOGGER.info("Ghostscript executable detected: %s", executable)
            return executable
    registry_executable = _find_ghostscript_in_registry()
    if registry_executable:
        LOGGER.info("Ghostscript executable detected through registry: %s", registry_executable)
        return str(registry_executable)
    standard_executable = _find_ghostscript_in_standard_paths()
    if standard_executable:
        LOGGER.info("Ghostscript executable detected in standard path: %s", standard_executable)
        return str(standard_executable)
    raise GhostscriptNotFoundError(
        "Ghostscript 실행 파일을 찾을 수 없습니다. Ghostscript 설치 후 다시 시도하세요."
    )


def _find_ghostscript_in_registry() -> Path | None:
    """Resolve GS_DLL registry entries to the adjacent console executable."""

    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key_name in GHOSTSCRIPT_REGISTRY_KEYS:
            try:
                with winreg.OpenKey(hive, key_name) as root_key:
                    index = 0
                    versions: list[str] = []
                    while True:
                        try:
                            versions.append(winreg.EnumKey(root_key, index))
                            index += 1
                        except OSError:
                            break
                    for version in sorted(versions, key=_version_key, reverse=True):
                        with winreg.OpenKey(root_key, version) as version_key:
                            dll_path, _ = winreg.QueryValueEx(version_key, "GS_DLL")
                        executable = Path(os.path.expandvars(dll_path)).with_name("gswin64c.exe")
                        if not executable.is_file():
                            executable = executable.with_name("gswin32c.exe")
                        if executable.is_file():
                            return executable
            except (OSError, ValueError):
                continue
    return None


def _find_ghostscript_in_standard_paths() -> Path | None:
    """Search the default 64-bit and 32-bit Ghostscript install locations."""

    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
    candidates: list[Path] = []
    for root in filter(None, roots):
        gs_root = Path(root) / "gs"
        candidates.extend(gs_root.glob("gs*/bin/gswin64c.exe"))
        candidates.extend(gs_root.glob("gs*/bin/gswin32c.exe"))
    return max(candidates, default=None, key=lambda path: _version_key(path.parent.parent.name))


def _version_key(value: str | Path) -> tuple[int, ...]:
    """Convert Ghostscript version text such as gs10.10.0 to a numeric tuple."""

    import re

    numbers = re.findall(r"\d+", str(value))
    return tuple(int(number) for number in numbers) or (0,)


def build_ghostscript_command(executable: str, input_path: Path, output_path: Path) -> list[str]:
    """Build the legacy-compatible Ghostscript argument list."""

    return [
        executable,
        *GHOSTSCRIPT_ARGUMENTS,
        f"-sOutputFile={output_path}",
        str(input_path),
    ]


def _restore_pdf_attachments(input_path: Path, ghostscript_output: Path, output_path: Path) -> None:
    """Restore embedded files that Ghostscript intentionally omits when rewriting a PDF."""

    import pikepdf

    with pikepdf.open(input_path) as source_pdf, pikepdf.open(ghostscript_output) as output_pdf:
        names = output_pdf.Root.get("/Names")
        if names is not None and names.get("/EmbeddedFiles") is not None:
            del names["/EmbeddedFiles"]
        for attachment_name in source_pdf.attachments:
            source_spec = source_pdf.attachments[attachment_name]
            source_file = source_spec.get_file()
            data = source_file.read_bytes()
            try:
                relationship = source_spec.relationship
            except (AttributeError, RuntimeError, ValueError):
                relationship = None
            if relationship is not None:
                relationship = pikepdf.Name(str(relationship))
            restored_spec = pikepdf.AttachedFileSpec(
                output_pdf,
                data,
                description=source_spec.description or "",
                filename=source_spec.filename or str(attachment_name),
                relationship=relationship,
            )
            output_pdf.attachments[str(attachment_name)] = restored_spec
        output_pdf.save(output_path)


def compress_with_ghostscript(
    input_path: Path,
    output_path: Path,
    *,
    allow_signature_invalidation: bool = False,
) -> CompressionDiagnostics:
    """Clean a PDF, compress it with Ghostscript, and copy it to output_path."""

    if pdf_requires_object_preserving_compression(input_path):
        LOGGER.info(
            "Interactive PDF objects detected; using object-preserving stream compression: %s",
            input_path,
        )
        diagnostics = compress_preserving_pdf_objects(input_path, output_path)
        return CompressionDiagnostics(
            images_processed=diagnostics.images_processed,
            images_skipped=diagnostics.images_skipped,
            images_failed=diagnostics.images_failed,
            metadata_cleanup_failed=diagnostics.metadata_cleanup_failed,
            warnings=diagnostics.warnings
            + (
                "PDF 주석·스탬프·입력 필드의 내용과 표시 설정을 보존하기 위해 손실 없는 객체 보존 압축을 적용했습니다.",
            ),
        )

    executable = find_ghostscript_executable()
    token = uuid.uuid4().hex
    # Ghostscript on Windows is unreliable with deeply nested UNC paths.  Keep
    # its private input/output files in a short local directory; only the
    # validated, attachment-restored result is written to the caller's target.
    with managed_temporary_directory("slimpdf-gs-") as workspace_path:
        cleaned_input = workspace_path / f"clean-{token}.pdf"
        temp_output = workspace_path / f"output-{token}.pdf"
        diagnostics = create_clean_pdf(input_path, cleaned_input)
        command = build_ghostscript_command(executable, cleaned_input, temp_output)
        LOGGER.info(
            "Running Ghostscript with local workspace: input=%s workspace=%s",
            input_path,
            workspace_path,
        )
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=GHOSTSCRIPT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            LOGGER.error("Ghostscript timed out after %d seconds", GHOSTSCRIPT_TIMEOUT_SECONDS)
            raise GhostscriptExecutionError(
                f"Ghostscript 압축이 {GHOSTSCRIPT_TIMEOUT_SECONDS}초 안에 완료되지 않아 중단했습니다."
            ) from error
        except subprocess.CalledProcessError as error:
            details = (error.stderr or error.stdout or str(error)).strip()
            LOGGER.exception("Ghostscript failed: %s", details)
            raise GhostscriptExecutionError(f"Ghostscript 오류: {details}") from error

        if not temp_output.is_file():
            raise GhostscriptExecutionError("Ghostscript가 결과 PDF를 생성하지 않았습니다.")
        _restore_pdf_attachments(input_path, temp_output, output_path)
    return diagnostics
