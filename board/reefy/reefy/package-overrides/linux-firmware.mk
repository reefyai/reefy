# Buildroot's MediaTek selections cover PCIe firmware but omit these files
# requested by the already-enabled mt76x0u and mt7663u/mt7663s drivers.
# Extend the package's own archive/install and WHENCE symlink handling.
ifeq ($(BR2_PACKAGE_LINUX_FIRMWARE_MEDIATEK_MT7610E),y)
LINUX_FIRMWARE_FILES += mediatek/mt7610u.bin mediatek/mt7650e.bin
LINUX_FIRMWARE_ALL_LICENSE_FILES += LICENSES/LICENCE.ralink_a_mediatek_company_firmware
endif

ifeq ($(BR2_PACKAGE_LINUX_FIRMWARE_MEDIATEK_MT76X2E),y)
LINUX_FIRMWARE_FILES += mediatek/mt7663pr2h.bin mediatek/mt7663_n9_v3.bin \
	mediatek/mt7663pr2h_rebb.bin mediatek/mt7663_n9_rebb.bin
LINUX_FIRMWARE_ALL_LICENSE_FILES += LICENSES/LICENCE.mediatek
endif
