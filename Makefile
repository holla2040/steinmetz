# Steinmetz task shortcuts. These are thin aliases — the real work needs a live
# Fusion session + netsh port-forward (see docs/fusion-bridge.md). Every tool
# takes flags; to pass them, call the script directly, e.g.
#   .venv/bin/python src/place.py --only U1 --clearance 0.2
# The one exception: `make place ROT=45` forwards the rotation step
# (src/place.py --rotate 45). Anything beyond that, call the script directly.
#
# PY defaults to the project venv so targets work without activating it first.
PY ?= .venv/bin/python

.PHONY: help setup read run-command selection place screenshot

help:            ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sort | sed -E 's/:.*## /\t/'

setup:           ## Create .venv and install the package (editable)
	python -m venv .venv && .venv/bin/pip install -e .

read:            ## Connect + summarize the open design
	$(PY) examples/read_design.py

run-command:     ## Prove the write path (harmless WINDOW FIT)
	$(PY) examples/run_command.py

selection:       ## Show the current GROUP-selected parts
	$(PY) src/selection.py

place:           ## Place the selected parts (ROT=DEG sets rotation step; writes MOVEs, then verifies)
	$(PY) src/place.py $(if $(ROT),--rotate $(ROT))

screenshot:      ## Snapshot the board to C:\tmp (read at /mnt/c/tmp)
	$(PY) src/screenshot.py
