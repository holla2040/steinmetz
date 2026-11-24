"""``steinmetz`` — cross-platform task runner (the Makefile's targets, in Python).

    steinmetz read                   # connect + summarize the open design
    steinmetz run-command            # prove the write path (harmless WINDOW FIT)
    steinmetz selection              # show the current GROUP-selected parts
    steinmetz place [place.py flags] # place the selected parts, e.g. --only U1 --rotate 45
    steinmetz screenshot [PATH]      # snapshot the board to the Fusion scratch dir
    steinmetz validate [flags]       # numpy engine vs scalar spec (read-only)

Everything after the task name is passed to the underlying tool unchanged, so
``steinmetz place --help`` shows the placer's own options. Installed as a console
script by ``pip install -e .``; ``python src/steinmetz_cli.py <task>`` works too.
``read``, ``run-command`` and ``validate`` run the repo's ``examples/`` and
``scripts/`` files, so they need a source checkout (an editable install).
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# task -> (description, how to run it)
TASKS = {
    "read": ("Connect + summarize the open design", "examples/read_design.py"),
    "run-command": ("Prove the write path (harmless WINDOW FIT)", "examples/run_command.py"),
    "selection": ("Show the current GROUP-selected parts", "selection"),
    "place": ("Place the selected parts (flags pass through to src/place.py)", "place"),
    "screenshot": ("Snapshot the board to a PNG in the Fusion scratch dir", "screenshot"),
    "validate": ("Check the numpy placement engine against the scalar spec "
                 "(read-only)", "scripts/validate_placevec.py"),
}


def _usage() -> str:
    lines = ["usage: steinmetz <task> [args...]", "", "tasks:"]
    lines += [f"  {name:<12} {desc}" for name, (desc, _) in TASKS.items()]
    lines += ["", "Arguments after the task go to the tool, e.g. "
              "steinmetz place --only R9 R13 --rotate 45"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0
    task, rest = argv[0], argv[1:]
    if task not in TASKS:
        print(f"steinmetz: unknown task {task!r}\n\n{_usage()}", file=sys.stderr)
        return 2
    target = TASKS[task][1]
    if target.endswith(".py"):
        path = ROOT / target
        if not path.is_file():
            print(f"steinmetz: {target} not found under {ROOT} — '{task}' needs a "
                  "source checkout (pip install -e .)", file=sys.stderr)
            return 2
        src = str(ROOT / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        sys.argv = [str(path), *rest]
        runpy.run_path(str(path), run_name="__main__")
        return 0
    sys.argv = [f"steinmetz {task}", *rest]
    module = __import__(target)
    return module.main() or 0


if __name__ == "__main__":
    sys.exit(main())
