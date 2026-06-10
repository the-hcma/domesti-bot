"""ORM models for the discovery SQLite database."""

from __future__ import annotations

from sqlalchemy import Float, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AndroidTvDiscoveredHost(Base):
    __tablename__ = "androidtv_discovered_hosts"

    host: Mapped[str] = mapped_column(String, primary_key=True)
    port: Mapped[int] = mapped_column(Integer, primary_key=True)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)
    friendly_name: Mapped[str | None] = mapped_column(String, nullable=True)
    uuid: Mapped[str | None] = mapped_column(String, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String, nullable=True)


class AppSecret(Base):
    __tablename__ = "app_secrets"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class DeviceDisplayName(Base):
    __tablename__ = "device_display_names"

    backend: Mapped[str] = mapped_column(String, primary_key=True)
    canonical_key: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class KasaDiscoveredDevice(Base):
    __tablename__ = "kasa_discovered_devices"

    host: Mapped[str] = mapped_column(String, primary_key=True)
    alias: Mapped[str | None] = mapped_column(String, nullable=True)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class MyTracksSettings(Base):
    __tablename__ = "mytracks_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String, nullable=False)
    username: Mapped[str] = mapped_column(String, nullable=False, default="")
    last_geofences_sync_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_participants_sync_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class RuleGeofence(Base):
    __tablename__ = "rule_geofences"

    geofence_id: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)
    center_lat: Mapped[float] = mapped_column(Float, nullable=False)
    center_lon: Mapped[float] = mapped_column(Float, nullable=False)
    radius_m: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    owntracks_rid: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class RuleParticipant(Base):
    __tablename__ = "rule_participants"

    participant_id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    tracking_device_label: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class RuleParticipantLastFix(Base):
    __tablename__ = "rule_participant_last_fix"

    participant_id: Mapped[str] = mapped_column(String, primary_key=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    received_at: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class SonosKnownZone(Base):
    __tablename__ = "sonos_known_zones"

    uuid: Mapped[str] = mapped_column(String, primary_key=True)
    host: Mapped[str] = mapped_column(String, nullable=False)
    zone_name: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class SmtpSettings(Base):
    __tablename__ = "smtp_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str] = mapped_column(String, nullable=False, default="")
    mail_domain: Mapped[str] = mapped_column(String, nullable=False)
    from_address: Mapped[str] = mapped_column(String, nullable=False)
    last_test_recipient: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class TailwindLastHost(Base):
    __tablename__ = "tailwind_last_host"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class UiPreference(Base):
    __tablename__ = "ui_preferences"

    backend: Mapped[str] = mapped_column(String, primary_key=True)
    canonical_key: Mapped[str] = mapped_column(String, primary_key=True)
    exclude_from_global: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)
