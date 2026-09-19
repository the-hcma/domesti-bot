---
description: All remote/network I/O must use explicit timeouts and bounded retries
alwaysApply: true
---

# Remote timeouts and retries

Any code that talks to a network peer (HTTP APIs, OAuth token endpoints, webhooks,
package indexes, `gh`/`curl` helpers, cloud SDKs, etc.) must not hang indefinitely
and must not retry without a bound. See repository-helpers#570.

## Timeouts (required)

- Set an **explicit timeout** on every remote call (connect + read, or a single
  overall deadline the library supports).
- Prefer the product's shared HTTP client / config timeout knob when one exists.
  Defaults must be finite and documented. Never leave library defaults that mean
  "wait forever."
- Agent / shell wrappers that hit the network should use the helpers'
  `--timeout` / `run_command … --timeout` patterns when available.
- For GitHub API work, honor rate-limit / secondary-limit waits
  (`GITHUB_API_RATE_LIMIT` / documented cooldown helpers) rather than spinning.

## Retries (required when retrying)

- Retries are for **transient** failures only (timeouts, 429, 502/503/504, reset).
- Cap attempts (small fixed `N`, typically 2–5) **or** a total retry budget.
- Back off between attempts (exponential with jitter when practical). Honor
  `Retry-After` when the peer sends it.
- Do **not** retry non-idempotent writes unless the API contract is safe
  (dedupe keys / explicit idempotency). Prefer fail fast on 4xx except 408/429.
- Log or surface a clear timeout/retry exhaustion error — never spin silently.

## Product-specific

Wire timeouts through the repo's shared HTTP client / config knob. Do not add
one-off unbounded clients beside that shared path.

## Anti-patterns

```python
# bad — no timeout
requests.get(url)
httpx.get(url)
client.execute()  # SDK call with no timeout / retry policy

# bad — unbounded retry
while True:
    try:
        return call()
    except TimeoutError:
        continue
```

```python
# good — timeout from config + bounded retries
http.timeout = httpx.Timeout(timeout_s, connect=min(30.0, timeout_s))
# or: run_command "label" --timeout 20 -- curl …
request_with_retries(call, max_attempts=3, retry_after_header=True)
```
