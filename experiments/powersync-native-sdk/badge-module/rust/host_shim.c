#include <stdlib.h>
#include "sqlite3.h"
static unsigned char heap[768*1024] __attribute__((aligned(64)));
static void *runtime;
void *psb_runtime_context(void) { return runtime; }
void psb_set_runtime_context(void *p) { runtime=p; }
void psb_panic_abort(void) { abort(); }
void psb_trace(unsigned stage) { (void)stage; }
void psb_panic_report(const unsigned char *file, size_t length, unsigned line) { (void)file; (void)length; (void)line; }
void rust_eh_personality(void) { abort(); }
extern int sqlite3_powersync_init(sqlite3*, char**, const void*);
int bridge_host_open(sqlite3 **db) {
    int rc=sqlite3_config(SQLITE_CONFIG_HEAP,heap,sizeof(heap),64);
    if(rc)return rc;
    rc=sqlite3_open(":memory:",db);
    if(rc)return rc;
    char *error=NULL;
    rc=sqlite3_powersync_init(*db,&error,NULL);
    sqlite3_free(error);
    return rc;
}
int bridge_host_exec(sqlite3 *db,const char *sql) { return sqlite3_exec(db,sql,NULL,NULL,NULL); }
int bridge_host_scalar(sqlite3 *db,const char *sql) {
    sqlite3_stmt *stmt=NULL;
    int rc=sqlite3_prepare_v2(db,sql,-1,&stmt,NULL);
    if(rc)return -1000-rc;
    rc=sqlite3_step(stmt);
    int value=rc==SQLITE_ROW?sqlite3_column_int(stmt,0):-2000-rc;
    sqlite3_finalize(stmt);
    return value;
}
