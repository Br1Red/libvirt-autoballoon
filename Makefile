PREFIX ?= /

SRC_DIR := $(dir $(lastword $(MAKEFILE_LIST)))

S_SERVICE := $(SRC_DIR)/libvirt-autoreclaim.service
S_BIN := $(SRC_DIR)/libvirt-autoreclaim.py
S_CONF := $(SRC_DIR)/autoreclaim.json
S_VIRTIOMEM_CONVERT := $(SRC_DIR)/virtiomem_convert.py
S_VIRTIOMEM_CONVERT_MAN := $(SRC_DIR)/virtiomem_convert.1

SERVICE := $(PREFIX)/lib/systemd/system/libvirt-autoreclaim.service
BIN := $(PREFIX)/usr/bin/libvirt-autoreclaim
CONF := $(PREFIX)/etc/libvirt/autoreclaim.json
VIRTIOMEM_CONVERT := $(PREFIX)/usr/bin/virtiomem_convert
VIRTIOMEM_CONVERT_MAN := $(PREFIX)/usr/share/man/man1/virtiomem_convert.1

default:  help

$(BIN): $(S_BIN)
	install -Dm755 $< $@

$(VIRTIOMEM_CONVERT): $(S_VIRTIOMEM_CONVERT)
	install -Dm755 $< $@

$(VIRTIOMEM_CONVERT_MAN): $(S_VIRTIOMEM_CONVERT_MAN)
	install -Dm644 $< $@

$(SERVICE): $(S_SERVICE)
	install -Dm644 $< $@

$(CONF): $(S_CONF)
	install -Dm644 $< $@

install: ## Install libvirt-autoreclaim
install: $(BIN) $(SERVICE) $(CONF) $(VIRTIOMEM_CONVERT) $(VIRTIOMEM_CONVERT_MAN)

uninstall: ## Delete libvirt-autoreclaim
uninstall:
	@rm -fv $(BIN) $(SERVICE) $(CONF) $(VIRTIOMEM_CONVERT) $(VIRTIOMEM_CONVERT_MAN)

help: ## Show help
	@grep -h "##" $(MAKEFILE_LIST) | grep -v grep | sed -e 's/\\$$//' | column -t -s '##'
