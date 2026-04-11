################################################################################
#
# tailscale-reefy
#
# Local fork of upstream buildroot's tailscale package. See Config.in help
# text for the full rationale. The build output is identical to a normal
# tailscale build, just with the broken /usr/sbin symlink hook omitted.
#
################################################################################

TAILSCALE_REEFY_VERSION = 1.94.2
TAILSCALE_REEFY_SITE = $(call github,tailscale,tailscale,v$(TAILSCALE_REEFY_VERSION))
TAILSCALE_REEFY_LICENSE = BSD-3-Clause
TAILSCALE_REEFY_LICENSE_FILES = LICENSE
TAILSCALE_REEFY_GOMOD = tailscale.com
TAILSCALE_REEFY_CPE_ID_VENDOR = tailscale
TAILSCALE_REEFY_BUILD_TARGETS = cmd/tailscale cmd/tailscaled
TAILSCALE_REEFY_LDFLAGS = \
	-X tailscale.com/version.longStamp=$(TAILSCALE_REEFY_VERSION) \
	-X tailscale.com/version.shortStamp=$(TAILSCALE_REEFY_VERSION)

# Use Go module proxy because a transitive dependency (tdakkota/asciicheck)
# has had its GitHub repo deleted; proxy.golang.org still serves cached copies.
TAILSCALE_REEFY_GO_ENV = GOPROXY=https://proxy.golang.org,direct

define TAILSCALE_REEFY_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(@D)/cmd/tailscaled/tailscaled.defaults \
		$(TARGET_DIR)/etc/default/tailscaled
	$(INSTALL) -D -m 0644 $(@D)/cmd/tailscaled/tailscaled.service \
		$(TARGET_DIR)/usr/lib/systemd/system/tailscaled.service
endef

# DELIBERATELY OMIT TAILSCALE_INSTALL_SYMLINK — see Config.in.

define TAILSCALE_REEFY_LINUX_CONFIG_FIXUPS
	$(call KCONFIG_ENABLE_OPT,CONFIG_IPV6)
	$(call KCONFIG_ENABLE_OPT,CONFIG_IPV6_MULTIPLE_TABLES)
	$(call KCONFIG_ENABLE_OPT,CONFIG_NETFILTER)
	$(call KCONFIG_ENABLE_OPT,CONFIG_NETFILTER_NETLINK)
	$(call KCONFIG_ENABLE_OPT,CONFIG_NFT_CT)
	$(call KCONFIG_ENABLE_OPT,CONFIG_NFT_MASQ)
	$(call KCONFIG_ENABLE_OPT,CONFIG_NFT_NAT)
	$(call KCONFIG_ENABLE_OPT,CONFIG_NF_CONNTRACK)
	$(call KCONFIG_ENABLE_OPT,CONFIG_NF_NAT)
	$(call KCONFIG_ENABLE_OPT,CONFIG_NF_TABLES)
	$(call KCONFIG_ENABLE_OPT,CONFIG_NF_TABLES_INET)
	$(call KCONFIG_ENABLE_OPT,CONFIG_NF_TABLES_IPV4)
	$(call KCONFIG_ENABLE_OPT,CONFIG_NF_TABLES_IPV6)
	$(call KCONFIG_ENABLE_OPT,CONFIG_TUN)
endef

$(eval $(golang-package))
