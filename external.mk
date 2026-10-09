include $(sort $(wildcard $(BR2_EXTERNAL_REEFY_PATH)/package/*/*.mk))
include $(sort $(wildcard $(BR2_EXTERNAL_REEFY_PATH)/board/reefy/reefy/package-overrides/*.mk))
# Capture exact installed ELF files before Buildroot's target-finalize strip.
# The content-addressed cache also handles later warm finalization runs.
define REEFY_CAPTURE_USERSPACE_DEBUG
	python3 $(BR2_EXTERNAL_REEFY_PATH)/tools/userspace-debug/archive.py capture --output $(BASE_DIR)
endef
TARGET_FINALIZE_HOOKS += REEFY_CAPTURE_USERSPACE_DEBUG

