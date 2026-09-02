"""SQLite persistence for etoroNightwatch (DESIGN §3).

``schema.sql`` is the baseline schema, ``migrations`` is the forward-only
runner, and ``repo`` is the only module the rest of the app talks to.
"""

from .repo import Repo, connect, default_db_path, open_repo

__all__ = ["Repo", "connect", "default_db_path", "open_repo"]
