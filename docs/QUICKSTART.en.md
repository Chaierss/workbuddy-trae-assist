# Get started in 3 minutes

From download to your first successful account switch. No terminal, no admin rights.

> Windows 10 / 11 (x64), plus Linux x64 (AppImage / deb; every distro except CentOS 7 —
> see below). This is an unofficial third-party tool — read the
> [disclaimer](policies/DISCLAIMER.md) first.

---

## 0. Check the prerequisites (~20 seconds)

- Windows 10 1809+ or Windows 11, 64-bit; or Linux x64
  (Ubuntu 20.04+ / Debian 11+ / CentOS 8+). **CentOS 7 (glibc 2.17) cannot run the
  desktop build** — run the backend from source instead, see
  [desktop notes](../desktop/README.md#centos-用户先读这一段).
- At least one client installed and signed in: WorkBuddy (China) or Trae (China).
- Switching restarts the target client: **quit it yourself first**. The tool refuses to
  write while the client is running — that is a safeguard, not a bug.

---

## 1. Download and install (~40 seconds)

1. Get the latest build from
   [Releases](https://github.com/Chaierss/workbuddy-trae-assist/releases):
   - Windows: `chaierww-Setup-<version>.exe` — run it. It installs per-user without admin
     rights and shows the licence highlights and disclaimer first.
   - Linux: `chaierww-x64.AppImage` or `chaierww-x64.deb`. `chmod +x` the AppImage first;
     without FUSE run it as `APPIMAGE_EXTRACT_AND_RUN=1 ./chaierww-x64.AppImage`. On
     Debian / Ubuntu install the deb with `sudo apt install ./chaierww-x64.deb`.
2. Windows SmartScreen warns once (the installer is not code-signed): click
   "More info → Run anyway".

The app lives in the system tray; closing the window does not quit it (Linux needs
`libappindicator` / `gir1.2-appindicator3` for the tray).

---

## 2. Add accounts by QR code (~40 seconds)

Open the app → **Accounts** → **WorkBuddy QR** (or **Trae QR**) → scan with the client.

Already signed in somewhere? Two shortcuts:

- **Add current client account**: the overview page offers it when the client is signed in.
- **JSON import**: drop the session file into the import box; the platform is detected
  automatically. A `chaierww-accounts-*.json` bundle exported by this tool restores as a whole.

Seeing an account in the pool means step 2 is done.

---

## 3. Preview, then switch (~30 seconds)

1. Click **Switch** on the target account. The first time, a read-only preview shows how
   many sessions will migrate, which config files change, and what is unsupported.
2. Confirm with **Continue**. The tool backs up first, writes atomically, then verifies;
   any failed step rolls back to the pre-switch state.
3. On success the client restarts into the target account. The first successful switch shows a
   one-time "what next" card: enable daily check-in, issue a gateway key, or dismiss it.

**Changed your mind?** A backup was made before writing — restore it in
**Settings → Backup**.

---

## 4. Common failures

| Symptom | Cause | Fix |
|:--|:--|:--|
| SmartScreen blocks the installer | No code signature | "More info → Run anyway" |
| Switch button disabled: client is running | The tool never writes to a running client | Quit the client and retry |
| Client does not start after switching | Wrong client path, or the client is updating | Check the login directory in **Settings → Environment**; launch the client manually |
| "Sign in again" badge | The upstream returned 401; credentials expired | Re-scan that account; the failure counter resets automatically |
| "Incomplete credentials" | QR sign-in gives no plaintext refresh token (the platform does not expose one) | Switching still works; re-scan once the token expires — not a defect |
| Port already in use | Local API port conflict | Free the port, or check the service URL in **Settings → Environment** |
| Credits show "unavailable" | The credential lacks the fields needed to query credits | Expected fallback; switching and activities still work |

---

## 5. What else is there

- **Activities**: the official catalogue lists rules and rewards; automatable ones run in one
  click, manual ones say exactly where they get stuck.
- **Usage**: requests and credits grouped by model and by day, so you can see which account is
  running low.
- **Gateway**: expose account quota as OpenAI / Anthropic-compatible endpoints for other local
  AI tools.
- **Settings → Backup / Environment**: daily backups, session sync, and local metrics.

---

## Where your data lives

Everything sits in `~/.chaierww` (override with `CHAIERWW_HOME`): the SQLite database, encrypted
credentials, backups, and redacted logs. **No cloud, no relay.** Copy that folder to migrate a
machine.

Local experience data (anonymous counters such as "first successful switch rate") is written
**only to this machine** and is off-switchable, exportable, and clearable in
**Settings → Environment → Experience data**. See
[local experience data](policies/TELEMETRY.md).

---

中文版：[QUICKSTART.md](QUICKSTART.md) · Changelog: [CHANGELOG.md](../CHANGELOG.md) ·
Security model: [SECURITY.md](../SECURITY.md)
