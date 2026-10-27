#include "py/dynruntime.h"
#include "sqlite3.h"
#include <string.h>
#include "psbadge.h"

/* Module BSS is a GC-owned allocation. Keep state non-static for GOT references. */
/* The loader's GC block is not guaranteed to be 64-byte aligned. Align the
 * arena at runtime so every memsys5 block satisfies the Rust task layouts. */
#define SDK_ARENA_BYTES (768*1024)
unsigned char badge_sqlite_arena_storage[SDK_ARENA_BYTES+63];
void *badge_sqlite_arena;
void *badge_sdk_context;
void *badge_sdk_runtime;
volatile unsigned badge_sdk_panicked;
volatile unsigned badge_sdk_stage;
unsigned badge_native_busy;
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
    if(badge_wsd_count==64 || badge_wsd_used+n>sizeof badge_wsd_bytes)psb_panic_abort();
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
static void enter_native(void){if(badge_native_busy)mp_raise_msg(&mp_type_RuntimeError,"Native SDK reentry is not allowed");badge_native_busy=1;}
void *psb_runtime_context(void) { return badge_sdk_runtime; }
void psb_set_runtime_context(void *context) { badge_sdk_runtime=context; }
void psb_trace(uint32_t stage) { badge_sdk_stage=stage; }
void psb_panic_report(const unsigned char *file,size_t length,uint32_t line) {
    /* A fatal Rust panic cannot unwind through MicroPython. Persist a bounded
     * diagnostic via a protected C callback before the watchdog recovers. */
    nlr_buf_t boundary;
    if(mp_fun_table.nlr_push(&boundary))return;
    mp_obj_t builtins=mp_import_name(MP_QSTR_builtins,mp_const_none,MP_OBJ_NEW_SMALL_INT(0));
    mp_obj_t open_fn=mp_load_attr(builtins,MP_QSTR_open);
    mp_obj_t open_args[2]={mp_obj_new_str("/state/dots_boxes_native_panic.txt",34),mp_obj_new_str("w",1)};
    mp_obj_t handle=mp_call_function_n_kw(open_fn,2,0,open_args);
    mp_obj_t write_fn=mp_load_attr(handle,MP_QSTR_write);
    mp_obj_t arg=mp_obj_new_str((const char*)file,length>512?512:length);
    mp_call_function_n_kw(write_fn,1,0,&arg);
    mp_obj_t values[2]={mp_obj_new_int_from_uint(line),mp_obj_new_int_from_uint(badge_sdk_stage)};
    arg=mp_obj_new_str("\nline,stage=",12);mp_call_function_n_kw(write_fn,1,0,&arg);
    mp_obj_t repr=mp_obj_new_tuple(2,values);
    mp_obj_t str_fn=mp_load_attr(builtins,MP_QSTR_str);
    arg=mp_call_function_n_kw(str_fn,1,0,&repr);mp_call_function_n_kw(write_fn,1,0,&arg);
    mp_obj_t close_fn=mp_load_attr(handle,MP_QSTR_close);mp_call_function_n_kw(close_fn,0,0,NULL);
    mp_fun_table.nlr_pop();
}
void psb_panic_abort(void) {
    badge_sdk_panicked=1;
    /* Never longjmp over Rust frames. The app watchdog provides recovery. */
    for (;;) { __asm volatile ("nop"); }
}
static mp_obj_t sql_open(mp_obj_t path){
    if(badge_db)mp_raise_ValueError("database already open");
    if(!badge_sqlite_ready){badge_sqlite_arena=(void*)(((uintptr_t)badge_sqlite_arena_storage+63u)&~(uintptr_t)63u);check(sqlite3_config(SQLITE_CONFIG_HEAP,badge_sqlite_arena,SDK_ARENA_BYTES,64));check(sqlite3_initialize());badge_sqlite_ready=1;}
    const char *s=mp_obj_str_get_str(path);
    if(strcmp(s,":memory:") && !badge_io)mp_raise_ValueError("configure VFS callback first");
    int opened=sqlite3_open(s,&badge_db);
    if(opened!=SQLITE_OK){if(badge_db){sqlite3_close(badge_db);badge_db=NULL;}check(opened);}
#ifdef NATIVE_SQLITE_POWERSYNC
    char *error=NULL;
    enter_native();
    int rc=sqlite3_powersync_init(badge_db,&error,NULL);
    badge_native_busy=0;
    if(error){mp_printf(&mp_plat_print,"PowerSync init: %s\n",error);sqlite3_free(error);}
    if(rc!=SQLITE_OK){sqlite3_close(badge_db);badge_db=NULL;check(rc);}
#endif
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(sql_open_obj,sql_open);
static mp_obj_t sql_configure(mp_obj_t callback){if(badge_db)mp_raise_ValueError("close database before configure");badge_io=callback;return mp_const_none;}
static MP_DEFINE_CONST_FUN_OBJ_1(sql_configure_obj,sql_configure);
static int sdk_finish(void) {
    if(!badge_sdk_context)return 1;
    if(!psb_stop(badge_sdk_context))return 0;
    if(!psb_destroy(badge_sdk_context))return 0;
    badge_sdk_context=NULL;
    return 1;
}
static mp_obj_t sql_close(void){
    enter_native();
    for(unsigned i=0;badge_sdk_context&&i<32;i++)if(sdk_finish())break;
    badge_native_busy=0;
    if(badge_sdk_context)mp_raise_msg(&mp_type_RuntimeError,"SDK cleanup pending; database retained");
    if(badge_db){check(sqlite3_close(badge_db));badge_db=NULL;}
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(sql_close_obj,sql_close);
static mp_obj_t sql_exec(mp_obj_t sql){
    sqlite3_stmt *volatile active=NULL;
    void *volatile lease=NULL;
    unsigned volatile entered=0;
    nlr_buf_t boundary;
    if(mp_fun_table.nlr_push(&boundary)){
        if(active)sqlite3_finalize(active);
        if(lease)psb_sql_leave((void*)lease);
        if(entered)badge_native_busy=0;
        nlr_raise(MP_OBJ_FROM_PTR(boundary.ret_val));
    }
    if(!badge_db)mp_raise_ValueError("database not open");
    enter_native();entered=1;
    if(badge_sdk_context){lease=psb_sql_enter(badge_sdk_context);if(!lease)mp_raise_msg(&mp_type_RuntimeError,"SDK database busy");}
    const char *p=mp_obj_str_get_str(sql); mp_obj_t rows=mp_obj_new_list(0,NULL);
    while(*p){sqlite3_stmt *stmt=NULL;const char *tail=NULL;int rc=sqlite3_prepare_v2(badge_db,p,-1,&stmt,&tail);active=stmt;check(rc);p=tail;if(!stmt)continue;
        unsigned row_count=0;
        while((rc=sqlite3_step(stmt))==SQLITE_ROW){
            if(++row_count>32)mp_raise_ValueError("query row limit");
            int n=sqlite3_column_count(stmt);mp_obj_t row=mp_obj_new_list(0,NULL);
            for(int i=0;i<n;i++){const unsigned char *s=sqlite3_column_text(stmt,i);mp_obj_list_append(row,s?mp_obj_new_str((const char*)s,sqlite3_column_bytes(stmt,i)):mp_const_none);}mp_obj_list_append(rows,row);
        }
        int done=sqlite3_finalize(stmt);active=NULL;if(rc!=SQLITE_DONE)check(rc);check(done);
    }if(lease)psb_sql_leave((void*)lease);badge_native_busy=0;mp_fun_table.nlr_pop();return rows;
}
static MP_DEFINE_CONST_FUN_OBJ_1(sql_exec_obj,sql_exec);
static mp_obj_t sql_stats(void){mp_obj_t items[8]={mp_obj_new_int(sqlite3_memory_used()),mp_obj_new_int(sqlite3_memory_highwater(0)),mp_obj_new_int(badge_wsd_used),mp_obj_new_int(badge_wsd_count),mp_obj_new_int_from_uint((uintptr_t)sql_stats),mp_obj_new_int_from_uint((uintptr_t)badge_sqlite_arena),mp_obj_new_int_from_uint((uintptr_t)badge_wsd_bytes),mp_obj_new_str(sqlite3_libversion(),strlen(sqlite3_libversion()))};return mp_obj_new_tuple(8,items);}
static MP_DEFINE_CONST_FUN_OBJ_0(sql_stats_obj,sql_stats);
static mp_obj_t sdk_start(mp_obj_t schema,mp_obj_t endpoint,mp_obj_t room) {
    if(!badge_db)mp_raise_ValueError("database not open");
    if(badge_sdk_context)mp_raise_ValueError("SDK already active");
    size_t a,b,c;
    const char *sa=mp_obj_str_get_data(schema,&a),*sb=mp_obj_str_get_data(endpoint,&b),*sc=mp_obj_str_get_data(room,&c);
    if(a>16384||b>1024||c>128)mp_raise_ValueError("SDK start input exceeds limit");
    enter_native();
    badge_sdk_context=psb_start(badge_db,(const unsigned char*)sa,a,(const unsigned char*)sb,b,(const unsigned char*)sc,c);
    badge_native_busy=0;
    if(!badge_sdk_context)mp_raise_msg(&mp_type_RuntimeError,"Native SDK start failed");
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_3(sdk_start_obj,sdk_start);
static mp_obj_t sdk_poll(mp_obj_t elapsed,mp_obj_t budget) {
    if(!badge_sdk_context)mp_raise_ValueError("SDK not active");
    int ms=mp_obj_get_int(elapsed),polls=mp_obj_get_int(budget);
    if(ms<0||ms>60000||polls<1||polls>32)mp_raise_ValueError("Invalid SDK poll budget");
    PsbBuffer volatile buffer={0};
    nlr_buf_t boundary;
    if(mp_fun_table.nlr_push(&boundary)){
        PsbBuffer released=buffer;psb_buffer_free(released);
        nlr_raise(MP_OBJ_FROM_PTR(boundary.ret_val));
    }
    enter_native();buffer=psb_poll(badge_sdk_context,(uint32_t)ms,(uint32_t)polls);badge_native_busy=0;
    if(buffer.len>65536)mp_raise_ValueError("SDK events exceed limit");
    mp_obj_t result=mp_obj_new_str((const char*)buffer.ptr,buffer.len);
    PsbBuffer released=buffer;psb_buffer_free(released);
    mp_fun_table.nlr_pop();return result;
}
static MP_DEFINE_CONST_FUN_OBJ_2(sdk_poll_obj,sdk_poll);
static mp_obj_t sdk_reply(mp_obj_t command) {
    if(!badge_sdk_context)mp_raise_ValueError("SDK not active");
    size_t length;const char *json=mp_obj_str_get_data(command,&length);
    if(length>32768)mp_raise_ValueError("SDK reply exceeds limit");
    enter_native();int rc=psb_command(badge_sdk_context,(const unsigned char*)json,length);badge_native_busy=0;
    if(rc)mp_raise_ValueError("Invalid SDK reply");
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(sdk_reply_obj,sdk_reply);
static mp_obj_t sdk_stop(void) {enter_native();int done=sdk_finish();badge_native_busy=0;return done?mp_const_true:mp_const_false;}
static MP_DEFINE_CONST_FUN_OBJ_0(sdk_stop_obj,sdk_stop);
mp_obj_t mpy_init(mp_obj_fun_bc_t*self,size_t n_args,size_t n_kw,mp_obj_t*args){
    MP_DYNRUNTIME_INIT_ENTRY
    mp_store_global(MP_QSTR_configure,MP_OBJ_FROM_PTR(&sql_configure_obj));
    mp_store_global(MP_QSTR_open,MP_OBJ_FROM_PTR(&sql_open_obj));mp_store_global(MP_QSTR_close,MP_OBJ_FROM_PTR(&sql_close_obj));mp_store_global(MP_QSTR_exec,MP_OBJ_FROM_PTR(&sql_exec_obj));mp_store_global(MP_QSTR_stats,MP_OBJ_FROM_PTR(&sql_stats_obj));
    mp_store_global(MP_QSTR_sdk_start,MP_OBJ_FROM_PTR(&sdk_start_obj));
    mp_store_global(MP_QSTR_sdk_poll,MP_OBJ_FROM_PTR(&sdk_poll_obj));
    mp_store_global(MP_QSTR_sdk_reply,MP_OBJ_FROM_PTR(&sdk_reply_obj));
    mp_store_global(MP_QSTR_sdk_stop,MP_OBJ_FROM_PTR(&sdk_stop_obj));
    MP_DYNRUNTIME_INIT_EXIT
}
