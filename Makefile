# Makefile for rrcc (RPM Repository Consistency Checker)
#
# All settings can be overridden from the environment or the command line:
#
#   make install
#   PREFIX=/usr make install
#   make install PREFIX=/opt/rrcc DESTDIR=/tmp/stage

PROGRAM  := rrcc
SCRIPT   := rrcc.py

PREFIX      ?= $(HOME)/.local
EXEC_PREFIX ?= $(PREFIX)
BINDIR      ?= $(EXEC_PREFIX)/bin
DATAROOTDIR ?= $(PREFIX)/share
DOCDIR      ?= $(DATAROOTDIR)/doc/$(PROGRAM)
INSTALL     ?= install
DESTDIR     ?=

INSTALL_PROGRAM ?= $(INSTALL) -m 0755
INSTALL_DATA    ?= $(INSTALL) -m 0644

DOCS := README.md ChangeLog

.PHONY: all install uninstall help

all: help

help:
	@echo "Targets:"
	@echo "  install    Install $(PROGRAM) and documentation"
	@echo "  uninstall  Remove what 'make install' installed"
	@echo
	@echo "Settings (environment or command line, current values shown):"
	@echo "  PREFIX=$(PREFIX)"
	@echo "  BINDIR=$(BINDIR)"
	@echo "  DOCDIR=$(DOCDIR)"
	@echo "  DESTDIR=$(DESTDIR)"

install:
	$(INSTALL) -d $(DESTDIR)$(BINDIR)
	$(INSTALL_PROGRAM) $(SCRIPT) $(DESTDIR)$(BINDIR)/$(PROGRAM)
	$(INSTALL) -d $(DESTDIR)$(DOCDIR)
	$(INSTALL_DATA) $(DOCS) $(DESTDIR)$(DOCDIR)/

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/$(PROGRAM)
	for f in $(DOCS); do rm -f $(DESTDIR)$(DOCDIR)/$$f; done
	-rmdir $(DESTDIR)$(DOCDIR)
