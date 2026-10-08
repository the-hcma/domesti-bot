# Rotating the Fernet master key

The master key encrypts the `app_secrets` table (see `docs/AGENTS.md`). Replacing it alone makes every stored secret undecryptable, so rotate in three steps:

1. Put the new key **in front** of the old one. `DOMESTI_BOT_SECRETS_KEY` and `domesti_secrets_key` accept several keys separated by commas (the JSON file also accepts a list), newest first. The first key encrypts, every key can decrypt, so nothing breaks while both are listed. Restart **every** process that uses the key (the server and any open REPL), because each reads the key list once at start; a process still on the old list keeps writing under the old key and would report rotation as finished when it is not.
2. In the REPL run `rotate-secrets` (`rotate-secrets --check` previews the count). It re-encrypts every stored secret under the newest key in one transaction and prints the count, never a value. If a row cannot be decrypted by any listed key, nothing changes and the row names are listed; add the key that wrote it, or use `--skip-undecryptable` and re-enter that secret in Settings.
3. Check `GET /v1/settings/secrets-key` (or `rotate-secrets --check`) from a freshly restarted process: when `rows_on_older_generation` and `rows_unreadable` are both `0`, remove the old key from the list and restart every process again.

Generate the new key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Do not use `setup-secrets` for this: it writes a single key over `domesti-bot.config.json`, which would drop the old key before the rotation has re-encrypted anything. Keep the old key in the list until `rotate-secrets` has succeeded and the status shows nothing left on it.
