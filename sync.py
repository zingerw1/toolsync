#!/usr/bin/env python3
"""toolsync launcher.

The implementation now lives in the ``toolsync/`` package next to this file
(see its ``__init__`` for the module map). This shim keeps the existing
``~/.local/bin/toolsync`` symlink — and any other symlink on PATH — working;
``python -m toolsync`` is equivalent.
"""

import os
import sys

# Resolve symlinks (realpath, not abspath) so the sibling `toolsync/` package is
# found when this launcher is reached through a symlink on PATH. Inserting this
# directory at the front makes the real package win over any stray same-named
# module elsewhere on sys.path.
_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)

from toolsync.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
