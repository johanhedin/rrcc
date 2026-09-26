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
BASHCOMPDIR ?= $(DATAROOTDIR)/bash-completion/completions
MANDIR      ?= $(DATAROOTDIR)/man
MAN1DIR     ?= $(MANDIR)/man1
INSTALL     ?= install
DESTDIR     ?=

INSTALL_PROGRAM ?= $(INSTALL) -m 0755
INSTALL_DATA    ?= $(INSTALL) -m 0644
# Man pages are installed gzip-compressed. Not named GZIP, since gzip itself
# reads that variable from the environment as default options.
MANCOMPRESS     ?= gzip -9nc

DOCS := README.md ChangeLog
BASHCOMP := bash-completion/$(PROGRAM)
MAN1     := man/$(PROGRAM).1

.PHONY: all install uninstall help

all: help

help:
	@echo "Targets:"
	@echo "  install    Install $(PROGRAM), bash completion, man page and documentation"
	@echo "  uninstall  Remove what 'make install' installed"
	@echo
	@echo "Settings (environment or command line, current values shown):"
	@echo "  PREFIX=$(PREFIX)"
	@echo "  BINDIR=$(BINDIR)"
	@echo "  DOCDIR=$(DOCDIR)"
	@echo "  BASHCOMPDIR=$(BASHCOMPDIR)"
	@echo "  MANDIR=$(MANDIR)"
	@echo "  DESTDIR=$(DESTDIR)"

install:
	$(INSTALL) -d $(DESTDIR)$(BINDIR)
	$(INSTALL_PROGRAM) $(SCRIPT) $(DESTDIR)$(BINDIR)/$(PROGRAM)
	$(INSTALL) -d $(DESTDIR)$(BASHCOMPDIR)
	$(INSTALL_DATA) $(BASHCOMP) $(DESTDIR)$(BASHCOMPDIR)/$(PROGRAM)
	$(INSTALL) -d $(DESTDIR)$(MAN1DIR)
	$(MANCOMPRESS) $(MAN1) > $(DESTDIR)$(MAN1DIR)/$(notdir $(MAN1)).gz
	chmod 0644 $(DESTDIR)$(MAN1DIR)/$(notdir $(MAN1)).gz
	$(INSTALL) -d $(DESTDIR)$(DOCDIR)
	$(INSTALL_DATA) $(DOCS) $(DESTDIR)$(DOCDIR)/

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/$(PROGRAM)
	rm -f $(DESTDIR)$(BASHCOMPDIR)/$(PROGRAM)
	rm -f $(DESTDIR)$(MAN1DIR)/$(notdir $(MAN1)).gz
	for f in $(DOCS); do rm -f $(DESTDIR)$(DOCDIR)/$$f; done
	-rmdir $(DESTDIR)$(DOCDIR)
