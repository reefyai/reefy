################################################################################
#
# r8127 - Realtek RTL8127 Ethernet driver
#
################################################################################

R8127_VERSION = f80bc64922ac76f90618f61245fb29743c018d0a
R8127_SITE = $(call github,openwrt,rtl8127,$(R8127_VERSION))
R8127_LICENSE = GPL-2.0

$(eval $(kernel-module))
$(eval $(generic-package))
