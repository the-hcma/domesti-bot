// Locale wall-clock timestamps with UTC tooltips for auditing.

export function formatLocalTimestamp(iso: string): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) {
    return iso;
  }
  return new Date(ms).toLocaleString();
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
