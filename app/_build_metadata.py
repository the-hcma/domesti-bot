"""Optional release-time stamps baked into wheels, containers, or sdists.

``scripts/embed_build_metadata.py`` overwrites this file before packaging so
``GET /v1/meta`` and OpenAPI still report version and commit when ``.git`` is
absent (for example after ``pip install`` from PyPI). Empty strings mean
"unset" and :mod:`app.build_info` falls back to ``importlib.metadata``, then
``pyproject.toml``, then ``git``.
"""

from __future__ import annotations

EMBEDDED_COMMIT: str = ""
EMBEDDED_VERSION: str = ""
