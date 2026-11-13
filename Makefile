# Installs the AllStarLink Zabbix collector on an ASL host.
#
#   sudo make install      install script, sudoers rule, agent config; restart agent
#   make check             run the collector through the agent's own path
#   sudo make uninstall    remove everything again
#
# Staged installs (packaging, testing) work with DESTDIR=/some/root; ownership
# changes and the agent restart are skipped in that case.

DESTDIR     ?=
BINDIR      ?= /usr/local/bin
SUDOERSDIR  ?= /etc/sudoers.d
AGENTCONFDIR ?= /etc/zabbix/zabbix_agent2.d
AGENTSERVICE ?= zabbix-agent2

SCRIPT      := $(BINDIR)/asl-zabbix-stats
SUDOERS     := $(SUDOERSDIR)/zabbix-asl
AGENTCONF   := $(AGENTCONFDIR)/asl.conf

ifeq ($(strip $(DESTDIR)),)
OWNER := -o root -g root
else
OWNER :=
endif

.PHONY: all install uninstall restart check validate

all:
	@echo "Nothing to build. Run 'sudo make install' on the ASL host."

# Refuse to install a sudoers file that does not parse.
validate:
	@if command -v visudo >/dev/null 2>&1; then \
		visudo -cf sudoers.d/zabbix-asl; \
	else \
		echo "visudo not found; skipping sudoers syntax check"; \
	fi
	python3 -m py_compile asl_zabbix_stats.py

install: validate
	install -d $(DESTDIR)$(BINDIR) $(DESTDIR)$(SUDOERSDIR) $(DESTDIR)$(AGENTCONFDIR)
	install $(OWNER) -m 0755 asl_zabbix_stats.py $(DESTDIR)$(SCRIPT)
	install $(OWNER) -m 0440 sudoers.d/zabbix-asl $(DESTDIR)$(SUDOERS)
	install $(OWNER) -m 0644 zabbix_agent2.d/asl.conf $(DESTDIR)$(AGENTCONF)
ifeq ($(strip $(DESTDIR)),)
	$(MAKE) restart
	@echo
	@echo "Installed. Run 'make check', then import template_asl_app_rpt.yaml into Zabbix."
endif

uninstall:
	rm -f $(DESTDIR)$(SCRIPT) $(DESTDIR)$(SUDOERS) $(DESTDIR)$(AGENTCONF)
ifeq ($(strip $(DESTDIR)),)
	$(MAKE) restart
endif

restart:
	systemctl restart $(AGENTSERVICE)

# Exercises the same chain the agent uses: zabbix -> sudo -> asterisk -> script.
check:
	sudo -u zabbix sudo -n -u asterisk $(SCRIPT) | python3 -m json.tool
