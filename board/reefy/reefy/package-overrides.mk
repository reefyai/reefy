# Loaded before Buildroot packages. Keep Go downloads pinned and offline at build.
override GO_VERSION = 1.27.2

# Buildroot copies Go sources without deleting files removed by a newer Go.
# Versioned roots prevent warm caches from mixing standard-library releases.
override HOST_GO_ROOT = $(HOST_DIR)/lib/go-$(GO_VERSION)
override GO_BIN = $(HOST_GO_ROOT)/bin/go
