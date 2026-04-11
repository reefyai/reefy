################################################################################
#
# mgmt
#
################################################################################

MGMT_VERSION = 1.0.2
MGMT_SITE = $(call github,purpleidea,mgmt,$(MGMT_VERSION))

MGMT_LICENSE = GPL-3.0+
MGMT_LICENSE_FILES = LICENSE

MGMT_GOMOD = github.com/purpleidea/mgmt

# Keep docker support, exclude C-dependent augeas and libvirt
MGMT_TAGS = noaugeas novirt netgo

# Upstream v1.0.2 go.sum is missing h1: hash for github.com/google/go-cmp v0.7.0
# (fixed on master). Buildroot's go-post-process runs "go mod vendor" which
# fails because go.sum is incomplete and go 1.24 defaults to -mod=readonly.
# Fix: skip go-post-process and vendor ourselves in POST_PATCH, running
# "go mod tidy" first to add missing go.sum entries before vendoring.
MGMT_GO_ENV = \
	GONOSUMCHECK='*' \
	GONOSUMDB='*'

MGMT_LDFLAGS = \
	-X main.program=mgmt \
	-X main.version=$(MGMT_VERSION) \
	-s -w

MGMT_BUILD_TARGETS = .

$(eval $(golang-package))

# Skip buildroot's go-post-process (fails on incomplete go.sum).
# Do vendoring in POST_PATCH instead, with go mod tidy to fix go.sum first.
MGMT_DOWNLOAD_POST_PROCESS =

define MGMT_VENDOR
	cd $(@D) && \
	$(HOST_GO_COMMON_ENV) \
	GOPROXY=direct \
	GONOSUMCHECK='*' \
	GONOSUMDB='*' \
	GOFLAGS= \
	go mod tidy && \
	$(HOST_GO_COMMON_ENV) \
	GOPROXY=direct \
	GONOSUMCHECK='*' \
	GONOSUMDB='*' \
	GOFLAGS= \
	go mod vendor -v -modcacherw
endef
MGMT_POST_PATCH_HOOKS += MGMT_VENDOR

# mgmt requires code generation before building:
# 1. ragel (system package) - interpolation parser
# 2. nex (Go tool) - lexer generator
# 3. goyacc (Go tool) - parser generator
# 4. goimports (Go tool) - import fixing for generated code
# 5. funcgen (internal) - mcl function definitions
# 6. WASM binary for http_server_ui
#
# Requires: apt install ragel (ragel 6.x)
MGMT_GOINSTALL_ENV = \
	$(HOST_GO_COMMON_ENV) \
	GOPROXY=direct \
	GONOSUMCHECK='*' \
	GONOSUMDB='*' \
	GOFLAGS= \
	GOBIN=$(HOST_DIR)/bin

# Environment for goimports/gofmt (needs GOROOT to resolve packages)
MGMT_FMT_ENV = $(HOST_GO_COMMON_ENV) GOFLAGS=

define MGMT_CODEGEN
	# Install Go-based code generation tools
	$(MGMT_GOINSTALL_ENV) $(GO_BIN) install github.com/blynn/nex@latest
	$(MGMT_GOINSTALL_ENV) $(GO_BIN) install golang.org/x/tools/cmd/goyacc@latest
	# Generate lexer (nex): merge duplicate import blocks and deduplicate
	cd $(@D)/lang && \
	$(HOST_DIR)/bin/nex -e -o parser/lexer.nn.go parser/lexer.nex && \
	sed -i '/^import "unsafe"$$/d' parser/lexer.nn.go && \
	perl -0777 -i -pe 's/\)\n\nimport \(\n/\n/g' parser/lexer.nn.go && \
	awk '/^import \(/{imp=1;print;next} imp && /^\)/{imp=0;print;next} imp{if(!seen[$$0]++)print;next} {print}' \
	parser/lexer.nn.go > parser/lexer.nn.go.tmp && \
	mv parser/lexer.nn.go.tmp parser/lexer.nn.go && \
	$(MGMT_FMT_ENV) gofmt -s -w parser/lexer.nn.go
	# Generate parser (goyacc)
	cd $(@D)/lang && \
	$(HOST_DIR)/bin/goyacc -o parser/y.go parser/parser.y && \
	$(MGMT_FMT_ENV) gofmt -s -w parser/y.go
	# Generate interpolation parser (ragel - requires system package)
	cd $(@D)/lang && \
	ragel -Z -G2 -o interpolate/parse.generated.go interpolate/parse.rl && \
	sed -i '1{/^\/\/ line /d}' interpolate/parse.generated.go && \
	$(MGMT_FMT_ENV) gofmt -s -w interpolate/parse.generated.go
	# Generate mcl function definitions (funcgen)
	cd $(@D) && \
	$(HOST_GO_COMMON_ENV) GOFLAGS=-mod=vendor \
	$(GO_BIN) run `find lang/funcs/funcgen/ -maxdepth 1 -type f -name '*.go' ! -name '*_test.go'` \
	-templates=lang/funcs/funcgen/templates/generated_funcs.go.tpl
	$(MGMT_FMT_ENV) gofmt -s -w $(@D)/lang/core/generated_funcs.go
	# Build embedded WASM binary (http_server_ui)
	cd $(@D)/engine/resources && \
	$(HOST_GO_COMMON_ENV) \
	GOOS=js GOARCH=wasm \
	$(GO_BIN) build -trimpath -tags "noaugeas novirt netgo" \
	-o http_server_ui/main.wasm ./http_server_ui/
endef
MGMT_PRE_BUILD_HOOKS += MGMT_CODEGEN
