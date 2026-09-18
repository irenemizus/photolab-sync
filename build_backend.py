"""PEP 517 backend that builds the vendored pyexiv2, then delegates to setuptools.

Install with build isolation disabled so pyexiv2 lands in the active venv:
    pip install -e . --no-build-isolation
"""

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _ensure_setuptools():
    try:
        import setuptools.build_meta  # noqa: F401
    except ImportError:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "setuptools"],
            check=True,
        )


def _build_pyexiv2():
    subprocess.run(
        [sys.executable, str(_ROOT / "third-party" / "build.py")],
        check=True,
    )


def get_requires_for_build_wheel(config_settings=None):
    return ["setuptools>=61"]


def get_requires_for_build_editable(config_settings=None):
    return ["setuptools>=61"]


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    _ensure_setuptools()
    import setuptools.build_meta as meta

    return meta.prepare_metadata_for_build_wheel(metadata_directory, config_settings)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    _build_pyexiv2()
    _ensure_setuptools()
    import setuptools.build_meta as meta

    return meta.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    _build_pyexiv2()
    _ensure_setuptools()
    import setuptools.build_meta as meta

    return meta.build_editable(wheel_directory, config_settings, metadata_directory)
