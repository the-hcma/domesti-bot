"""Pydantic models for the domesti HTTP API."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.cron_schedule import validate_schedule_cron_expression
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.presence_connection_type import normalize_presence_connection_type

RuleTriggerOut = Literal["edge_true", "scheduled"]

SecretsKeySourceOut = Literal["env", "file", "none"]
TailwindTokenSourceOut = Literal["cli", "env", "database", "none"]
VizioAuthSourceOut = Literal["cli", "env", "database", "none"]


class CompletionAliasesOut(BaseModel):
    """Device name fragments for Tab completion in remote CLI mode."""

    switch: list[str] = Field(default_factory=list)
    sonos: list[str] = Field(default_factory=list)
    tailwind: list[str] = Field(default_factory=list)
    all_device_labels: list[str] = Field(default_factory=list)


class ExecuteLineIn(BaseModel):
    """One REPL line (same syntax as ``domesti_bot_cli``)."""

    line: str = Field(..., min_length=1, description="Full line, e.g. ``turn-off Kitchen``")


class ExecuteLineOut(BaseModel):
    """Captured stdout/stderr from executing that line (plain text, no TTY colors)."""

    stdout: str = ""
    stderr: str = ""
    error: str | None = Field(
        default=None,
        description="Set when the line could not run (e.g. unknown command, API-only restriction).",
    )


class MetaOut(BaseModel):
    """Build identity for the running server process (landing-page tooltip)."""

    version: str = Field(..., description="``project.version`` from ``pyproject.toml``.")
    commit: str = Field(
        ...,
        description=(
            "Short git SHA (``git rev-parse --short=12``) when available, else "
            "``GITHUB_SHA`` / ``DOMESTI_GIT_COMMIT`` truncated to 12 hex chars, else "
            "``unknown``."
        ),
    )


class UISonosStreamFavoriteOut(BaseModel):
    """One configured Sonos radio stream favorite."""

    name: str = Field(..., description="Human-readable station label.")
    uri: str = Field(..., description="Direct HTTP(S) stream URI for ``play_uri``.")


class UIDeviceOut(BaseModel):
    """One tile on the landing page.

    Field semantics (the front-end renders the tile from these):

    * ``id``: stable per-family **canonical key** also used as
      ``ui_preferences.canonical_key`` (kasa → host IP, sonos →
      ``RINCON_…`` UID, tailwind → door identifier, future androidtv →
      Cast UUID). Pair with ``family_id`` for cross-family uniqueness.
    * ``family_id``: matches the parent :class:`UIFamilyOut.id`. Repeated
      here so the UI can flatten the structure when needed (e.g. building a
      "global off" set without re-walking the tree).
    * ``label``: ``preferred_label`` (display name when the user has set
      one via ``set-display-name`` in the CLI; otherwise the identifier).
    * ``kind``: ``"switch"`` (kasa, future androidtv), ``"speaker"``
      (sonos), or ``"door"`` (tailwind). The UI uses this to pick
      tile iconography and which action verb to render.
    * ``state``: family-specific cached state — ``"on"`` / ``"off"`` for
      switches; ``"playing"`` / ``"paused"`` for speakers; ``"open"`` /
      ``"closed"`` for doors. ``"unknown"`` covers transient cases (a
      Tailwind door reporting ``OPENING`` / ``CLOSING``, a Sonos zone
      we haven't polled yet) so the UI never has to crash on
      unexpected payloads.
    * ``exclude_from_global``: from the ``ui_preferences`` SQLite table.
      ``False`` (the default) means a global "turn off all" / "close all"
      action will operate on this device; ``True`` means it is skipped.
      Family-level bulk actions ignore this flag.
    * ``compact_icon``: stable key for compact mobile tile SVGs (``bulb``,
      ``outlet``, ``garage``, ``speaker``, …). Resolved server-side from
      label and, for Kasa, hardware model — not from TP-Link app rooms.
    """

    id: str = Field(..., description="Stable canonical key within the family.")
    family_id: str = Field(..., description="Parent family id (e.g. ``kasa``).")
    label: str = Field(..., description="Display name; falls back to ``id``.")
    kind: str = Field(..., description="``switch``, ``speaker``, or ``door``.")
    state: str = Field(..., description="``on``/``off`` (switch), ``playing``/``paused`` (speaker), or ``open``/``closed`` (door); ``unknown`` for transient.")
    compact_icon: str = Field(
        ...,
        description="Icon key for saturated compact tiles (e.g. ``bulb``, ``garage``).",
    )
    exclude_from_global: bool = Field(
        default=False,
        description="True → skip this device on global turn-off/close-all.",
    )
    stream_favorites: list[UISonosStreamFavoriteOut] = Field(
        default_factory=list,
        description=(
            "Configured radio streams for Sonos zones (empty for other families). "
            "Resume uses ``favorite_index`` into this list."
        ),
    )


class UIFamilyOut(BaseModel):
    """A row of tiles in the UI (one per device family).

    ``color`` is a CSS-compatible string (currently a hex literal) used as
    the tile background tint. Owned by the server so the same color renders
    consistently across the web UI, future native UI, and any embeds.
    Empty families are not emitted by ``GET /v1/ui/state`` (the user opted
    out via ``--no-tailwind`` etc., so there is nothing to render).
    """

    id: str = Field(..., description="Family slug (``kasa`` / ``sonos`` / ``tailwind`` / future ``androidtv``).")
    label: str = Field(..., description="Human-facing family name.")
    color: str = Field(..., description="CSS color (hex, e.g. ``#3B82F6``).")
    devices: list[UIDeviceOut] = Field(default_factory=list)


class UIStateOut(BaseModel):
    """Top-level payload for ``GET /v1/ui/state``.

    ``families`` is ordered for deterministic UI rendering: ``kasa``, then
    ``sonos``, ``vizio``, and ``tailwind`` (garage doors last). Empty
    families are omitted.
    """

    families: list[UIFamilyOut] = Field(default_factory=list)


class UIBulkActionOut(BaseModel):
    """Result of a *family-level* bulk action (kasa-all-off / tailwind-close-all).

    Family endpoints return device ids as plain strings because every entry
    is implicitly scoped to the URL's family. ``affected`` lists only
    devices that were not already in the target state (off / paused /
    closed) and were commanded. ``skipped`` is always empty in practice
    (family bulks ignore ``exclude_from_global``); kept in the signature
    so callers don't have to special-case the return shape.
    """

    affected: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class UIGlobalBulkActionItem(BaseModel):
    """One entry in ``UIGlobalBulkActionOut`` — needs ``family_id`` since the
    global bulk action spans multiple families."""

    family_id: str
    device_id: str


class UIGlobalBulkActionOut(BaseModel):
    """Result of ``POST /v1/ui/global/bulk-off``.

    Mixes kasa hosts, Sonos zone UIDs, and tailwind door ids; the
    ``family_id`` field disambiguates them. ``affected`` lists only
    devices that were commanded because they were not already off,
    paused, or closed. ``skipped`` collects every device with
    ``exclude_from_global=True``. Devices already in the target state
    are omitted from both lists.
    """

    affected: list[UIGlobalBulkActionItem] = Field(default_factory=list)
    skipped: list[UIGlobalBulkActionItem] = Field(default_factory=list)


class UIDeviceActionOut(BaseModel):
    """One refreshed :class:`UIDeviceOut` after a single-device action.

    The endpoint reads the device's cached state *after* the action so the
    UI can flip the toggle to its new position without re-fetching the
    full ``GET /v1/ui/state``.
    """

    device: UIDeviceOut


class UIPowerSetIn(BaseModel):
    """Body for ``POST /v1/ui/kasa/devices/{device_id}/toggle``."""

    on: bool = Field(..., description="``True`` → turn on; ``False`` → turn off.")


class UISonosSetIn(BaseModel):
    """Body for ``POST /v1/ui/sonos/zones/{device_id}/toggle``."""

    playing: bool = Field(
        ...,
        description="``True`` → resume (play); ``False`` → pause.",
    )
    favorite_index: int = Field(
        default=0,
        ge=0,
        description=(
            "When ``playing`` is ``True``, which configured stream favorite to "
            "play (``0`` = first entry in ``domesti-bot.config.json``)."
        ),
    )


class UIPreferenceIn(BaseModel):
    """Body for ``PUT /v1/ui/preferences/{family_id}/{device_id}``."""

    exclude_from_global: bool = Field(
        ...,
        description="``True`` excludes the device from any future global bulk action.",
    )


class UIPreferenceOut(BaseModel):
    """Confirmation echo of a write to ``ui_preferences``."""

    family_id: str
    device_id: str
    exclude_from_global: bool


class SmtpConfigIn(BaseModel):
    """SMTP settings payload (password optional on update)."""

    from_address: str = Field(..., min_length=1)
    host: str = Field(..., min_length=1)
    mail_domain: str = Field(..., min_length=1)
    password: str | None = Field(
        default=None,
        description="Null keeps the stored password on update.",
    )
    port: int = Field(..., ge=1, le=65535)
    username: str = Field(default="")


class SmtpConfigOut(BaseModel):
    """Stored SMTP settings without the password."""

    from_address: str
    host: str
    last_test_recipient: str | None
    mail_domain: str
    password_configured: bool
    port: int
    username: str


class SmtpTestEmailIn(SmtpConfigIn):
    """Transient SMTP settings plus a test recipient."""

    to_address: str = Field(..., min_length=1)


class SmtpTestEmailOut(BaseModel):
    """Result of a test email attempt."""

    message: str
    ok: bool


class TailwindTokenSetIn(BaseModel):
    """Body for ``PUT /v1/settings/tailwind-token`` (token is never returned)."""

    token: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="GoTailwind Local Control Key (six-digit code from the Tailwind dashboard).",
    )


class TailwindTokenSetOut(BaseModel):
    """Confirmation after persisting an encrypted Tailwind token."""

    configured: bool = Field(
        ...,
        description="True when a token is now available (env, CLI, or database).",
    )
    source: TailwindTokenSourceOut = Field(
        ...,
        description="Where the active token is read from after this request.",
    )
    restart_required: bool = Field(
        ...,
        description=(
            "True when the running server must restart (or rediscover) before "
            "GoTailwind doors appear if they were previously skipped."
        ),
    )


class TailwindTokenSettingsOut(BaseModel):
    """Tailwind credential status (no secret material)."""

    configured: bool = Field(
        ...,
        description="True when CLI, environment, or encrypted database provides a token.",
    )
    source: TailwindTokenSourceOut = Field(
        ...,
        description="Active source: ``cli`` → ``--tailwind-token``, ``env`` → ``TAILWIND_TOKEN``, ``database`` → encrypted SQLite row.",
    )
    secrets_key_configured: bool = Field(
        ...,
        description="True when a valid Fernet key is available (environment or ``domesti-bot.config.json``).",
    )
    secrets_key_source: SecretsKeySourceOut = Field(
        ...,
        description="``env`` → ``DOMESTI_BOT_SECRETS_KEY``; ``file`` → ``domesti-bot.config.json`` at repo root.",
    )
    stored_in_database: bool = Field(
        ...,
        description="True when an encrypted ``tailwind_token`` row exists (may be overridden by env/CLI).",
    )
    stored_token: str | None = Field(
        default=None,
        description=(
            "Decrypted token from the database row when present; ``None`` when "
            "nothing is stored or decryption is unavailable. Not the env/CLI override."
        ),
    )


class GeofenceOut(BaseModel):
    """Automation geofence definition."""

    center_lat: float
    center_lon: float
    enabled: bool
    geofence_id: str
    label: str
    owntracks_rid: str | None = None
    radius_m: int


class LocationHistoryRetentionIn(BaseModel):
    """Location-history retention policy for my-tracks pairing."""

    max_age_hours: float = Field(default=24.0, gt=0)
    min_keep_count: int = Field(default=20, ge=1)
    unlimited: bool = False


class LocationHistoryRetentionOut(BaseModel):
    """Effective location-history retention policy."""

    max_age_hours: float
    min_keep_count: int
    unlimited: bool


class MyTracksGeofencesSyncOut(BaseModel):
    """Result of a geofence sync pull from My Tracks."""

    geofence_count: int
    last_synced_at: str | None
    source: Literal["my-tracks"] = "my-tracks"


class MyTracksLocationUpdatesIn(BaseModel):
    """Body for ``PATCH /v1/settings/my-tracks/location-updates``."""

    accepted: bool
    password: str | None = None


class MyTracksLocationUpdatesOut(BaseModel):
    """Result of toggling whether domesti-bot accepts live location relays."""

    accepted: bool
    mytracks_location_updates_enabled: bool | None = None


class MyTracksPairIn(BaseModel):
    """Body for ``POST /v1/settings/my-tracks/pair``."""

    domain: str = Field(..., min_length=1)
    location_history_retention: LocationHistoryRetentionIn = Field(
        default_factory=LocationHistoryRetentionIn
    )
    password: str = Field(..., min_length=1)
    username: str = Field(..., min_length=1)


class MyTracksRelayKeySettingsOut(BaseModel):
    """Relay API key status for my-tracks webhook authentication."""

    configured: bool = Field(
        ...,
        description="True when an encrypted relay key row exists in the discovery database.",
    )
    stored_relay_key: str | None = Field(
        default=None,
        description="Decrypted relay key when stored; never returned when unpaired.",
    )


class MyTracksPairStatusOut(BaseModel):
    """Pairing status for domesti-bot ↔ my-tracks integration."""

    domain: str
    domesti_public_base_url: str | None = None
    last_pair_error: str | None = None
    last_verify_at: str | None = None
    last_verify_ok: bool | None = None
    location_history_retention: LocationHistoryRetentionOut
    location_updates_accepted: bool = True
    mytracks_location_updates_enabled: bool | None = None
    paired_at: str | None = None
    user_location_test_url: str | None = None
    user_location_update_url: str | None = None
    relay_key_configured: bool = False
    username: str


class MyTracksUsersSyncOut(BaseModel):
    """Result of a user roster sync pull from My Tracks."""

    last_synced_at: str | None
    user_count: int
    source: Literal["my-tracks"] = "my-tracks"
    webhook_ready: bool = True


class MyTracksSettingsIn(BaseModel):
    """Body for ``PUT /v1/settings/my-tracks``."""

    domain: str = Field(..., min_length=1)
    username: str = Field(..., min_length=1)


class MyTracksSettingsOut(BaseModel):
    """Stored My Tracks connection settings."""

    domain: str
    username: str


class MyTracksSyncIn(BaseModel):
    """Admin credentials for a one-shot sync request (password is never stored)."""

    password: str = Field(..., min_length=1)
    username: str | None = None


class UserOut(BaseModel):
    """Automation user roster row."""

    display_name: str
    enabled: bool
    first_name: str
    last_name: str
    tracking_device_label: str
    user_id: str


class UserLocationOut(BaseModel):
    """Latest known GPS location for a user."""

    accuracy_m: int | None
    connection_type: str | None = None
    lat: float
    lon: float
    received_at: str
    source: str | None = None


class LocationUpdateWebhookIn(BaseModel):
    """Live or test location-update payload from my-tracks."""

    accuracy_m: int | None = None
    connection_type: str | None = Field(
        default=None,
        max_length=1,
        description=(
            "OwnTracks conn: w=WiFi, m=mobile/cell, o=no network at publish "
            "(OwnTracks offline/queued waypoint — not “user away from home”)"
        ),
    )
    device_id: str | None = None
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    mqtt_user: str | None = None
    source: str | None = None
    timestamp: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)

    @field_validator("connection_type", mode="before")
    @classmethod
    def _normalize_connection_type(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(
                f"Expected str or None for connection_type, got {type(value).__name__}",
            )
        if value.strip() == "":
            return None
        normalized = normalize_presence_connection_type(value)
        if normalized is None:
            raise ValueError(
                f"Expected OwnTracks conn code w, m, or o, got {value!r}",
            )
        return normalized


class UserStatusOut(UserOut):
    """User roster row plus live presence fields."""

    age_seconds: int | None = None
    inside_geofence_ids: list[str] = Field(default_factory=list)
    last_location: UserLocationOut | None = None


class AfterLocalTimeCondition(BaseModel):
    type: Literal["after_local_time"]
    time_hhmm: str


class AfterSunsetCondition(BaseModel):
    type: Literal["after_sunset"]
    offset_minutes: int
    window_end: Literal["midnight"] | None = None


class AllConditionsCondition(BaseModel):
    type: Literal["all"]
    conditions: list["RuleConditionOut"]


class AnyConditionsCondition(BaseModel):
    type: Literal["any"]
    conditions: list["RuleConditionOut"]


class BeforeLocalTimeCondition(BaseModel):
    type: Literal["before_local_time"]
    time_hhmm: str


class BeforeSunriseCondition(BaseModel):
    type: Literal["before_sunrise"]
    offset_minutes: int
    window_start: Literal["midnight"] | None = None


class DaysOfWeekCondition(BaseModel):
    type: Literal["days_of_week"]
    days: list[int]


class RuleConditionDeviceRefOut(BaseModel):
    """One device reference inside a device-state rule condition."""

    device_id: str
    family_id: DeviceFamilyId


class DevicesAllOnCondition(BaseModel):
    type: Literal["devices_all_on"]
    devices: list[RuleConditionDeviceRefOut] = Field(min_length=1)


class DevicesAnyOnCondition(BaseModel):
    type: Literal["devices_any_on"]
    devices: list[RuleConditionDeviceRefOut] = Field(min_length=1)


class DevicesAnyOpenCondition(BaseModel):
    type: Literal["devices_any_open"]
    devices: list[RuleConditionDeviceRefOut] = Field(min_length=1)


class LocalTimeWindowCondition(BaseModel):
    type: Literal["local_time_window"]
    end_hhmm: str
    start_hhmm: str


class UsersInsideGeofenceCondition(BaseModel):
    type: Literal["users_inside_geofence"]
    geofence_id: str
    user_ids: list[str] = Field(min_length=1)


class UsersInsideGeofenceForSCondition(BaseModel):
    type: Literal["users_inside_geofence_for_s"]
    geofence_id: str
    min_inside_s: int = Field(ge=1)
    user_ids: list[str] = Field(min_length=1)


class UsersOutsideGeofenceCondition(BaseModel):
    type: Literal["users_outside_geofence"]
    geofence_id: str
    user_ids: list[str] = Field(min_length=1)


class UsersOutsideGeofenceForSCondition(BaseModel):
    type: Literal["users_outside_geofence_for_s"]
    geofence_id: str
    min_outside_s: int = Field(ge=1)
    user_ids: list[str] = Field(min_length=1)


RuleConditionOut = Annotated[
    AfterLocalTimeCondition
    | AfterSunsetCondition
    | AllConditionsCondition
    | AnyConditionsCondition
    | BeforeLocalTimeCondition
    | BeforeSunriseCondition
    | DaysOfWeekCondition
    | DevicesAllOnCondition
    | DevicesAnyOnCondition
    | DevicesAnyOpenCondition
    | LocalTimeWindowCondition
    | UsersInsideGeofenceCondition
    | UsersInsideGeofenceForSCondition
    | UsersOutsideGeofenceCondition
    | UsersOutsideGeofenceForSCondition,
    Field(discriminator="type"),
]


class RuleConditionsOut(BaseModel):
    all: list[RuleConditionOut]


class RuleConditionStatusOut(BaseModel):
    """One evaluated condition row for the Automations Status tab."""

    condition: RuleConditionOut
    detail: str
    label: str
    met: bool


class RuleDeviceActionOut(BaseModel):
    action: RuleDeviceActionType
    device_id: str
    family_id: DeviceFamilyId


class RuleOut(BaseModel):
    """One automation rule from ``automation-rules.json``."""

    accuracy_edge_grace_s: int | None = Field(
        default=None,
        ge=1,
        description=(
            "When a geofence edge matches but GPS accuracy fails "
            "``min_location_accuracy_m``, retry firing for this many seconds "
            "on subsequent location updates once accuracy improves."
        ),
    )
    conditions: RuleConditionsOut
    cooldown_s: int
    device_actions: list[RuleDeviceActionOut]
    enabled: bool
    fire_once_per_local_day: bool = Field(
        default=False,
        description=(
            "When true on a scheduled rule, fire at most once per local calendar day "
            "(timezone from settings_location)."
        ),
    )
    id: str
    label: str
    min_location_accuracy_m: int
    notification_emails: list[str] = Field(
        default_factory=list,
        description="Recipients when notify_on_fire is true.",
    )
    notify_on_fire: bool
    schedule_cron: str | None = Field(
        default=None,
        description=(
            "5-field cron expression (minute hour day month weekday). "
            "Required when trigger is scheduled; timezone from settings_location."
        ),
    )
    trigger: RuleTriggerOut

    @model_validator(mode="before")
    @classmethod
    def _migrate_notification_email(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "notification_emails" in data:
            return data
        legacy = data.get("notification_email")
        if legacy is None:
            return data
        migrated = dict(data)
        text = str(legacy).strip()
        migrated["notification_emails"] = [text] if text else []
        migrated.pop("notification_email", None)
        return migrated

    @model_validator(mode="after")
    def _validate_trigger_fields(self) -> Self:
        cron = (self.schedule_cron or "").strip()
        if self.trigger == "scheduled":
            validate_schedule_cron_expression(cron)
            self.schedule_cron = cron
            return self
        if cron != "":
            raise ValueError(
                "schedule_cron is only allowed when trigger is scheduled"
            )
        self.schedule_cron = None
        if self.fire_once_per_local_day:
            raise ValueError(
                "fire_once_per_local_day is only allowed when trigger is scheduled"
            )
        return self


def normalized_rule_notification_emails(rule: RuleOut) -> list[str]:
    """Return de-duplicated non-empty notification recipients for ``rule``."""
    seen: set[str] = set()
    recipients: list[str] = []
    for raw in rule.notification_emails:
        email = raw.strip()
        if email == "" or email in seen:
            continue
        seen.add(email)
        recipients.append(email)
    return recipients


class RuleReferenceIssueOut(BaseModel):
    """Broken reference from a rule definition to roster / geofence data."""

    detail: str
    kind: Literal[
        "discovery_pending",
        "missing_notification_email",
        "missing_smtp",
        "unknown_device",
        "unknown_geofence",
        "unknown_user",
    ]
    reference: str


class RuleStatusSummaryOut(BaseModel):
    """Per-rule status row for ``GET /v1/rules/status``."""

    condition_currently_true: bool
    conditions: list[RuleConditionStatusOut]
    enabled: bool
    id: str
    label: str
    last_error: str | None = None
    last_fired_at: str | None = None
    next_evaluate_at: str | None = None
    reference_issues: list[RuleReferenceIssueOut] = Field(default_factory=list)
    scheduled_detail: str | None = None
    trigger: RuleTriggerOut


class RulesValidationOut(BaseModel):
    """Cross-reference check for file-backed rules against persisted data."""

    rules: list["RuleValidationOut"]


class RuleValidationOut(BaseModel):
    """Reference issues for one automation rule."""

    id: str
    issues: list[RuleReferenceIssueOut]


class RulesEvaluatorOut(BaseModel):
    """Evaluator heartbeat timestamps."""

    last_run_at: str | None = None
    next_sun_check_at: str | None = None


class RulesStatusOut(BaseModel):
    """Full Automations Status tab payload."""

    evaluator: RulesEvaluatorOut
    geofences: list[GeofenceOut]
    users: list[UserStatusOut]
    rules: list[RuleStatusSummaryOut]
    sun: "RulesSunOut"


class RulesSunOut(BaseModel):
    """Sunrise/sunset row for astronomical conditions."""

    is_dark: bool
    sunrise_at: str
    sunset_at: str


class VizioAuthTokenSetIn(BaseModel):
    """Body for ``PUT /v1/settings/vizio/tvs/{device_id}/auth``."""

    token: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="SmartCast auth token from a completed pairing session.",
    )


class VizioAuthTokenSetOut(BaseModel):
    """Confirmation after persisting an encrypted per-TV auth token."""

    configured: bool
    device_id: str
    restart_required: bool = Field(
        ...,
        description=(
            "True when the running server could not hot-reload the Vizio "
            "manager after saving the token."
        ),
    )


class VizioPairBeginIn(BaseModel):
    """Body for ``POST /v1/settings/vizio/pair/begin``."""

    host: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="TV host or ``HOST:PORT`` (default port 7345).",
    )


class VizioPairBeginOut(BaseModel):
    """Pairing challenge returned after ``pairing/start`` succeeds."""

    device_id: str = Field(..., description="Stable id (normalized MAC when known).")
    challenge_type: int
    pairing_req_token: int


class VizioPairCancelIn(BaseModel):
    """Body for ``POST /v1/settings/vizio/pair/cancel``."""

    device_id: str = Field(..., min_length=1, max_length=128)
    challenge_type: int
    pairing_req_token: int


class VizioPairCompleteIn(BaseModel):
    """Body for ``POST /v1/settings/vizio/pair/complete``."""

    device_id: str = Field(..., min_length=1, max_length=128)
    pin: str = Field(..., min_length=4, max_length=8)
    challenge_type: int
    pairing_req_token: int


class VizioPairCompleteOut(BaseModel):
    """Confirmation after persisting an encrypted per-TV auth token."""

    configured: bool
    device_id: str
    restart_required: bool = Field(
        ...,
        description=(
            "True when the running server could not hot-reload the Vizio "
            "manager after pairing."
        ),
    )


class VizioTvSettingsOut(BaseModel):
    """One known Vizio TV row for the settings UI."""

    device_id: str = Field(
        ...,
        description="Stable id (normalized MAC when known; legacy host or host:port otherwise).",
    )
    mac: str | None = Field(
        default=None,
        description="Normalized MAC address when resolved; used to locate the TV after DHCP changes.",
    )
    host: str
    port: int
    display_name: str | None = None
    auth_configured: bool
    auth_source: VizioAuthSourceOut
    stored_token: str | None = Field(
        default=None,
        description=(
            "Decrypted per-TV token from the database when auth_source is "
            "database; null when auth comes from CLI/env or no DB row exists."
        ),
    )


class VizioTvsSettingsOut(BaseModel):
    """Known Vizio TVs and Fernet key status for the settings UI."""

    secrets_key_configured: bool = Field(
        ...,
        description="True when a valid Fernet key is available for encrypted storage.",
    )
    secrets_key_source: SecretsKeySourceOut = Field(
        ...,
        description="``env`` → ``DOMESTI_BOT_SECRETS_KEY``; ``file`` → ``domesti-bot.config.json``.",
    )
    tvs: list[VizioTvSettingsOut] = Field(default_factory=list)


class SettingsLocationOut(BaseModel):
    """Home coordinates for astronomical conditions."""

    home_label: str | None = None
    lat: float
    lon: float
    timezone: str
    wifi_home_geofence_id: str | None = Field(
        default=None,
        description=(
            "When set, low-accuracy OwnTracks WiFi (conn=w) reports whose coordinates "
            "lie within this geofence radius plus 20% slack reconcile home presence "
            "without firing an enter edge. When unset, geofences containing settings "
            "lat/lon are used."
        ),
    )
    wifi_home_presence_enabled: bool = Field(
        default=True,
        description=(
            "When true, low-accuracy WiFi fixes within home geofence radius + 20% "
            "sync inside state without low-accuracy GPS enter/leave edges."
        ),
    )


AllConditionsCondition.model_rebuild()
AnyConditionsCondition.model_rebuild()
RuleConditionsOut.model_rebuild()
RuleConditionStatusOut.model_rebuild()
RuleOut.model_rebuild()
RulesStatusOut.model_rebuild()
