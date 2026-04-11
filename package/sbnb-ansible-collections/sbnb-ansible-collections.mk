################################################################################
#
# sbnb-ansible-collections
#
################################################################################

SBNB_ANSIBLE_COLLECTIONS_VERSION = 1.0
SBNB_ANSIBLE_COLLECTIONS_SOURCE =
SBNB_ANSIBLE_COLLECTIONS_LICENSE = Various (see individual collections)

# Download collections directly from Ansible Galaxy
SBNB_ANSIBLE_COLLECTIONS_EXTRA_DOWNLOADS = \
	https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/artifacts/community-general-12.4.0.tar.gz \
	https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/artifacts/community-crypto-3.1.1.tar.gz \
	https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/artifacts/community-docker-5.0.6.tar.gz \
	https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/artifacts/ansible-posix-2.1.0.tar.gz

define SBNB_ANSIBLE_COLLECTIONS_INSTALL_TARGET_CMDS
	# Extract each collection to its proper namespace/name directory
	mkdir -p $(TARGET_DIR)/usr/share/ansible/collections/ansible_collections/community/general
	tar -xzf $(SBNB_ANSIBLE_COLLECTIONS_DL_DIR)/community-general-12.4.0.tar.gz \
		-C $(TARGET_DIR)/usr/share/ansible/collections/ansible_collections/community/general

	mkdir -p $(TARGET_DIR)/usr/share/ansible/collections/ansible_collections/community/crypto
	tar -xzf $(SBNB_ANSIBLE_COLLECTIONS_DL_DIR)/community-crypto-3.1.1.tar.gz \
		-C $(TARGET_DIR)/usr/share/ansible/collections/ansible_collections/community/crypto

	mkdir -p $(TARGET_DIR)/usr/share/ansible/collections/ansible_collections/community/docker
	tar -xzf $(SBNB_ANSIBLE_COLLECTIONS_DL_DIR)/community-docker-5.0.6.tar.gz \
		-C $(TARGET_DIR)/usr/share/ansible/collections/ansible_collections/community/docker

	mkdir -p $(TARGET_DIR)/usr/share/ansible/collections/ansible_collections/ansible/posix
	tar -xzf $(SBNB_ANSIBLE_COLLECTIONS_DL_DIR)/ansible-posix-2.1.0.tar.gz \
		-C $(TARGET_DIR)/usr/share/ansible/collections/ansible_collections/ansible/posix
endef

$(eval $(generic-package))
