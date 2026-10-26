"""Run with the SQLite-only or PowerSync variant of native_sqlite on sys.path."""
import native_sqlite

native_sqlite.open(":memory:")
try:
    native_sqlite.exec("CREATE TABLE notes(id INTEGER PRIMARY KEY, title TEXT)")
    native_sqlite.exec("INSERT INTO notes(title) VALUES('Hello Tufty')")
    print(native_sqlite.exec("SELECT id, title FROM notes"))
finally:
    native_sqlite.close()
