"""Use the standalone shadow VFS component's native callback test bridge."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sqlite-shadow-vfs/tests"))
from sqlite_vfs_host import PythonVFSCore
