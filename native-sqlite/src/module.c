#include "py/dynruntime.h"
#include "sqlite3.h"
#include <string.h>

/* Module BSS is a GC-owned allocation. Keep state non-static for GOT references. */
unsigned char badge_sqlite_arena[256*1024] __attribute__((aligned(8)));
unsigned char badge_wsd_bytes[16384] __attribute__((aligned(8)));
struct badge_wsd_item {void *key; void *value;};
struct badge_wsd_item badge_wsd_items[64];
unsigned badge_wsd_count, badge_wsd_used;
sqlite3 *badge_db;
int badge_sqlite_ready;
sqlite3_vfs badge_vfs;
extern mp_obj_t badge_io;
void badge_vfs_install(void);
#ifdef NATIVE_SQLITE_POWERSYNC
int sqlite3_powersync_init(sqlite3*,char**,const void*);
#endif

int sqlite3_wsd_init(int n, int j) {return (n<=sizeof badge_wsd_bytes && j<=64)?SQLITE_OK:SQLITE_NOMEM;}
void *sqlite3_wsd_find(void *key,int len) {
    for(unsigned i=0;i<badge_wsd_count;i++)if(badge_wsd_items[i].key==key)return badge_wsd_items[i].value;
    unsigned n=((unsigned)len+7)&~7u;
    if(badge_wsd_count==64 || badge_wsd_used+n>sizeof badge_wsd_bytes)mp_raise_msg(&mp_type_RuntimeError,"SQLite WSD limit");
    void *p=badge_wsd_bytes+badge_wsd_used; memcpy(p,key,len); badge_wsd_used+=n;
    badge_wsd_items[badge_wsd_count++]=(struct badge_wsd_item){key,p}; return p;
}
int badge_vfs_random(sqlite3_vfs *v, int n, char *out);
int badge_vfs_time(sqlite3_vfs *v, double *out);
static int vfs_sleep(sqlite3_vfs*v,int n){return 0;}
int sqlite3_os_init(void) {
    memset(&badge_vfs,0,sizeof badge_vfs);badge_vfs.iVersion=1;badge_vfs.mxPathname=256;badge_vfs.zName="badgepy";
    badge_vfs.xRandomness=badge_vfs_random;badge_vfs.xSleep=vfs_sleep;badge_vfs.xCurrentTime=badge_vfs_time;
    badge_vfs_install();return sqlite3_vfs_register(&badge_vfs,1);
}
int sqlite3_os_end(void){return SQLITE_OK;}
static void check(int rc){if(rc!=SQLITE_OK){mp_printf(&mp_plat_print,"SQLite rc=%d msg=%s\n",rc,badge_db?sqlite3_errmsg(badge_db):"no db");mp_raise_msg(&mp_type_RuntimeError,"SQLite operation failed");}}
static mp_obj_t sql_open(mp_obj_t path){
    if(badge_db)mp_raise_ValueError("database already open");
    if(!badge_sqlite_ready){check(sqlite3_config(SQLITE_CONFIG_HEAP,badge_sqlite_arena,sizeof badge_sqlite_arena,64));check(sqlite3_initialize());badge_sqlite_ready=1;}
    const char *s=mp_obj_str_get_str(path);
    if(strcmp(s,":memory:") && !badge_io)mp_raise_ValueError("configure VFS callback first");
    int opened=sqlite3_open(s,&badge_db);
    if(opened!=SQLITE_OK){if(badge_db){sqlite3_close(badge_db);badge_db=NULL;}check(opened);}
#ifdef NATIVE_SQLITE_POWERSYNC
    char *error=NULL;
    int rc=sqlite3_powersync_init(badge_db,&error,NULL);
    if(error){mp_printf(&mp_plat_print,"PowerSync init: %s\n",error);sqlite3_free(error);}
    if(rc!=SQLITE_OK){sqlite3_close(badge_db);badge_db=NULL;check(rc);}
#endif
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(sql_open_obj,sql_open);
static mp_obj_t sql_configure(mp_obj_t callback){if(badge_db)mp_raise_ValueError("close database before configure");badge_io=callback;return mp_const_none;}
static MP_DEFINE_CONST_FUN_OBJ_1(sql_configure_obj,sql_configure);
static mp_obj_t sql_close(void){if(badge_db){check(sqlite3_close(badge_db));badge_db=NULL;}return mp_const_none;}
static MP_DEFINE_CONST_FUN_OBJ_0(sql_close_obj,sql_close);
static mp_obj_t sql_exec(mp_obj_t sql){
    sqlite3_stmt *volatile active=NULL;
    nlr_buf_t boundary;
    if(mp_fun_table.nlr_push(&boundary)){
        if(active)sqlite3_finalize(active);
        nlr_raise(MP_OBJ_FROM_PTR(boundary.ret_val));
    }
    if(!badge_db)mp_raise_ValueError("database not open");
    const char *p=mp_obj_str_get_str(sql); mp_obj_t rows=mp_obj_new_list(0,NULL);
    while(*p){sqlite3_stmt *stmt=NULL;const char *tail=NULL;int rc=sqlite3_prepare_v2(badge_db,p,-1,&stmt,&tail);active=stmt;check(rc);p=tail;if(!stmt)continue;
        unsigned row_count=0;
        while((rc=sqlite3_step(stmt))==SQLITE_ROW){
            if(++row_count>32)mp_raise_ValueError("query row limit");
            int n=sqlite3_column_count(stmt);mp_obj_t row=mp_obj_new_list(0,NULL);
            for(int i=0;i<n;i++){const unsigned char *s=sqlite3_column_text(stmt,i);mp_obj_list_append(row,s?mp_obj_new_str((const char*)s,sqlite3_column_bytes(stmt,i)):mp_const_none);}mp_obj_list_append(rows,row);
        }
        int done=sqlite3_finalize(stmt);active=NULL;if(rc!=SQLITE_DONE)check(rc);check(done);
    }mp_fun_table.nlr_pop();return rows;
}
static MP_DEFINE_CONST_FUN_OBJ_1(sql_exec_obj,sql_exec);
static mp_obj_t sql_stats(void){mp_obj_t items[8]={mp_obj_new_int(sqlite3_memory_used()),mp_obj_new_int(sqlite3_memory_highwater(0)),mp_obj_new_int(badge_wsd_used),mp_obj_new_int(badge_wsd_count),mp_obj_new_int_from_uint((uintptr_t)sql_stats),mp_obj_new_int_from_uint((uintptr_t)badge_sqlite_arena),mp_obj_new_int_from_uint((uintptr_t)badge_wsd_bytes),mp_obj_new_str(sqlite3_libversion(),strlen(sqlite3_libversion()))};return mp_obj_new_tuple(8,items);}
static MP_DEFINE_CONST_FUN_OBJ_0(sql_stats_obj,sql_stats);
mp_obj_t mpy_init(mp_obj_fun_bc_t*self,size_t n_args,size_t n_kw,mp_obj_t*args){
    MP_DYNRUNTIME_INIT_ENTRY
    mp_store_global(MP_QSTR_configure,MP_OBJ_FROM_PTR(&sql_configure_obj));
    mp_store_global(MP_QSTR_open,MP_OBJ_FROM_PTR(&sql_open_obj));mp_store_global(MP_QSTR_close,MP_OBJ_FROM_PTR(&sql_close_obj));mp_store_global(MP_QSTR_exec,MP_OBJ_FROM_PTR(&sql_exec_obj));mp_store_global(MP_QSTR_stats,MP_OBJ_FROM_PTR(&sql_stats_obj));
    MP_DYNRUNTIME_INIT_EXIT
}
