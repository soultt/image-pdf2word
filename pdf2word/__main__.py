"""Entry point for `python -m pdf2word`."""

import sys

from .cli import main


def run() -> None:
    """Dispatch to the GUI when requested, otherwise to the CLI."""
    if len(sys.argv) > 1 and sys.argv[1] == "gui":
        from .gui import main as gui_main

        sys.argv.pop(1)
        gui_main()
        return
    main()


if __name__ == "__main__":
    run()
