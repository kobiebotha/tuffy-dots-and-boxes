/* Host POSIX VFS with the same constrained SQL and allocator profile. */
#include "config.h"
#undef SQLITE_OS_OTHER
#define SQLITE_OS_OTHER 0
#define SQLITE_OS_UNIX 1
