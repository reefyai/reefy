################################################################################
#
# borgbackup
#
# Pre-built standalone binary from upstream GitHub releases.
#
################################################################################

BORGBACKUP_VERSION = 1.4.0
BORGBACKUP_SOURCE = borg-linux-glibc228
BORGBACKUP_SITE = https://github.com/borgbackup/borg/releases/download/$(BORGBACKUP_VERSION)
BORGBACKUP_LICENSE = BSD-3-Clause
BORGBACKUP_LICENSE_FILES = LICENSE

define BORGBACKUP_EXTRACT_CMDS
	cp $(BORGBACKUP_DL_DIR)/$(BORGBACKUP_SOURCE) $(@D)/borg
	chmod +x $(@D)/borg
endef

define BORGBACKUP_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/borg $(TARGET_DIR)/usr/bin/borg
endef

$(eval $(generic-package))
