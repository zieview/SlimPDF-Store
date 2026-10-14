"""Select the public Store edition before SlimPDF imports application code."""

from __future__ import annotations

import os


os.environ["SLIMPDF_BUILD_EDITION"] = "store"
