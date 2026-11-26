// Locale wall-clock timestamps with the zone abbreviation and UTC in parentheses,
// plus an exact-ISO UTC tooltip for auditing. Never a bare UTC or bare local stamp.

// IANA identifiers that are UTC itself. Compared against the formatter's resolved zone, never
// against the localized text, because some locales put the zone name before the clock.
const UTC_ZONE_IDS = new Set(["UTC", "Etc/UTC", "Etc/UCT", "Etc/Universal", "Etc/Zulu", "Etc/GMT", "GMT"]);

/**
 * Local time with the zone label and the same instant in UTC in parentheses. `locale` defaults to
 * the viewer's locale; pass one only to make output reproducible (tests).
 */
export function formatLocalTimestamp(iso: string, locale?: string | string[]): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) {
    return iso;
  }
  const when = new Date(ms);
  const options: Intl.DateTimeFormatOptions = {
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    month: "numeric",
    second: "2-digit",
    timeZoneName: "short",
    year: "numeric",
  };
  const formatter = new Intl.DateTimeFormat(locale, options);
  const local = formatter.format(when);
  if (UTC_ZONE_IDS.has(formatter.resolvedOptions().timeZone)) {
    return local;
  }
  const utc = `${when.toISOString().slice(0, 19).replace("T", " ")} UTC`;
  return `${local} (${utc})`;
}

/** Same shape as {@link formatLocalTimestamp}, from a Unix epoch in seconds. */
export function formatLocalTimestampFromEpoch(epochSeconds: number, locale?: string | string[]): string {
  const ms = epochSeconds * 1000;
  if (!Number.isFinite(ms) || Number.isNaN(new Date(ms).getTime())) {
    return String(epochSeconds);
  }
  return formatLocalTimestamp(new Date(ms).toISOString(), locale);
}

export function formatUtcTimestampTitle(iso: string): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) {
    return `UTC: ${iso}`;
  }
  return `UTC: ${new Date(ms).toISOString()}`;
}

export function createAuditedTimeElement(iso: string): HTMLTimeElement {
  const el = document.createElement("time");
  el.dateTime = iso;
  el.textContent = formatLocalTimestamp(iso);
  el.title = formatUtcTimestampTitle(iso);
  return el;
}

export function setAuditedTimestampLine(
  parent: HTMLElement,
  parts: { iso: string; prefix: string; suffix?: string },
): void {
  parent.replaceChildren(
    document.createTextNode(parts.prefix),
    createAuditedTimeElement(parts.iso),
  );
  if (parts.suffix !== undefined && parts.suffix !== "") {
    parent.append(document.createTextNode(parts.suffix));
  }
}
