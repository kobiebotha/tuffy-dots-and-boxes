#include <stddef.h>
#ifndef NATIVE_SQLITE_POWERSYNC
int memcmp(const void *a, const void *b, size_t n) { const unsigned char *x=a,*y=b; while(n--) { if(*x!=*y) return *x-*y; x++; y++; } return 0; }
size_t strlen(const char *s) { const char *p=s; while(*p)p++; return p-s; }
#endif
int strcmp(const char *a, const char *b) { while(*a && *a==*b) {a++;b++;} return (unsigned char)*a-(unsigned char)*b; }
int strncmp(const char *a, const char *b, size_t n) { while(n--) {if(*a!=*b || !*a)return (unsigned char)*a-(unsigned char)*b; a++;b++;} return 0; }
char *strchr(const char *s,int c) {for(;;s++) {if(*s==(char)c)return (char*)s; if(!*s)return NULL;}}
char *strrchr(const char *s,int c) {const char *r=NULL; do{if(*s==(char)c)r=s;}while(*s++);return (char*)r;}
void *memchr(const void *s,int c,size_t n) {const unsigned char *p=s;while(n--){if(*p==(unsigned char)c)return (void*)p;p++;}return NULL;}
size_t strspn(const char *s,const char *accept) {size_t n=0;while(s[n] && strchr(accept,s[n]))n++;return n;}
size_t strcspn(const char *s,const char *reject) {size_t n=0;while(s[n] && !strchr(reject,s[n]))n++;return n;}
