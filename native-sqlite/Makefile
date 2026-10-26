# Build with tools/build.py, which prepares and verifies the source inputs.
MOD = native_sqlite
BUILD ?= build/sqlite
VARIANT ?= sqlite
MPY_DIR ?= vendor/micropython
SQLITE_DIR ?= vendor/sqlite
SQLITE_ADAPTED = build/generated/sqlite3-adapted.inc
ARCH = armv7emsp
SRC = src/module.c src/sqlite_port.c src/libc_shim.c src/vfs.c
LINK_RUNTIME = 1
CFLAGS_EXTRA += -Isrc -I$(SQLITE_DIR) -Ibuild/generated
CFLAGS_EXTRA += -DMICROPY_OBJ_REPR=MICROPY_OBJ_REPR_C -fno-builtin -fdata-sections -mfloat-abi=softfp
CFLAGS_EXTRA += -Wno-misleading-indentation -Wno-unused-value -Wno-unused-function -Wno-unused-variable -Wno-unused-but-set-variable -Wno-sign-compare
ifeq ($(VARIANT),powersync)
CFLAGS_EXTRA += -DNATIVE_SQLITE_POWERSYNC
MPY_LD_FLAGS += -l$(CORE_ARCHIVE)
endif
include $(MPY_DIR)/py/dynruntime.mk
# Variants share this final filename; always merge the selected intermediate.
.PHONY: native_sqlite.mpy $(BUILD)/native_sqlite.mpy
# Ask GCC for the libraries matching the qualified softfp calling convention.
LIBGCC_PATH = $(shell $(CC) -mthumb -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=softfp --print-libgcc-file-name)
LIBM_PATH = $(shell $(CC) -mthumb -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=softfp --print-file-name=libm.a)
$(BUILD)/src/sqlite_port.o: $(SQLITE_ADAPTED) src/config.h
