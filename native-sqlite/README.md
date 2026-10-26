# native-sqlite for Tufty 2350

A standalone MicroPython native SQLite module for Tufty 2350 / Badgeware 1.29.0.
Copy this directory into another project to build it. No sibling application,
firmware checkout, raw-flash driver, or Python PowerSync SDK is required.

Both build variants import as `native_sqlite`:

| Variant | Contents | Prebuilt module |
| --- | --- | --- |
| `sqlite` | SQLite 3.54.1 | `prebuilt/sqlite/native_sqlite.mpy` |
| `powersync` | SQLite 3.54.1 and the PowerSync SQLite extension 0.5.4 | `prebuilt/powersync/native_sqlite.mpy` |

The extension variant provides PowerSync's SQL functions. Network transport,
credentials, sync scheduling and uploads belong to the calling SDK/application.
It is separate from a future native PowerSync SDK.

## Use a prebuilt module

Put **one** variant's `native_sqlite.mpy` on your app's import path. Include
`THIRD_PARTY_NOTICES.txt` when redistributing it. `examples/memory.py` runs with
either variant and does not require the Dots & Boxes app:

```python
import native_sqlite

native_sqlite.open(":memory:")
try:
    print(native_sqlite.exec("SELECT sqlite_version()"))
finally:
    native_sqlite.close()
```

This API exposes one connection per loaded module:

- `configure(callback)`: set the filesystem/runtime callback before opening a
  persistent database. Close the connection before changing callbacks.
- `open(path)`: open a database, registering the extension in the PowerSync
  variant. `:memory:` needs no file callback.
- `exec(sql)`: execute SQL, returning rows of UTF-8 text or `None` for SQL NULL.
  Numbers also return as text. Results are limited to 32 rows **per statement**;
  page larger queries. Bind parameters and typed BLOB results are not exposed.
- `close()`: close the connection.
- `stats()`: return `(live_sqlite_bytes, peak_sqlite_bytes, wsd_bytes, wsd_entries,
  code_address, arena_address, wsd_address, sqlite_version)`.

The allocator uses a fixed 256 KiB arena, shared with the optional Rust extension,
and a separate 16 KiB writable-static-data buffer. SQLite is single-threaded,
uses memory-only temporary storage, and omits WAL and dynamic extension loading.
`src/config.h` contains the full feature and size profile.

## Persistent storage callback

The native module delegates storage to Python; it does not mount, format or
select a flash partition. Pass `callback(op, item, arg1, arg2)` to `configure`.
It must return `(sqlite_result_code, value)` and must never re-enter SQLite.
Nonzero result codes report errors; the C bridge catches Python exceptions so
they cannot unwind through SQLite/Rust.

| `op` | Operation | Input | Success value |
| --- | --- | --- | --- |
| 0 | Open | path, SQLite open flags | integer handle |
| 1 | Close | handle | ignored |
| 2 | Read | handle, offset, byte count | bytes/buffer; short reads are zero-filled by SQLite's bridge |
| 3 | Write | handle, offset, bytes | count written |
| 4 | Truncate | handle, size | ignored |
| 5 | Sync | handle, SQLite sync flags | ignored |
| 6 | File size | handle | size |
| 7 | Delete | path, sync-directory flag | ignored |
| 8 | Access | path, SQLite access flags | boolean/integer |
| 9 | Entropy | `None`, byte count | random bytes |
| 10 | Time | unused | `(Unix days, seconds within day)` |

Use a single serialized filesystem owner and retain the callback while the
database is open. SQLite's journal ordering and sync requests must be honored.
The standalone [Python shadow VFS](../sqlite-shadow-vfs/README.md) supplies
`ShadowVFS` with a bounded RAM image and LittleFS journal. It works with either
native build variant and is also a dependency of the Python PowerSync SDK.
Another project can supply its own callback.
The portable C `sqlite-littlefs-vfs` library is a separate alternative.

## Build from source

Prerequisites: Python 3.11+, Make, Arm GNU Toolchain **13.2.Rel1 / GCC 13.2.1**
(`arm-none-eabi-gcc` on `PATH`), and the packages in `requirements-build.txt`.
The PowerSync variant also needs rustup, Rust **1.96.0**, and its
`thumbv8m.main-none-eabi` standard library target:

```sh
cd native-sqlite
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-build.txt
rustup toolchain install 1.96.0
rustup target add --toolchain 1.96.0 thumbv8m.main-none-eabi
python tools/fetch.py
python tools/build.py mpy --variant all
```

Rust is unnecessary for `--variant sqlite`. That variant needs only the
`sqlite` and `micropython` dependencies: `python tools/fetch.py sqlite micropython`.

The fetcher downloads immutable archives into `cache/`, checks archive and
individual-file hashes, and extracts allowlisted sources into `vendor/`.
`dependencies.json` records upstream versions, commits, URLs and checksums.
`sqlite-adaptations.json` specifies every checked SQLite transformation required
by MicroPython's dynamic loader; original upstream sources remain unchanged.

Use `python tools/fetch.py --check` to verify prepared sources without network
access. `--cache /path/to/archives` accepts an existing archive cache. After Rust
dependencies have been cached, `tools/build.py ... --offline` also forbids Cargo
downloads. The upstream Cargo.lock is used without dependency updates.
PowerSync reports `0.5.4/cratesio` for the archive build because it has no Git
checkout; the actual upstream commit is recorded in the artifact metadata.

Outputs live in `dist/sqlite/` and `dist/powersync/`, each containing the module
and a `module.json` recording its digest, ABI, source hashes and build variant.
Builds never copy to hardware or update the checked-in `prebuilt/` artifacts.
The app packager consumes `prebuilt/powersync/` and verifies the SDK runtime pin.

The target is `.mpy` ABI **7942**, object representation C, single-precision
MicroPython floats and the **softfp calling convention**. MicroPython's
`armv7emsp` module header alone does not identify the floating-point ABI. The
Rust archive uses Cortex-M33 PIC code and the soft `thumbv8m.main-none-eabi`
target. Do not substitute an `eabihf` archive or an arbitrary firmware build.

## Host qualification

On macOS or Linux with a C compiler, build and test either host variant:

```sh
python tools/fetch.py sqlite powersync
python tools/build.py host --variant all
python tests/qualify_host.py --variant sqlite
python tests/qualify_host.py --variant powersync
python -m unittest discover -s tests -v
```

The PowerSync qualifier checks checkpoint publication, local CRUD, rollback and
close/reopen against the real extension. Each qualifier runs in a separate
process with a timeout. The Dots & Boxes tests use this component's PowerSync
host library by default, including their actual Python VFS fault tests.

Host tests do not execute the ARM `.mpy`. A new native build needs a Tufty smoke
test before being described as hardware-qualified. The upstream no-std Rust
panic handler loops; watchdogs and bounded workloads remain necessary. Neither
these tests nor the existing Python VFS tests establish physical power-cut or
flash-wear guarantees.
