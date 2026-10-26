"""Plain persistent SQLite through ShadowVFS; no PowerSync or game imports."""
import os

import native_sqlite
from sqlite_shadow_vfs import ShadowVFS

ROOT = "/state/shadow_vfs_example"
PATH = ROOT + "/notes.sqlite"
CAPACITY = 128 * 1024
HEADROOM = 64 * 1024


def free_bytes(path):
    info = os.statvfs(path)
    return info[1] * info[4]


def main():
    try:
        os.stat(ROOT)
    except OSError as exc:
        if exc.args[0] != 2:
            raise
        if free_bytes("/state") < CAPACITY + HEADROOM:
            raise OSError("Not enough storage for the example")
        os.mkdir(ROOT)
    try:
        journal = os.stat(PATH + "-journal")
    except OSError as exc:
        if exc.args[0] != 2:
            raise
    else:
        if journal[6]:
            raise OSError("Interrupted write; retain the database and journal for recovery")
    # Conservative initial admission; later transactions use the logical size.
    if free_bytes(ROOT) < CAPACITY + HEADROOM:
        raise OSError("Not enough storage for the example")
    io = ShadowVFS(PATH, max_database_bytes=CAPACITY)
    native_sqlite.configure(io)
    try:
        native_sqlite.open(PATH)
        native_sqlite.exec("PRAGMA page_size=1024")
        native_sqlite.exec("PRAGMA max_page_count=%d" % (CAPACITY // 1024))
        native_sqlite.exec("PRAGMA journal_mode=DELETE")
        native_sqlite.exec("PRAGMA synchronous=FULL")
        native_sqlite.exec("PRAGMA temp_store=MEMORY")
        if free_bytes(ROOT) < io.db_size + HEADROOM:
            raise OSError("Not enough space for the next database snapshot")
        native_sqlite.exec("BEGIN IMMEDIATE")
        try:
            native_sqlite.exec("CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, title TEXT)")
            native_sqlite.exec("INSERT OR REPLACE INTO notes VALUES(1,'SQLite without PowerSync')")
            native_sqlite.exec("COMMIT")
        except Exception:
            native_sqlite.exec("ROLLBACK")
            raise
        print(native_sqlite.exec("SELECT id,title FROM notes LIMIT 16"))
    finally:
        native_sqlite.close()
        io.close_all()


if __name__ == "__main__":
    main()
