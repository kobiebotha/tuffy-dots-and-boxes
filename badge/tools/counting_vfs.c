/* Host benchmark only. Wrap the pinned core's POSIX VFS and count file I/O.
 * Sector size/device caps match the badge VFS, preserving DELETE/FULL work.
 * Compile/link instructions are in benchmark_cache.py. Never a device module.
 */
#include "sqlite3.h"
#include <stdint.h>
#include <string.h>

typedef struct { sqlite3_file base; sqlite3_file *real; int kind; } CountFile;
enum { OP_OPEN, OP_READ, OP_WRITE, OP_SYNC, OP_TRUNCATE, OP_CLOSE,
       OP_SIZE, OP_LOCK, OP_UNLOCK, OP_ACCESS, OP_DELETE, READ_BYTES,
       WRITE_BYTES, OP_DELETE_SYNC, METRICS };
static uint64_t counters[3][METRICS]; /* all, main database, journal/other */
static sqlite3_vfs counting, *underlying;
static const int file_offset = (sizeof(CountFile) + 15) & ~15;
static void inc(int kind, int op, uint64_t amount) {
    counters[0][op] += amount;
    counters[kind][op] += amount;
}
static int close_file(sqlite3_file *f) {
    CountFile *p = (CountFile *)f; inc(p->kind, OP_CLOSE, 1);
    int rc = p->real->pMethods->xClose(p->real); f->pMethods = 0; return rc;
}
static int read_file(sqlite3_file *f, void *buf, int n, sqlite3_int64 at) {
    CountFile *p = (CountFile *)f; inc(p->kind, OP_READ, 1); inc(p->kind, READ_BYTES, n);
    return p->real->pMethods->xRead(p->real, buf, n, at);
}
static int write_file(sqlite3_file *f, const void *buf, int n, sqlite3_int64 at) {
    CountFile *p = (CountFile *)f; inc(p->kind, OP_WRITE, 1); inc(p->kind, WRITE_BYTES, n);
    return p->real->pMethods->xWrite(p->real, buf, n, at);
}
static int truncate_file(sqlite3_file *f, sqlite3_int64 n) {
    CountFile *p = (CountFile *)f; inc(p->kind, OP_TRUNCATE, 1);
    return p->real->pMethods->xTruncate(p->real, n);
}
static int sync_file(sqlite3_file *f, int flags) {
    CountFile *p = (CountFile *)f; inc(p->kind, OP_SYNC, 1);
    return p->real->pMethods->xSync(p->real, flags);
}
static int size_file(sqlite3_file *f, sqlite3_int64 *n) {
    CountFile *p = (CountFile *)f; inc(p->kind, OP_SIZE, 1);
    return p->real->pMethods->xFileSize(p->real, n);
}
static int lock_file(sqlite3_file *f, int level) {
    CountFile *p = (CountFile *)f; inc(p->kind, OP_LOCK, 1);
    return p->real->pMethods->xLock(p->real, level);
}
static int unlock_file(sqlite3_file *f, int level) {
    CountFile *p = (CountFile *)f; inc(p->kind, OP_UNLOCK, 1);
    return p->real->pMethods->xUnlock(p->real, level);
}
static int reserved_file(sqlite3_file *f, int *result) {
    CountFile *p = (CountFile *)f;
    return p->real->pMethods->xCheckReservedLock(p->real, result);
}
static int control_file(sqlite3_file *f, int op, void *arg) {
    /* Match the badge VFS: no POSIX batch-atomic or size-hint shortcuts. */
    return SQLITE_NOTFOUND;
}
static int sector_file(sqlite3_file *f) { return 512; }
static int caps_file(sqlite3_file *f) { return 0; }
static const sqlite3_io_methods methods = {
    1, close_file, read_file, write_file, truncate_file, sync_file, size_file,
    lock_file, unlock_file, reserved_file, control_file, sector_file, caps_file
};
static int open_file(sqlite3_vfs *vfs, const char *name, sqlite3_file *file, int flags, int *out) {
    CountFile *p = (CountFile *)file;
    p->real = (sqlite3_file *)((char *)file + file_offset);
    p->kind = (flags & SQLITE_OPEN_MAIN_DB) ? 1 : 2;
    inc(p->kind, OP_OPEN, 1);
    int rc = underlying->xOpen(underlying, name, p->real, flags, out);
    if (rc == SQLITE_OK) file->pMethods = &methods;
    return rc;
}
static int delete_file(sqlite3_vfs *vfs, const char *name, int sync) {
    inc(2, OP_DELETE, 1); if (sync) inc(2, OP_DELETE_SYNC, 1);
    return underlying->xDelete(underlying, name, sync);
}
static int access_file(sqlite3_vfs *vfs, const char *name, int flags, int *result) {
    inc(2, OP_ACCESS, 1); return underlying->xAccess(underlying, name, flags, result);
}
int counts_install(void) {
    if (underlying) return SQLITE_OK;
    underlying = sqlite3_vfs_find(0);
    counting = *underlying;
    counting.zName = "dots-counting-vfs";
    counting.szOsFile = file_offset + underlying->szOsFile;
    counting.xOpen = open_file; counting.xDelete = delete_file; counting.xAccess = access_file;
    return sqlite3_vfs_register(&counting, 1);
}
void counts_reset(void) { memset(counters, 0, sizeof(counters)); }
uint64_t counts_get(int kind, int metric) { return counters[kind][metric]; }
