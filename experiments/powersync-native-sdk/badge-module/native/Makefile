# Isolated SDK candidate; never builds or overwrites native-sqlite's artifacts.
MOD = native_powersync_sdk
BUILD ?= build/sdk
MPY_DIR ?= ../../../../native-sqlite/vendor/micropython
SQLITE_DIR ?= ../../../../native-sqlite/vendor/sqlite
SQLITE_ADAPTED = build/generated/sqlite3-adapted.inc
ARCH = armv7emsp
SRC = src/module.c src/sqlite_port.c src/libc_shim.c src/vfs.c
LINK_RUNTIME = 1
CFLAGS_EXTRA += -Isrc -I../rust -I$(SQLITE_DIR) -Ibuild/generated
CFLAGS_EXTRA += -DMICROPY_OBJ_REPR=MICROPY_OBJ_REPR_C -fno-builtin -fdata-sections -mfloat-abi=softfp
CFLAGS_EXTRA += -DNATIVE_SQLITE_POWERSYNC -DNATIVE_POWERSYNC_SDK
CFLAGS_EXTRA += -Wno-misleading-indentation -Wno-unused-value -Wno-unused-function -Wno-unused-variable -Wno-unused-but-set-variable -Wno-sign-compare
ifndef SDK_ARCHIVE
$(error SDK_ARCHIVE must point to the actual full native SDK bridge static library)
endif
MPY_LD_FLAGS += -l$(SDK_ARCHIVE)
include $(MPY_DIR)/py/dynruntime.mk
.PHONY: native_powersync_sdk.mpy $(BUILD)/native_powersync_sdk.mpy
LIBGCC_PATH = $(shell $(CC) -mthumb -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=softfp --print-libgcc-file-name)
LIBM_PATH = $(shell $(CC) -mthumb -mcpu=cortex-m4 -mfpu=fpv4-sp-d16 -mfloat-abi=softfp --print-file-name=libm.a)
$(BUILD)/src/sqlite_port.o: $(SQLITE_ADAPTED) src/config.h
