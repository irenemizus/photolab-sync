import sys
from pathlib import Path

# pytest's default import mode adds the test file's directory (tests/pyexiv2/)
# to sys.path, not the project root, so the root-level modules (metadata, place)
# can't be imported. Prepend the root so they resolve without installing the
# project. Not needed for `python -m pytest` from the root or an installed
# project; this makes the plain `pytest` script work too.
sys.path.insert(0, str(Path(__file__).resolve().parent))
