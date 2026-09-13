"""Pytest bootstrap: Lambda packages `lambda/` as its import root.

Insert the repo's `lambda/` dir into `sys.path` so tests can
`from common.envelope import ...` exactly as deployed code does.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lambda"))
