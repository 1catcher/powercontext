"""Make ``booklib`` importable when pytest collects ``tests/`` directly."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
