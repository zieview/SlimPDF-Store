"""Build-edition boundary for internal and public Store distributions."""

from __future__ import annotations

import os
from enum import StrEnum


class AppEdition(StrEnum):
    """Supported SlimPDF distribution editions."""

    INTERNAL = "internal"
    STORE = "store"


_EDITION_ENVIRONMENT_VARIABLE = "SLIMPDF_BUILD_EDITION"


def get_app_edition() -> AppEdition:
    """Return the edition selected before application modules are imported."""

    raw_value = os.environ.get(
        _EDITION_ENVIRONMENT_VARIABLE, AppEdition.STORE.value
    ).strip().lower()
    try:
        return AppEdition(raw_value)
    except ValueError:
        return AppEdition.INTERNAL


def is_news_enabled() -> bool:
    """Return whether company-only NEWS features are available."""

    return get_app_edition() is AppEdition.INTERNAL
