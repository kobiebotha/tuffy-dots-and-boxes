#!/usr/bin/env python3
"""Offline SQLite and optional PowerSync checks in a bounded host process."""
import argparse
import ctypes as c
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def qualify(variant):
    from native_sqlite_host import HostCore

    core = HostCore()

    def scalar(sql):
        rows = core.exec(sql)
        assert len(rows) == 1 and len(rows[0]) == 1
        value = rows[0][0]
        if value is not None and value.lstrip("-").isdigit():
            return int(value)
        return value

    with tempfile.TemporaryDirectory(prefix="native-sqlite-host-") as folder:
        path = str(Path(folder) / "state.sqlite")
        core.open(path)
        try:
            assert scalar("SELECT sqlite_version()") == "3.54.1"
            if variant == "powersync":
                import powersync_fixture
                print(powersync_fixture.fresh(core.exec, scalar))
            else:
                core.exec("CREATE TABLE notes(id TEXT PRIMARY KEY, title TEXT)")
                core.exec("INSERT INTO notes VALUES('hello','SQLite without PowerSync')")
                core.exec("BEGIN; UPDATE notes SET title='uncommitted'; ROLLBACK")
                assert scalar("SELECT title FROM notes") == "SQLite without PowerSync"
                assert scalar("SELECT json_extract('{\"value\":7}', '$.value')") == 7
                try:
                    core.exec("SELECT powersync_rs_version()")
                except RuntimeError as exc:
                    assert "no such function" in str(exc)
                else:
                    raise AssertionError("SQLite-only build unexpectedly includes PowerSync")
            assert scalar("PRAGMA integrity_check") == "ok"
            try:
                core.exec("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<33) SELECT x FROM n")
            except ValueError as exc:
                assert "row limit" in str(exc)
            else:
                raise AssertionError("Host adapter did not enforce its result bound")
        finally:
            core.close()
        core.open(path)
        try:
            if variant == "powersync":
                print(powersync_fixture.reopened(core.exec, scalar))
            else:
                assert scalar("SELECT title FROM notes") == "SQLite without PowerSync"
        finally:
            core.close()
    core.library.sqlite3_memory_highwater.restype = c.c_longlong
    peak = core.library.sqlite3_memory_highwater(0)
    assert peak <= 256 * 1024, peak
    print("PASS %s: SQL, rollback, reopen, integrity, 32-row bound; arena peak %d bytes" % (variant, peak))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("sqlite", "powersync"), default="sqlite")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    library = root / "build/host" / args.variant / ("native_sqlite_host" + suffix)
    if not library.is_file():
        parser.exit(1, "Build the matching host variant with tools/build.py first.\n")
    if args.child:
        qualify(args.variant)
    else:
        environment = dict(os.environ, POWERSYNC_TEST_LIBRARY=str(library))
        subprocess.run([sys.executable, __file__, "--variant", args.variant, "--child"],
                       check=True, timeout=30, env=environment)


if __name__ == "__main__":
    main()
