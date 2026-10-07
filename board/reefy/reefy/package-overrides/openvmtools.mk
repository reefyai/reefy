# GCC 15 defaults to C23, which rejects the old callback declarations in
# Buildroot's open-vm-tools 11.3.5. Keep only this package in GNU C17 mode.
# Remove when the upstream package reaches 13.0.0, which includes the fix:
# https://github.com/vmware/open-vm-tools/issues/750
ifeq ($(OPENVMTOOLS_VERSION),11.3.5-18557794)
ifeq ($(BR2_TOOLCHAIN_GCC_AT_LEAST_15),y)
OPENVMTOOLS_CONF_ENV += CFLAGS="$(TARGET_CFLAGS) -std=gnu17"
endif
endif
