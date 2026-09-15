"""Process entrypoint for `python -m opencode_tools`; delegates to `cli.main`."""

from __future__ import annotations

import sys

from opencode_tools.cli import main

if __name__ == "__main__":
    sys.exit(main())
