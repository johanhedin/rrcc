# Makefile for rrcc (RPM Repository Consistency Checker)
#
# The version and release date are taken from the topmost released entry in
# ChangeLog, which is the single source of truth. Entries without a numeric
# version, e.g. "1.x.y (not released yet)", are skipped. The man page is
# generated from man/rrcc.1.in and 'make check' verifies rrcc.py. 'make test'
# runs the test suite in tests/, 'make test-all' also the slow tests.
#
# All settings can be overridden from the environment or the command line:
#
#   make install
#   PREFIX=/usr make install
#   make install PREFIX=/opt/rrcc DESTDIR=/tmp/stage

PROGRAM  := rrcc
SCRIPT   := rrcc.py
PYTHON   ?= python3

# Topmost released ChangeLog entry, e.g. "* Sat Sep 26 2026 Johan Hedin - 1.1.0"
CL_ENTRY := $(shell sed -n -E '/^\* .* - [0-9]+\.[0-9]+\.[0-9]+[[:space:]]*$$/{p;q}' ChangeLog)
ifeq ($(CL_ENTRY),)
$(error No released version found in ChangeLog)
endif
VERSION    := $(lastword $(CL_ENTRY))
CL_WEEKDAY := $(word 2,$(CL_ENTRY))
CL_DATE    := $(wordlist 3,5,$(CL_ENTRY))
MAN_DATE   := $(shell LC_ALL=C date -d '$(CL_DATE)' '+%B %-d, %Y')

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
MAN1_IN  := $(MAN1).in

.PHONY: all man check test test-all clean install uninstall help

all: help

help:
	@echo "Targets:"
	@echo "  man        Generate $(MAN1) from $(MAN1_IN)"
	@echo "  check      Verify that rrcc.py and ChangeLog agree on version and date"
	@echo "  test       Run the test suite (PYTHON=$(PYTHON))"
	@echo "  test-all   Run the test suite including the slow tests (needs dnf)"
	@echo "  clean      Remove generated files"
	@echo "  install    Install $(PROGRAM), bash completion, man page and documentation"
	@echo "  uninstall  Remove what 'make install' installed"
	@echo
	@echo "Version $(VERSION) ($(CL_DATE)), from ChangeLog"
	@echo
	@echo "Settings (environment or command line, current values shown):"
	@echo "  PREFIX=$(PREFIX)"
	@echo "  BINDIR=$(BINDIR)"
	@echo "  DOCDIR=$(DOCDIR)"
	@echo "  BASHCOMPDIR=$(BASHCOMPDIR)"
	@echo "  MANDIR=$(MANDIR)"
	@echo "  DESTDIR=$(DESTDIR)"

man: $(MAN1)

$(MAN1): $(MAN1_IN) ChangeLog Makefile
	sed -e 's/@VERSION@/$(VERSION)/g' -e 's/@DATE@/$(MAN_DATE)/g' $(MAN1_IN) > $@

check:
	@py=`sed -n 's/^__version__ = "\(.*\)"$$/\1/p' $(SCRIPT)`; \
	if [ "$$py" != "$(VERSION)" ]; then \
		echo "ERROR: $(SCRIPT) has version '$$py' but ChangeLog has '$(VERSION)'" >&2; exit 1; \
	fi; \
	wd=`LC_ALL=C date -d '$(CL_DATE)' +%a`; \
	if [ "$$wd" != "$(CL_WEEKDAY)" ]; then \
		echo "ERROR: ChangeLog says $(CL_WEEKDAY) $(CL_DATE), which is a $$wd" >&2; exit 1; \
	fi; \
	echo "OK: version $(VERSION), $(CL_WEEKDAY) $(CL_DATE)"

test:
	cd tests && $(PYTHON) -m unittest discover -p 'test_*.py' $(TESTFLAGS)

test-all:
	cd tests && RRCC_SLOW_TESTS=1 $(PYTHON) -m unittest discover -p 'test_*.py' $(TESTFLAGS)

install: check $(MAN1)
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

clean:
	rm -f $(MAN1)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
