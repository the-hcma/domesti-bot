// Locale wall-clock timestamps with the zone abbreviation and UTC in parentheses,
// plus an exact-ISO UTC tooltip for auditing. Never a bare UTC or bare local stamp.

export function formatLocalTimestamp(iso: string): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) {
    return iso;
  }
  const when = new Date(ms);
  const local = when.toLocaleString(undefined, { timeZoneName: "short" });
  const utc = `${when.toISOString().slice(0, 19).replace("T", " ")} UTC`;
  if (local.endsWith(" UTC")) {
    return local;
  }
  return `${local} (${utc})`;
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
