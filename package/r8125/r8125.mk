################################################################################
#
# r8125 - Realtek RTL8125 2.5 Gigabit Ethernet driver
#
################################################################################

R8125_VERSION = 024816580bb7792f36d8cc1bd1515443f3749605
R8125_SITE = $(call github,openwrt,rtl8125,$(R8125_VERSION))
R8125_LICENSE = GPL-2.0

$(eval $(kernel-module))
$(eval $(generic-package))
