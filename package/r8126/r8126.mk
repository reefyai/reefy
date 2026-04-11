################################################################################
#
# r8126 - Realtek RTL8126 5 Gigabit Ethernet driver
#
################################################################################

R8126_VERSION = 27721fbedc45897cf1f155f5eb44de76962a1ba8
R8126_SITE = $(call github,openwrt,rtl8126,$(R8126_VERSION))
R8126_LICENSE = GPL-2.0

$(eval $(kernel-module))
$(eval $(generic-package))
