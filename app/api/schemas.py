"""Pydantic models for the domesti HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExecuteLineIn(BaseModel):
    """One REPL line (same syntax as ``device_manager_cli``)."""

    line: str = Field(..., min_length=1, description="Full line, e.g. ``turn-off Kitchen``")


class ExecuteLineOut(BaseModel):
    """Captured stdout/stderr from executing that line (plain text, no TTY colors)."""

    stdout: str = ""
    stderr: str = ""
    error: str | None = Field(
        default=None,
        description="Set when the line could not run (e.g. unknown command, API-only restriction).",
    )


class CompletionAliasesOut(BaseModel):
    """Device name fragments for Tab completion in remote CLI mode."""

    switch: list[str] = Field(default_factory=list)
    sonos: list[str] = Field(default_factory=list)
    tailwind: list[str] = Field(default_factory=list)
    all_device_labels: list[str] = Field(default_factory=list)


class UIDeviceOut(BaseModel):
    """One tile on the landing page.

    Field semantics (the front-end renders the tile from these):

    * ``id``: stable per-family **canonical key** also used as
      ``ui_preferences.canonical_key`` (kasa → host IP, tailwind → door
      identifier, future androidtv → Cast UUID, future sonos → ``RINCON_…``).
      Pair with ``family_id`` for cross-family uniqueness.
    * ``family_id``: matches the parent :class:`UIFamilyOut.id`. Repeated
      here so the UI can flatten the structure when needed (e.g. building a
      "global off" set without re-walking the tree).
    * ``label``: ``preferred_label`` (display name when the user has set
      one via ``set-display-name`` in the CLI; otherwise the identifier).
    * ``kind``: ``"switch"`` (kasa, future androidtv) or ``"door"``
      (tailwind). The UI uses this to pick toggle iconography.
    * ``state``: family-specific cached state — ``"on"`` / ``"off"`` for
      switches; ``"open"`` / ``"closed"`` for doors. ``"unknown"`` covers
      transient cases (e.g. a Tailwind door reporting ``OPENING`` /
      ``CLOSING`` rather than a settled position) so the UI never has to
      crash on unexpected payloads.
    * ``exclude_from_global``: from the ``ui_preferences`` SQLite table.
      ``False`` (the default) means a global "turn off all" / "close all"
      action will operate on this device; ``True`` means it is skipped.
      Family-level bulk actions ignore this flag.
    """

    id: str = Field(..., description="Stable canonical key within the family.")
    family_id: str = Field(..., description="Parent family id (e.g. ``kasa``).")
    label: str = Field(..., description="Display name; falls back to ``id``.")
    kind: str = Field(..., description="``switch`` or ``door``.")
    state: str = Field(..., description="``on``/``off`` (switch) or ``open``/``closed`` (door); ``unknown`` for transient.")
    exclude_from_global: bool = Field(
        default=False,
        description="True → skip this device on global turn-off/close-all.",
    )


class UIFamilyOut(BaseModel):
    """A row of tiles in the UI (one per device family).

    ``color`` is a CSS-compatible string (currently a hex literal) used as
    the tile background tint. Owned by the server so the same color renders
    consistently across the web UI, future native UI, and any embeds.
    Empty families are not emitted by ``GET /v1/ui/state`` (the user opted
    out via ``--no-tailwind`` etc., so there is nothing to render).
    """

    id: str = Field(..., description="Family slug (``kasa`` / ``tailwind`` / future ``sonos`` / ``androidtv``).")
    label: str = Field(..., description="Human-facing family name.")
    color: str = Field(..., description="CSS color (hex, e.g. ``#3B82F6``).")
    devices: list[UIDeviceOut] = Field(default_factory=list)


class UIStateOut(BaseModel):
    """Top-level payload for ``GET /v1/ui/state``.

    ``families`` is ordered for deterministic UI rendering: alphabetical by
    family ``id`` (currently ``kasa``, ``tailwind``). Future families slot
    into the same order without front-end changes.
    """

    families: list[UIFamilyOut] = Field(default_factory=list)
