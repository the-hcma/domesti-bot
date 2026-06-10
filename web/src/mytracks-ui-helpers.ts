// Shared My Tracks instance URL helpers for Settings and Automations UI.

export function myTracksBaseUrl(domain: string): string {
  const trimmed = domain.trim().replace(/\/+$/, "");
  if (trimmed === "") {
    return "";
  }
  if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
    return trimmed;
  }
  return `https://${trimmed}`;
}

export function myTracksHostLabel(domain: string): string {
  const base = myTracksBaseUrl(domain);
  if (base === "") {
    return "";
  }
  try {
    return new URL(base).host;
  } catch {
    return domain.trim();
  }
}

export function createMyTracksInstanceLink(domain: string): HTMLAnchorElement | null {
  const href = myTracksBaseUrl(domain);
  if (href === "") {
    return null;
  }
  const link = document.createElement("a");
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.className = "rules-inline-link";
  link.textContent = myTracksHostLabel(domain);
  return link;
}

/** Append prose that names the My Tracks instance; link the host when domain is configured. */
export function appendMyTracksInstanceText(
  parent: HTMLElement,
  parts: {
    after?: string;
    before?: string;
    domain: string;
    fallbackLabel?: string;
  },
): void {
  const label = parts.fallbackLabel ?? "My Tracks";
  const link = createMyTracksInstanceLink(parts.domain);
  if (parts.before !== undefined && parts.before !== "") {
    parent.append(document.createTextNode(parts.before));
  }
  if (link !== null) {
    parent.append(link);
  } else {
    parent.append(document.createTextNode(label));
  }
  if (parts.after !== undefined && parts.after !== "") {
    parent.append(document.createTextNode(parts.after));
  }
}
