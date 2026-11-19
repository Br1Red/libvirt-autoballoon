PREFIX ?= /

SRC_DIR := $(dir $(lastword $(MAKEFILE_LIST)))

S_SERVICE := $(SRC_DIR)/libvirt-autoreclaim.service
S_BIN := $(SRC_DIR)/libvirt-autoreclaim.py
S_CONF := $(SRC_DIR)/autoreclaim.json

SERVICE := $(PREFIX)/lib/systemd/system/libvirt-autoreclaim.service
BIN := $(PREFIX)/usr/bin/libvirt-autoreclaim
CONF := $(PREFIX)/etc/libvirt/autoreclaim.json

default:  help

$(BIN): $(S_BIN)
	install -Dm755 $< $@

$(SERVICE): $(S_SERVICE)
	install -Dm644 $< $@

$(CONF): $(S_CONF)
	install -Dm644 $< $@

install: ## Install libvirt-autoreclaim
install: $(BIN) $(SERVICE) $(CONF)

uninstall: ## Delete libvirt-autoreclaim
uninstall:
	@rm -fv $(BIN) $(SERVICE) $(CONF)

help: ## Show help
	@grep -h "##" $(MAKEFILE_LIST) | grep -v grep | sed -e 's/\\$$//' | column -t -s '##'
