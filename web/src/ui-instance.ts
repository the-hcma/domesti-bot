// Derive instance hostname / origin from the browser location bar.

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1", "[::1]", "localhost"]);

/** Hostname from ``window.location``; empty on loopback (operator types a real domain). */
export function defaultMailDomainFromUi(): string {
  const hostname = window.location.hostname.trim().toLowerCase();
  if (hostname === "" || LOOPBACK_HOSTS.has(hostname)) {
    return "";
  }
  return hostname;
}
