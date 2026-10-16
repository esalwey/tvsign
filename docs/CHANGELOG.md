# Changelog

Notable changes of each release, for the people who run tvsign. The release page on GitHub links the
Docker image and the Windows installer.

## 1.1.0

Upgrading keeps your apps, Apple accounts, pairings and settings. The database is changed in place after a
copy is made next to it (`tvsign.db.pre-migrate-<time>`, kept 30 days). Read "Changed" below once: a few
defaults are not the ones of 1.0.

### New

- **iPhone and iPad** (iOS / iPadOS 17 or later) are kept alive like an Apple TV. Pair once over USB (or
  import a pairing file made on another computer, which is the route for Docker), then everything runs
  over Wi-Fi; a phone that left the network is renewed as soon as it is back. **Experimental**: the USB
  pairing has not been checked on every device and iOS release yet.
- **Several Apple TVs**: the same app on each TV with its own status and expiry, a TV reached by an address
  you type when discovery is blocked, a renewal queue per TV, and a check that the app is really on the TV
  after an install and every 6 hours (an app that disappeared is reinstalled).
- **Control of updates, per app**: pin a version, install new versions automatically or only when you ask,
  release notes, "Install now", the previous version kept for a rollback (and an automatic fallback when a
  new version cannot be installed), a release that needs a newer tvOS held back until the TV is updated,
  and "Replace the IPA" for an uploaded app.
- **Notifications**: ntfy, a JSON webhook, Discord, Telegram and Gotify, each with the events it receives
  and a test button. Messages follow the language you chose and name the TV when you have several.
- **Renewals** run side by side, with a growing delay between retries, an "Action needed" state for what only
  you can fix (the card says what to do), the limits of a free Apple ID shown before they are hit, and an
  optional time window for non-urgent renewals.
- **Signing with an imported certificate** (advanced, optional, never needed): a `.p12` and a provisioning
  profile you already hold can replace the Apple ID for an app. tvsign does not recommend or contact any
  signing service.
- **Support**: a support bundle with the secrets masked, a paginated and downloadable journal, a diagnostics
  page that explains each failure, and English translations of the journal lines.
- **Backups** are checked before anything is replaced, and a restore is all or nothing.
- **Windows**: the PC is kept awake while a renewal runs (and an optional wake-up task), the installer offers
  to create the firewall rule (ticked by default; Windows asks for an administrator confirmation once),
  quitting lets a renewal finish, and a new version is announced in the window and by a notification.
- **Docker**: container logs rotate, the optional anisette server has its own volume, and the compose file
  reads `TZ`, `PUID` and `PGID` from a `.env` file.

### Changed

- When the Apple ID already holds 2 development certificates, tvsign no longer revokes the ones another
  tool created (AltStore, SideStore, Sideloadly, Xcode). Signing stops with an "Apple account full" message
  until you revoke one yourself or allow it in Settings › Certificates (`TVSIGN_CERT_REVOKE`). Before, it
  revoked any of them.
- A failed renewal is retried after 10 minutes, then 20, 40 and up to 45 (5 to 15 minutes when the TV is off
  or the anisette server is down), instead of a flat 45 (15). An error only you can fix is not retried blindly.
- Once a day tvsign asks GitHub whether a newer tvsign exists (one request, nothing else is sent). Turn it
  off in Settings › Check for tvsign updates or with `TVSIGN_UPDATE_CHECK=off`.
- An ntfy destination without a list of events receives all five kinds, including app updates.
- Notifications follow the language you chose. With "Automatic" they use the last language shown in the
  interface: English until you have opened it once, unless the server's `LANG` is French (they used to be
  French).
- The journal keeps the last 250 lines of each app; older ones are pruned at the first write after the upgrade.
- Each app keeps the previous IPA next to the current one: expect one more IPA of disk per app.
- New pairings are made under a name of their own (`tvsign-xxxxxxxx`); the ones you already have keep theirs.
- `docker-compose.yml` defaults `TZ` to UTC (it said Europe/Paris). If you replace your compose file with the
  new one, set `TZ` in a `.env` file next to it, or the time window and the journal use UTC.

### Going back to 1.0.x

1.0.x starts on a 1.1 database and keeps your apps, accounts and pairings, but it ignores the per-app
settings of 1.1: an app signed with an imported certificate is signed with the Apple ID again, and pinned
versions and "new versions only when I ask" are forgotten. Switch such apps to the Apple ID first, or put the
copy made before the upgrade back with tvsign stopped.

## 1.0.1

Apple refuses the data of Android anisette servers since 12 October 2026: sign-in uses Windows anisette
(`ani.jkcoxson.com`, fallback `ani2.stikstore.app`), explains the new refusals (`-80045`, HTTP 429 and 503,
SMS refused, `-22411`), waits instead of insisting, and sends a single alert per signed-out account. A
setting that names an Android server is replaced by the default.

## 1.0.0

First release: Docker image and Windows installer, Apple TV pairing and renewal, GitHub, link and uploaded
apps, backups, notifications through ntfy.
