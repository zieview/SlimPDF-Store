"""Persist user-specific normalized exclusion-region presets."""

import json
import re
from pathlib import Path

from slimpdf.checker_models import NormalizedRect
from slimpdf.utils.paths import get_app_data_directory


PRESET_NAME_PATTERN = re.compile(r"^[^\\/:*?\"<>|\r\n]{1,60}$")


def list_checker_presets() -> tuple[str, ...]:
    """List available preset names without reading unrelated files."""

    directory = _preset_directory()
    if not directory.is_dir():
        return ()
    return tuple(sorted((path.stem for path in directory.glob("*.json")), key=str.casefold))


def save_checker_preset(name: str, exclusions: tuple[NormalizedRect, ...]) -> None:
    """Store normalized rectangles for reuse on pages of the same format."""

    normalized = name.strip()
    if not PRESET_NAME_PATTERN.fullmatch(normalized):
        raise ValueError("프리셋 이름은 1~60자의 파일명 사용 가능 문자로 입력하세요.")
    directory = _preset_directory()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "exclusions": [
            {"x0": item.x0, "y0": item.y0, "x1": item.x1, "y1": item.y1}
            for item in exclusions
        ],
    }
    (directory / f"{normalized}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_checker_preset(name: str) -> tuple[NormalizedRect, ...]:
    """Load and validate one exclusion-region preset."""

    if name not in list_checker_presets():
        raise ValueError("선택한 제외 영역 프리셋을 찾을 수 없습니다.")
    payload = json.loads((_preset_directory() / f"{name}.json").read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        raise ValueError("지원하지 않는 CHECKER 프리셋 버전입니다.")
    rectangles = tuple(NormalizedRect(**item) for item in payload.get("exclusions", ()))
    for rectangle in rectangles:
        if not (0 <= rectangle.x0 < rectangle.x1 <= 1 and 0 <= rectangle.y0 < rectangle.y1 <= 1):
            raise ValueError("프리셋에 올바르지 않은 제외 영역이 있습니다.")
    return rectangles


def _preset_directory() -> Path:
    return get_app_data_directory() / "checker_presets"

