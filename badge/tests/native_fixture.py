"""Use the standalone native-sqlite component's pinned host test library."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "native-sqlite/tests"))
from native_sqlite_host import HostCore, LIBRARY, SQLITE_INCLUDE
