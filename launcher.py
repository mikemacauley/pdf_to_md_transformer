"""PyInstaller entry point: GUI by default, CLI when arguments are given."""

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

if len(sys.argv) > 1:
    from pdf_to_md.cli import main
else:
    from pdf_to_md.gui import main

raise SystemExit(main())
