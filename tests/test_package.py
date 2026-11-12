"""The package version: the module constant and pyproject.toml must not drift apart."""

from importlib.metadata import version

import passing_notes


def test_module_version_is_the_installed_distribution_version() -> None:
    """``uv run`` installs the project from pyproject.toml, so the distribution metadata
    carries pyproject's version; ``--version`` prints ``__version__``."""
    assert passing_notes.__version__ == version("passing-notes")
