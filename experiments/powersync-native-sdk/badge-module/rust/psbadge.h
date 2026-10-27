#ifndef PSBADGE_NATIVE_H
#define PSBADGE_NATIVE_H
#include <stddef.h>
#include <stdint.h>
typedef struct { unsigned char *ptr; size_t len; size_t capacity; } PsbBuffer;
void *psb_start(void *db, const unsigned char *schema, size_t schema_len,
    const unsigned char *endpoint, size_t endpoint_len, const unsigned char *room, size_t room_len);
int psb_command(void *ctx, const unsigned char *json, size_t len);
PsbBuffer psb_poll(void *ctx, uint32_t elapsed_ms, uint32_t budget);
void psb_buffer_free(PsbBuffer);
int psb_stop(void *ctx);
int psb_destroy(void *ctx);
void *psb_sql_enter(void *ctx);
void psb_sql_leave(void *lease);
/* C owns one permanent engine pointer per imported module instance. */
void *psb_runtime_context(void);
void psb_set_runtime_context(void *);
void psb_trace(uint32_t stage);
void psb_panic_report(const unsigned char *file, size_t length, uint32_t line);
void psb_panic_abort(void) __attribute__((noreturn));
#endif
