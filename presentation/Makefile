# The slides: slides.md through pandoc, with the look of template.pptx.
PYTHON ?= python3
PANDOC ?= pandoc

BUILD := build
REFERENCE := $(BUILD)/reference.pptx
DECK := $(BUILD)/beck.pptx

# The suites make open looks for, the most faithful to .pptx first; make open OFFICE=libreoffice picks one.
OFFICE ?= $(firstword $(foreach suite,onlyoffice-desktopeditors libreoffice soffice,$(if $(shell command -v $(suite)),$(suite))))

.PHONY: all help check open install-onlyoffice install-libreoffice clean
.DELETE_ON_ERROR:

all: $(DECK)

help:
	@echo "make        the slides, in $(DECK)"
	@echo "make check  the slides, then their layouts, notes and text that must fit its box"
	@echo "make open   the slides, opened in OnlyOffice or LibreOffice, whichever is installed"
	@echo "make install-onlyoffice   OnlyOffice, on Arch Linux: from the repositories, else from the AUR"
	@echo "make install-libreoffice  LibreOffice, on Arch Linux and Ubuntu"
	@echo "make clean  remove $(BUILD)/"

$(DECK): slides.md slides.lua $(REFERENCE)
	$(PANDOC) $< --from markdown --slide-level 2 --lua-filter slides.lua --fail-if-warnings --reference-doc $(REFERENCE) --output $@

$(REFERENCE): template.pptx adapt_template.py | $(BUILD)
	$(PYTHON) adapt_template.py $< $@

$(BUILD):
	mkdir -p $@

check: $(DECK)
	$(PYTHON) check_deck.py slides.md $(REFERENCE) $(DECK)

open: $(DECK)
	@test -n "$(OFFICE)" || { echo "No office suite: make install-onlyoffice or make install-libreoffice" >&2; exit 1; }
	@command -v "$(OFFICE)" >/dev/null || { echo "$(OFFICE): not installed" >&2; exit 1; }
	@echo "$(DECK): opening in $(OFFICE)"
	@setsid -f "$(OFFICE)" "$(abspath $(DECK))" >/dev/null 2>&1

install-onlyoffice:
	@command -v pacman >/dev/null || { echo "Unsupported distribution: install OnlyOffice by hand (https://www.onlyoffice.com)." >&2; exit 1; }
	@if pacman -Si onlyoffice-bin >/dev/null 2>&1; then sudo pacman -S --needed onlyoffice-bin; \
	elif command -v paru >/dev/null; then paru -S --needed onlyoffice-bin; \
	elif command -v yay >/dev/null; then yay -S --needed onlyoffice-bin; \
	else echo "onlyoffice-bin is in the AUR: install paru or yay first." >&2; exit 1; fi

install-libreoffice:
	@. /etc/os-release; case "$$ID $$ID_LIKE" in \
		*arch*) sudo pacman -S --needed libreoffice-fresh ;; \
		*ubuntu*|*debian*) sudo apt-get update && sudo apt-get install libreoffice-impress ;; \
		*) echo "Unsupported distribution: install LibreOffice by hand." >&2; exit 1 ;; \
	esac

clean:
	rm -rf $(BUILD)
