"""etoroNightwatch — pure-Python trading core.

This package is the Chaquopy Python source set for the Android app, and it is
also the package the desktop forward test and unit tests import.  It therefore
MUST remain importable on plain CPython 3.13: no Android-only imports anywhere
in this tree.  Anything host-specific lives behind the ``Host`` interface in
:mod:`nightwatch.host`.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
