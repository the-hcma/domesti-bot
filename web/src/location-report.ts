/**
 * Report vs fix time for user locations (OwnTracks tst vs report/ingest time).
 */

export interface LocationReportFields {
  fix_at?: string;
  reported_at?: string;
  trigger?: string | null;
}

/** Primary ordering/display time: when the device report was built or ingested. */
export function locationReportedAtMs(loc: LocationReportFields): number {
  const raw = loc.reported_at ?? loc.fix_at;
  if (raw === undefined || raw === "") {
    return 0;
  }
  return Date.parse(raw);
}

/** Show fix-age note when report time is meaningfully later than GPS fix (tst). */
export const FIX_AGE_LABEL_THRESHOLD_SECONDS = 60;

export function formatTriggerLabel(trigger?: string | null): string | null {
  if (!trigger) {
    return null;
  }
  switch (trigger) {
    case "p":
      return "ping";
    case "r":
      return "reportLocation";
    case "u":
      return "manual";
    case "t":
      return "move timer";
    case "c":
    case "C":
      return "region";
    case "b":
      return "beacon";
    case "v":
      return "frequent locations";
    default:
      return trigger;
  }
}

export function formatDurationShort(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  const parts: string[] = [];
  if (hours > 0) {
    parts.push(`${hours}h`);
  }
  if (minutes > 0) {
    parts.push(`${minutes}m`);
  }
  if (hours === 0 && (minutes === 0 || secs > 0)) {
    parts.push(`${secs}s`);
  }
  return parts.join(" ") || "0s";
}

export function locationFixAgeSeconds(loc: LocationReportFields): number {
  const reportedMs = locationReportedAtMs(loc);
  const fixRaw = loc.fix_at ?? loc.reported_at;
  if (fixRaw === undefined || fixRaw === "") {
    return 0;
  }
  const fixMs = Date.parse(fixRaw);
  return Math.max(0, Math.floor((reportedMs - fixMs) / 1000));
}

/**
 * Human-readable note when fix (tst) predates the report.
 * @param fixTimeLabel - pre-formatted fix time string
 */
export function formatFixAgeNote(
  fixAgeSeconds: number,
  fixTimeLabel: string,
): string | null {
  if (fixAgeSeconds < FIX_AGE_LABEL_THRESHOLD_SECONDS) {
    return null;
  }
  return `Position from ${fixTimeLabel} (${formatDurationShort(fixAgeSeconds)} before this report)`;
}
