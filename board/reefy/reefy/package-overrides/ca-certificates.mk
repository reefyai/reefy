# Package installs do not remove roots retired by a newer upstream bundle.
# Replace only the package-owned Mozilla directory on warm builds, leaving
# local trust additions and the independent MQTT CA chain untouched.
define REEFY_CLEAR_OLD_MOZILLA_TARGET_CERTS
	rm -rf $(TARGET_DIR)/usr/share/ca-certificates/mozilla
endef
CA_CERTIFICATES_PRE_INSTALL_TARGET_HOOKS += REEFY_CLEAR_OLD_MOZILLA_TARGET_CERTS

define REEFY_CLEAR_OLD_MOZILLA_HOST_CERTS
	rm -rf $(HOST_DIR)/usr/share/ca-certificates/mozilla
endef
HOST_CA_CERTIFICATES_PRE_INSTALL_HOOKS += REEFY_CLEAR_OLD_MOZILLA_HOST_CERTS
