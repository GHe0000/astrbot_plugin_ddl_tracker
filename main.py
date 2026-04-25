"""AstrBot plugin entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

# AstrBot may import this file as a top-level module, so we add the plugin
# root directory to sys.path before importing the package modules.
PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from ddl_tracker.plugin import DDLTrackerPlugin

__all__ = ["DDLTrackerPlugin"]
