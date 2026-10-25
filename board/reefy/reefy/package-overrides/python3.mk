# ensurepip can seed virtual environments from its independently bundled pip.
# Keep its version/code intact and apply the upstream CA data with wheel's
# standard unpack/pack tools, which verify and regenerate RECORD hashes.
define REEFY_FIX_ENSUREPIP_CA_DATA
	$(HOST_DIR)/bin/python3 $(BR2_EXTERNAL_REEFY_PATH)/tools/ci/patch-ensurepip-ca.py \
		--root $(TARGET_DIR) \
		--patch-dir $(BR2_EXTERNAL_REEFY_PATH)/board/reefy/reefy/patches/python-pip \
		--report $(BASE_DIR)/reefy-python-ca-provenance.json
endef
TARGET_FINALIZE_HOOKS += REEFY_FIX_ENSUREPIP_CA_DATA
