#ifndef NATIVE_SQLITE_CONFIG_H
#define NATIVE_SQLITE_CONFIG_H

/* Bounded feature/allocator profile shared by native and host builds. */
#define SQLITE_OS_OTHER 1
#define SQLITE_THREADSAFE 0
#define SQLITE_TEMP_STORE 3
#define SQLITE_ENABLE_MEMSYS5 1
/* Even a missed SQLITE_CONFIG_HEAP must never fall back to libc malloc. */
#define SQLITE_ZERO_MALLOC 1
#define SQLITE_OMIT_LOAD_EXTENSION 1
#define SQLITE_OMIT_SHARED_CACHE 1
#define SQLITE_OMIT_DEPRECATED 1
#define SQLITE_OMIT_LOCALTIME 1
/* UTF-8 SQL, memory-only temporary storage and rollback journals. */
#define SQLITE_OMIT_UTF16 1
#define SQLITE_OMIT_WAL 1
#define SQLITE_OMIT_GET_TABLE 1
#define SQLITE_OMIT_TRACE 1
#define SQLITE_OMIT_DESERIALIZE 1
#define SQLITE_OMIT_EXPLAIN 1
#define SQLITE_MAX_MMAP_SIZE 0
#define SQLITE_DEFAULT_MEMSTATUS 1
#define SQLITE_DEFAULT_PAGE_SIZE 1024
#define SQLITE_DEFAULT_CACHE_SIZE -16
#define SQLITE_DEFAULT_LOOKASIDE 0,0
#define SQLITE_DEFAULT_WORKER_THREADS 0
#define SQLITE_MAX_WORKER_THREADS 0
#define SQLITE_DQS 0
#define SQLITE_MAX_LENGTH 65536
#define SQLITE_MAX_SQL_LENGTH 16384
#define SQLITE_MAX_COLUMN 64
#define SQLITE_MAX_VARIABLE_NUMBER 32
#define SQLITE_MAX_EXPR_DEPTH 50
#define SQLITE_JSON_MAX_DEPTH 20
#define SQLITE_PRINTF_PRECISION_LIMIT 4096

/* JSON, virtual tables, triggers and static extension registration remain. */

#endif
