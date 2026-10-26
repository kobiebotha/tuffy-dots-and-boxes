/* Test adapter only: pinned host SQLite -> actual Python BadgeVFS op0..10.
 * There is deliberately no storage implementation here. All file bytes,
 * handles, entropy and clock values come from the configured Python callback.
 */
#include "sqlite3.h"
#include <stdint.h>
#include <string.h>

typedef int (*PythonIO)(int op, const char *path, int handle, sqlite3_int64 arg,
                        void *buffer, int length, sqlite3_int64 *out1, sqlite3_int64 *out2);
typedef struct { sqlite3_file base; int handle; int lock; int main_db; } PythonFile;
static PythonIO python_io;
static sqlite3_vfs python_vfs, *previous_vfs;
static int open_files, main_open;

static int invoke(int op, const char *path, int handle, sqlite3_int64 arg,
                  void *buffer, int length, sqlite3_int64 *a, sqlite3_int64 *b) {
    if (!python_io) return SQLITE_CANTOPEN;
    sqlite3_int64 ignored1 = 0, ignored2 = 0;
    return python_io(op, path, handle, arg, buffer, length,
                     a ? a : &ignored1, b ? b : &ignored2);
}
static int file_close(sqlite3_file *file) {
    PythonFile *f = (PythonFile *)file;
    int rc = invoke(1, 0, f->handle, 0, 0, 0, 0, 0);
    file->pMethods = 0;
    --open_files;
    if (f->main_db) main_open = 0;
    return rc;
}
static int file_read(sqlite3_file *file, void *buffer, int size, sqlite3_int64 offset) {
    if (offset < 0 || offset > 0x7fffffff) return SQLITE_IOERR_READ;
    sqlite3_int64 read = 0;
    int rc = invoke(2, 0, ((PythonFile *)file)->handle, offset, buffer, size, &read, 0);
    if (rc) return rc;
    if (read < 0 || read > size) return SQLITE_IOERR_READ;
    if (read < size) {
        memset((char *)buffer + read, 0, size - read);
        return SQLITE_IOERR_SHORT_READ;
    }
    return SQLITE_OK;
}
static int file_write(sqlite3_file *file, const void *buffer, int size, sqlite3_int64 offset) {
    if (offset < 0 || offset > 0x7fffffff) return SQLITE_IOERR_WRITE;
    sqlite3_int64 written = 0;
    int rc = invoke(3, 0, ((PythonFile *)file)->handle, offset, (void *)buffer, size, &written, 0);
    return rc ? rc : (written == size ? SQLITE_OK : SQLITE_IOERR_WRITE);
}
static int file_truncate(sqlite3_file *file, sqlite3_int64 size) {
    if (size < 0 || size > 0x7fffffff) return SQLITE_IOERR_TRUNCATE;
    return invoke(4, 0, ((PythonFile *)file)->handle, size, 0, 0, 0, 0);
}
static int file_sync(sqlite3_file *file, int flags) {
    return invoke(5, 0, ((PythonFile *)file)->handle, flags, 0, 0, 0, 0);
}
static int file_size(sqlite3_file *file, sqlite3_int64 *size) {
    return invoke(6, 0, ((PythonFile *)file)->handle, 0, 0, 0, size, 0);
}
static int file_lock(sqlite3_file *file, int lock) { ((PythonFile *)file)->lock = lock; return SQLITE_OK; }
static int file_unlock(sqlite3_file *file, int lock) { ((PythonFile *)file)->lock = lock; return SQLITE_OK; }
static int file_reserved(sqlite3_file *file, int *result) { *result = 0; return SQLITE_OK; }
static int file_control(sqlite3_file *file, int op, void *arg) { return SQLITE_NOTFOUND; }
static int file_sector(sqlite3_file *file) { return 512; }
static int file_caps(sqlite3_file *file) { return 0; }
static const sqlite3_io_methods methods = {
    .iVersion = 1, .xClose = file_close, .xRead = file_read, .xWrite = file_write,
    .xTruncate = file_truncate, .xSync = file_sync, .xFileSize = file_size,
    .xLock = file_lock, .xUnlock = file_unlock, .xCheckReservedLock = file_reserved,
    .xFileControl = file_control, .xSectorSize = file_sector, .xDeviceCharacteristics = file_caps
};
static int vfs_open(sqlite3_vfs *vfs, const char *path, sqlite3_file *file, int flags, int *out) {
    file->pMethods = 0;
    if (!path || ((flags & SQLITE_OPEN_MAIN_DB) && main_open)) return SQLITE_CANTOPEN;
    sqlite3_int64 handle = 0;
    int rc = invoke(0, path, 0, flags, 0, 0, &handle, 0);
    if (rc) return rc;
    if (handle < 1 || handle > 0x7fffffff) return SQLITE_CANTOPEN;
    PythonFile *f = (PythonFile *)file;
    f->handle = (int)handle; f->lock = SQLITE_LOCK_NONE;
    f->main_db = !!(flags & SQLITE_OPEN_MAIN_DB);
    if (f->main_db) main_open = 1;
    ++open_files;
    f->base.pMethods = &methods;
    if (out) *out = flags;
    return SQLITE_OK;
}
static int vfs_delete(sqlite3_vfs *vfs, const char *path, int sync_dir) {
    return invoke(7, path, 0, sync_dir, 0, 0, 0, 0);
}
static int vfs_access(sqlite3_vfs *vfs, const char *path, int flags, int *out) {
    sqlite3_int64 value = 0;
    int rc = invoke(8, path, 0, flags, 0, 0, &value, 0);
    if (!rc) *out = !!value;
    return rc;
}
static int vfs_full_path(sqlite3_vfs *vfs, const char *path, int size, char *out) {
    size_t length = strlen(path);
    if (length >= (size_t)size) return SQLITE_CANTOPEN;
    memcpy(out, path, length + 1); return SQLITE_OK;
}
static int vfs_random(sqlite3_vfs *vfs, int size, char *buffer) {
    sqlite3_int64 count = 0;
    int rc = invoke(9, 0, 0, size, buffer, size, &count, 0);
    return rc ? 0 : (int)count;
}
static int vfs_sleep(sqlite3_vfs *vfs, int microseconds) {
    return previous_vfs && previous_vfs->xSleep ? previous_vfs->xSleep(previous_vfs, microseconds) : 0;
}
static int vfs_time64(sqlite3_vfs *vfs, sqlite3_int64 *milliseconds) {
    sqlite3_int64 days = 0, seconds = 0;
    int rc = invoke(10, 0, 0, 0, 0, 0, &days, &seconds);
    if (rc) return rc;
    if (seconds < 0 || seconds >= 86400) return SQLITE_IOERR;
    /* BadgeVFS returns Unix days/seconds; SQLite requires Julian milliseconds. */
    *milliseconds = INT64_C(210866760000000) + days * INT64_C(86400000) + seconds * 1000;
    return SQLITE_OK;
}
static int vfs_time(sqlite3_vfs *vfs, double *days) {
    sqlite3_int64 milliseconds = 0;
    int rc = vfs_time64(vfs, &milliseconds);
    if (!rc) *days = milliseconds / 86400000.0;
    return rc;
}
int python_vfs_install(PythonIO callback) {
    if (python_io || open_files) return SQLITE_BUSY;
    previous_vfs = sqlite3_vfs_find(0);
    python_io = callback;
    memset(&python_vfs, 0, sizeof(python_vfs));
    python_vfs.iVersion = 2; python_vfs.szOsFile = sizeof(PythonFile);
    python_vfs.mxPathname = 512; python_vfs.zName = "sqlite-shadow-python-vfs";
    python_vfs.xOpen = vfs_open; python_vfs.xDelete = vfs_delete;
    python_vfs.xAccess = vfs_access; python_vfs.xFullPathname = vfs_full_path;
    python_vfs.xRandomness = vfs_random; python_vfs.xSleep = vfs_sleep;
    python_vfs.xCurrentTime = vfs_time; python_vfs.xCurrentTimeInt64 = vfs_time64;
    int rc = sqlite3_vfs_register(&python_vfs, 1);
    if (rc) { python_io = 0; previous_vfs = 0; }
    return rc;
}
int python_vfs_uninstall(void) {
    if (open_files) return SQLITE_BUSY;
    if (!python_io) return SQLITE_OK;
    int rc = previous_vfs ? sqlite3_vfs_register(previous_vfs, 1) : SQLITE_OK;
    if (rc) return rc;
    rc = sqlite3_vfs_unregister(&python_vfs);
    if (!rc) { python_io = 0; previous_vfs = 0; }
    return rc;
}
const char *python_vfs_default_name(void) {
    sqlite3_vfs *vfs = sqlite3_vfs_find(0);
    return vfs ? vfs->zName : "";
}
/* Probe the actual registered randomness callback even if SQLite's global
 * PRNG was seeded by an earlier POSIX test and no longer asks its VFS. */
int python_vfs_probe_random(int size, void *buffer) {
    return vfs_random(&python_vfs, size, (char *)buffer);
}
