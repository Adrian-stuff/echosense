# Parent login

EchoSense has one device owner and explicitly invited caregiver accounts. The parent dashboard, original device console, APIs, static application pages, and live WebSocket require a signed-in session. Sensor processing and physical device controls continue running independently of web login.

## First account

1. Start EchoSense normally, for example:

   ```sh
   .venv/bin/echosense run --source sim --audio none --host 127.0.0.1 --port 8080 --no-browser
   ```

2. Open http://localhost:8080/login (or `/login` on your device's address).
3. Copy the device setup code printed in the terminal running EchoSense.
4. Choose a username and a password or passphrase of 15–128 characters. Confirm the password and create the account.

The setup code is generated each time the server starts and is shown only while no account exists. Creating the account requires the code; registration closes after the first account. No default password is installed. Keep the setup code private until registration is complete.

Username characters: 3–64 letters, numbers, or `. _ @ + -`. Usernames are case-sensitive. Passwords may include spaces and Unicode.

## Sign in, sign out, and change password

Sign in at `/login`. Sessions last up to 12 hours and survive server restarts while valid. **Sign out** is available in both the parent dashboard and device console. Sign-out revokes that browser session and closes its live streams, including other tabs sharing the cookie.

Use **Profile & privacy → Parent account** to change the password. Enter the current password and confirm the new one. Password changes revoke all browser sessions for that account; they do not sign out unrelated caregivers. Finish and save active parent training or recordings before changing passwords or signing out.

When a parent session expires, the app immediately locks private content and preserves notes and stopped recordings in the background. Sign in within that lock screen to recover without reloading. Device-managed practice pauses when its supervising connection or authentication ends; resuming requires an explicit action. The original console redirects to login.

## Forgotten password

Run this from a terminal on the EchoSense device, as the operating-system account that owns EchoSense data:

```sh
.venv/bin/echosense reset-password
```

The terminal prompts for the new password twice without echoing it. All sessions are revoked. There is no email reset or public recovery endpoint.

If using a custom settings location, specify the corresponding account database:

```sh
.venv/bin/echosense reset-password --auth-db /path/to/accounts.sqlite3
```

The global `--config` and `--profile` flags work before the `reset-password` command, as with other EchoSense commands.

## Caregiver invitations and permissions

In **Profile & privacy → Their trusted circle**, the owner chooses a username and access level and creates a one-time invitation code. Share it privately. The recipient chooses **Use a caregiver invitation** at `/login` and enters the invited username, code, and their own new password. Codes expire after 24 hours; generating another for the same unused username replaces its previous code.

| Account | Review live view/history and download | Record, annotate, run practice, restore | Change sensor/fitting/device settings | Invite/revoke accounts |
|---|---|---|---|---|
| Owner | Yes | Yes | Yes | Yes |
| Viewer | Yes | No | No | No |
| Trainer | Yes | Yes | No | No |
| Device manager (`technician`) | Yes | No | Yes | No |

The server checks these permissions on APIs and WebSocket commands. UI restrictions supplement the server checks. The owner can revoke or restore caregiver access. Revocation removes that caregiver's sessions, closes their live streams, and pauses any training they supervise. The owner account cannot be disabled through this interface.

Existing single-account databases are migrated to an owner account without changing its password or valid sessions. Session records are associated with their specific user. Terminal owner-password recovery revokes all sessions, including caregiver sessions.

## Account data and deployment

By default, `accounts.sqlite3` lives beside the active device settings file, normally `~/.echosense/accounts.sqlite3`. Set `ECHOSENSE_AUTH_DB` to override it. Back up this database securely: it contains password hashes and session records. It is created with owner-only file permissions. It is excluded from Git.

Credentials are salted and hashed with scrypt (N=2^14, r=8, p=5). Raw session tokens are not stored in SQLite; only SHA-256 digests are stored. Cookies are HttpOnly and SameSite=Strict. State-changing HTTP requests require the app's Origin and authenticated mutations also require a session-bound CSRF token. WebSocket upgrades verify both Origin and session. Live streams recheck expiry/revocation, and incoming commands revalidate the session. Authentication attempts are limited per source address and across the device.

The memory-constrained scrypt profile follows an option in [OWASP's password-storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html). Cookie and expiry choices follow [OWASP's session-management guidance](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).

HTTP on localhost supports local development. Use HTTPS for credentials and footage sent across a network. For HTTPS behind a reverse proxy, configure the exact external origin:

```sh
export ECHOSENSE_ORIGIN=https://echo.example
```

This makes the app require that Origin and issue Secure cookies, without trusting arbitrary forwarded headers. The proxy must support WebSocket upgrades. Keep the backend port private to the proxy. Rate limiting uses the actual peer address; when behind a proxy, clients share its rate limit.

The owner can invite caregivers with separate view/train/configure permissions. The system does not add internet remote access, email verification, or per-child cloud storage. Clips, child profiles, goals, and cached history live in the current browser; confirmed practice and observations are also persisted on the device. Logging out does not delete or encrypt browser-local records; use a trusted browser profile and device. A terminal user with access to the account database can reset the password, as with other local device administration.

## Checks

```sh
.venv/bin/python -m pytest -q
node tests/test_parent_ui.cjs
```

Authentication tests exercise real aiohttp HTTP and WebSocket handlers, including setup-code validation, route protection, cookies, cross-site requests, session revocation, password changes, expiry, HTTPS origin configuration, rate limits, restart persistence, and terminal recovery. Login/setup rendering was checked at desktop and mobile widths; no real parent account was created during browser checks.
