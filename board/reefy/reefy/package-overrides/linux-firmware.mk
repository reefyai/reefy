# Buildroot's MediaTek selections cover PCIe firmware but omit these files
# requested by the already-enabled mt76x0u and mt7663u/mt7663s drivers.
# Extend the package's own archive/install and WHENCE symlink handling.
ifeq ($(BR2_PACKAGE_LINUX_FIRMWARE_MEDIATEK_MT7610E),y)
LINUX_FIRMWARE_FILES += mediatek/mt7610u.bin mediatek/mt7650e.bin
LINUX_FIRMWARE_ALL_LICENSE_FILES += LICENSES/LICENCE.ralink_a_mediatek_company_firmware
endif

ifeq ($(BR2_PACKAGE_LINUX_FIRMWARE_IWLWIFI_6E),y)
# The kernel depends on linux-firmware, so source/config checks run at target
# finalization to avoid a dependency cycle. Archive selection checks the pins.
LINUX_FIRMWARE_DEPENDENCIES += host-python3
REEFY_INTEL_WIFI_TOOL = $(BR2_EXTERNAL_REEFY_PATH)/board/reefy/reefy/scripts/intel_wifi_firmware.py
REEFY_INTEL_WIFI_POLICY = $(BR2_EXTERNAL_REEFY_PATH)/board/reefy/reefy/package-overrides/intel-wifi-firmware.json
REEFY_INTEL_WIFI_CMD = $(HOST_DIR)/bin/python3 $(REEFY_INTEL_WIFI_TOOL) --manifest $(REEFY_INTEL_WIFI_POLICY)

define REEFY_SELECT_INTEL_WIFI_FIRMWARE
	$(REEFY_INTEL_WIFI_CMD) prepare --archive $(@D)/br-firmware.tar \
		--kernel-ref $(call qstrip,$(BR2_LINUX_KERNEL_CUSTOM_REPO_VERSION)) \
		--firmware-version $(LINUX_FIRMWARE_VERSION)
endef
LINUX_FIRMWARE_POST_BUILD_HOOKS += REEFY_SELECT_INTEL_WIFI_FIRMWARE

define REEFY_CLEAN_INTEL_WIFI_TARGET
	$(REEFY_INTEL_WIFI_CMD) clean --destination $(TARGET_DIR) --target
endef
LINUX_FIRMWARE_PRE_INSTALL_TARGET_HOOKS += REEFY_CLEAN_INTEL_WIFI_TARGET

define REEFY_COMPRESS_INTEL_WIFI_TARGET
	$(REEFY_INTEL_WIFI_CMD) install --destination $(TARGET_DIR) --target \
		--report $(BASE_DIR)/reefy-intel-wifi-target.json
endef
LINUX_FIRMWARE_POST_INSTALL_TARGET_HOOKS += REEFY_COMPRESS_INTEL_WIFI_TARGET

define REEFY_CLEAN_INTEL_WIFI_IMAGES
	$(REEFY_INTEL_WIFI_CMD) clean --destination $(BINARIES_DIR)
endef
LINUX_FIRMWARE_PRE_INSTALL_IMAGES_HOOKS += REEFY_CLEAN_INTEL_WIFI_IMAGES

define REEFY_COMPRESS_INTEL_WIFI_IMAGES
	$(REEFY_INTEL_WIFI_CMD) install --destination $(BINARIES_DIR) \
		--report $(BASE_DIR)/reefy-intel-wifi-images.json
endef
LINUX_FIRMWARE_POST_INSTALL_IMAGES_HOOKS += REEFY_COMPRESS_INTEL_WIFI_IMAGES

define REEFY_VERIFY_INTEL_WIFI_KERNEL
	$(REEFY_INTEL_WIFI_CMD) check-kernel --kernel-dir $(LINUX_DIR)
	$(REEFY_INTEL_WIFI_CMD) verify --destination $(TARGET_DIR) --target
endef
TARGET_FINALIZE_HOOKS += REEFY_VERIFY_INTEL_WIFI_KERNEL
endif

ifeq ($(BR2_PACKAGE_LINUX_FIRMWARE_MEDIATEK_MT76X2E),y)
LINUX_FIRMWARE_FILES += mediatek/mt7663pr2h.bin mediatek/mt7663_n9_v3.bin \
	mediatek/mt7663pr2h_rebb.bin mediatek/mt7663_n9_rebb.bin
LINUX_FIRMWARE_ALL_LICENSE_FILES += LICENSES/LICENCE.mediatek
endif
