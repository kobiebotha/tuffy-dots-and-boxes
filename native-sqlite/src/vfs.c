#include "py/dynruntime.h"
#include "sqlite3.h"
#include <string.h>
/* The Python bridge owns all file objects, catches exceptions, and restricts paths.
 * This deliberately supports one database connection and no external concurrent access. */
mp_obj_t badge_io;
typedef struct {sqlite3_file base;int handle;int lock;} BadgeFile;
extern sqlite3_vfs badge_vfs;
static int invoke(int op,mp_obj_t target,mp_obj_t a,mp_obj_t b,mp_obj_t*out){
    if(!badge_io)return SQLITE_CANTOPEN;
    mp_obj_t args[4]={MP_OBJ_NEW_SMALL_INT(op),target,a,b};
    mp_obj_t response=mp_call_function_n_kw(badge_io,4,0,args);size_t len;mp_obj_t *items;
    mp_obj_get_array(response,&len,&items);if(len!=2)return SQLITE_IOERR;
    if(out)*out=items[1];return mp_obj_get_int(items[0]);
}
static int fileop(sqlite3_file*f,int op,int a,mp_obj_t b,mp_obj_t*out){return invoke(op,MP_OBJ_NEW_SMALL_INT(((BadgeFile*)f)->handle),mp_obj_new_int(a),b,out);}
/* These callbacks are separate from file I/O, but preserve the same exception
 * boundary. Return no entropy / an I/O error on failure, never fixture values. */
int badge_vfs_random(sqlite3_vfs *v, int n, char *out) {
    nlr_buf_t boundary;
    if (mp_fun_table.nlr_push(&boundary)) return 0;
    mp_obj_t result;
    int rc = invoke(9, mp_const_none, mp_obj_new_int(n), mp_const_none, &result);
    if (rc) { mp_fun_table.nlr_pop(); return 0; }
    mp_buffer_info_t buffer;
    mp_get_buffer_raise(result, &buffer, MP_BUFFER_READ);
    int count = buffer.len < (size_t)n ? buffer.len : n;
    memcpy(out, buffer.buf, count);
    mp_fun_table.nlr_pop();
    return count;
}
int badge_vfs_time(sqlite3_vfs *v, double *out) {
    nlr_buf_t boundary;
    if (mp_fun_table.nlr_push(&boundary)) return SQLITE_IOERR;
    mp_obj_t result;
    int rc = invoke(10, mp_const_none, mp_const_none, mp_const_none, &result);
    if (!rc) {
        size_t count;
        mp_obj_t *parts;
        mp_obj_get_array(result, &count, &parts);
        if (count != 2) rc = SQLITE_IOERR;
        else {
            /* Split days/seconds avoids MicroPython's single-precision float
             * and a signed 32-bit seconds overflow after 2038. */
            *out = 2440587.5 + (double)mp_obj_get_int(parts[0])
                + (double)mp_obj_get_int(parts[1]) / 86400.0;
        }
    }
    mp_fun_table.nlr_pop();
    return rc;
}
static int io_close_impl(sqlite3_file*f){int rc=fileop(f,1,0,mp_const_none,NULL);f->pMethods=NULL;return rc;}
static int io_read_impl(sqlite3_file*f,void*out,int n,sqlite3_int64 offset){
    if(offset<0||offset>0x7fffffff)return SQLITE_IOERR_READ;
    mp_obj_t data;int rc=fileop(f,2,(int)offset,mp_obj_new_int(n),&data);if(rc)return rc;
    mp_buffer_info_t buf;mp_get_buffer_raise(data,&buf,MP_BUFFER_READ);
    size_t count=buf.len<(size_t)n?buf.len:(size_t)n;memcpy(out,buf.buf,count);
    if(count<(size_t)n){memset((char*)out+count,0,n-count);return SQLITE_IOERR_SHORT_READ;}return SQLITE_OK;
}
static int io_write_impl(sqlite3_file*f,const void*data,int n,sqlite3_int64 offset){
    if(offset<0||offset>0x7fffffff)return SQLITE_IOERR_WRITE;
    mp_obj_t count;int rc=fileop(f,3,(int)offset,mp_obj_new_bytes(data,n),&count);
    return rc?rc:(mp_obj_get_int(count)==n?SQLITE_OK:SQLITE_IOERR_WRITE);
}
static int io_truncate_impl(sqlite3_file*f,sqlite3_int64 n){return n<0||n>0x7fffffff?SQLITE_IOERR_TRUNCATE:fileop(f,4,(int)n,mp_const_none,NULL);}
static int io_sync_impl(sqlite3_file*f,int flags){return fileop(f,5,flags,mp_const_none,NULL);}
static int io_size_impl(sqlite3_file*f,sqlite3_int64*out){mp_obj_t n;int rc=fileop(f,6,0,mp_const_none,&n);if(!rc)*out=mp_obj_get_int(n);return rc;}
static int io_lock(sqlite3_file*f,int level){((BadgeFile*)f)->lock=level;return SQLITE_OK;}
static int io_unlock(sqlite3_file*f,int level){((BadgeFile*)f)->lock=level;return SQLITE_OK;}
static int io_reserved(sqlite3_file*f,int*out){*out=0;return SQLITE_OK;}
static int io_control(sqlite3_file*f,int op,void*arg){return SQLITE_NOTFOUND;}
static int io_sector(sqlite3_file*f){return 512;}
static int io_caps(sqlite3_file*f){return 0;}
/* Catch every MicroPython allocation, callback and result-conversion exception
 * inside the VFS boundary. Never jump across active SQLite/Rust frames. The
 * matched dynamic table exports nlr_push/nlr_pop, so no firmware symbol is used. */
#define GUARDED(name, signature, arguments) \
    static int name signature { \
        nlr_buf_t boundary; \
        if (mp_fun_table.nlr_push(&boundary)) return SQLITE_IOERR; \
        int rc = name##_impl arguments; \
        mp_fun_table.nlr_pop(); \
        return rc; \
    }
GUARDED(io_close, (sqlite3_file *f), (f))
GUARDED(io_read, (sqlite3_file *f, void *p, int n, sqlite3_int64 o), (f,p,n,o))
GUARDED(io_write, (sqlite3_file *f, const void *p, int n, sqlite3_int64 o), (f,p,n,o))
GUARDED(io_truncate, (sqlite3_file *f, sqlite3_int64 n), (f,n))
GUARDED(io_sync, (sqlite3_file *f, int flags), (f,flags))
GUARDED(io_size, (sqlite3_file *f, sqlite3_int64 *n), (f,n))
const sqlite3_io_methods badge_io_methods={.iVersion=1,.xClose=io_close,.xRead=io_read,.xWrite=io_write,.xTruncate=io_truncate,.xSync=io_sync,.xFileSize=io_size,.xLock=io_lock,.xUnlock=io_unlock,.xCheckReservedLock=io_reserved,.xFileControl=io_control,.xSectorSize=io_sector,.xDeviceCharacteristics=io_caps};
static int vfs_open_impl(sqlite3_vfs*v,const char*name,sqlite3_file*f,int flags,int*outflags){
    if(!name)return SQLITE_CANTOPEN;mp_obj_t handle;
    int rc=invoke(0,mp_obj_new_str(name,strlen(name)),mp_obj_new_int(flags),mp_const_none,&handle);
    if(rc)return rc;BadgeFile*b=(BadgeFile*)f;b->handle=mp_obj_get_int(handle);b->lock=SQLITE_LOCK_NONE;b->base.pMethods=&badge_io_methods;if(outflags)*outflags=flags;return SQLITE_OK;
}
static int vfs_delete_impl(sqlite3_vfs*v,const char*name,int syncDir){return invoke(7,mp_obj_new_str(name,strlen(name)),MP_OBJ_NEW_SMALL_INT(syncDir),mp_const_none,NULL);}
static int vfs_access_impl(sqlite3_vfs*v,const char*name,int flags,int*out){mp_obj_t exists;int rc=invoke(8,mp_obj_new_str(name,strlen(name)),MP_OBJ_NEW_SMALL_INT(flags),mp_const_none,&exists);if(!rc)*out=mp_obj_is_true(exists);return rc;}
static int vfs_fullpath(sqlite3_vfs*v,const char*name,int n,char*out){size_t len=strlen(name);if(len>=(size_t)n)return SQLITE_CANTOPEN;memcpy(out,name,len+1);return SQLITE_OK;}
GUARDED(vfs_open, (sqlite3_vfs *v,const char *n,sqlite3_file *f,int flags,int *out), (v,n,f,flags,out))
GUARDED(vfs_delete, (sqlite3_vfs *v,const char *n,int sync), (v,n,sync))
GUARDED(vfs_access, (sqlite3_vfs *v,const char *n,int flags,int *out), (v,n,flags,out))
void badge_vfs_install(void){badge_vfs.szOsFile=sizeof(BadgeFile);badge_vfs.xOpen=vfs_open;badge_vfs.xDelete=vfs_delete;badge_vfs.xAccess=vfs_access;badge_vfs.xFullPathname=vfs_fullpath;}
