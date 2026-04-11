################################################################################
#
# r8168 - Realtek RTL8168/RTL8111 Gigabit Ethernet driver
#
# Official Realtek source mirrored by OpenWrt (Realtek's site is CAPTCHA-gated)
#
################################################################################

R8168_VERSION = 92c9d03efe78e3550b1b1a8e5bf85d79a5eb3b7d
R8168_SITE = $(call github,openwrt,rtl8168,$(R8168_VERSION))
R8168_LICENSE = GPL-2.0
R8168_LICENSE_FILES = COPYING

$(eval $(kernel-module))
$(eval $(generic-package))
