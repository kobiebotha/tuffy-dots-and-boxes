/* Only the host fixture qualifier uses this adapter. It is not a .mpy module. */
#include <stdlib.h>
#ifdef NATIVE_SQLITE_POWERSYNC
extern int powersync_init_static(void);
int fixture_register(void) { return powersync_init_static(); }
void rust_eh_personality(void) { abort(); }
#else
int fixture_register(void) { return 0; }
#endif
