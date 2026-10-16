<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/assets/banner-dark.svg">
  <img alt="tvsign: your Apple TV apps, signed and renewed before day 7" src=".github/assets/banner-light.svg" width="100%">
</picture>

<p align="center">
  <img alt="Docker amd64 / arm64" src="https://img.shields.io/badge/docker-amd64%20%C2%B7%20arm64-2f6bff?style=flat-square&logo=docker&logoColor=white">
  <img alt="Windows 10 / 11" src="https://img.shields.io/badge/windows-10%20%C2%B7%2011-111318?style=flat-square">
  <img alt="NuvioTV: tvOS 26+" src="https://img.shields.io/badge/NuvioTV-tvOS%2026%2B-000?style=flat-square&logo=apple">
  <img alt="iOS / iPadOS 17+" src="https://img.shields.io/badge/iOS%20%C2%B7%20iPadOS-17%2B-000?style=flat-square&logo=apple">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-555?style=flat-square"></a>
</p>

[English](#english) · [Français](#français)

---

## English

### Quick start

- **A Windows PC that stays on (the simplest way):** download `tvsign-setup-<version>.exe` from the
  [Releases page](https://github.com/esalwey/tvsign/releases), run it and follow the wizard. The installer
  is not code-signed yet: SmartScreen asks for a confirmation (*More info → Run anyway*), and on Windows 11
  with **Smart App Control** turned on it cannot run at all, see
  [Windows blocks the installer](windows/README-windows.md#windows-blocks-the-installer-smart-app-control-antivirus).
- **A NAS, a Raspberry Pi or any Linux box:** `sh install.sh --install` (it needs Docker), then open
  `http://<host>:8765`. Details in [Install](#install).
- In both cases: a dedicated free Apple ID, the Apple TV (or iPhone / iPad) on the same network, and a
  machine that stays on. The wizard walks through the rest.

### What it is

tvsign keeps sideloaded tvOS apps alive on your Apple TVs. Apps signed with a free Apple ID expire
after 7 days; tvsign re-signs them with your own Apple ID and reinstalls them over Wi-Fi/Ethernet
before they expire, and installs new releases of the apps you follow (GitHub releases, a link, or
an uploaded `.ipa`). Everything runs on your own machine, managed from a small web interface.
iPhones and iPads (iOS / iPadOS 17+) work the same way: see [iPhone and iPad](#iphone-and-ipad).

Two editions share the same code:

- **Self-host (this page):** a Docker container for an always-on Linux box: NAS, Raspberry Pi,
  home server.
- **Windows desktop:** a small window app with a tray icon, for a PC that stays on. Install,
  SmartScreen / Smart App Control, firewall and sleep: see the [Windows guide](windows/README-windows.md).

Run one tvsign per Apple ID. Two instances on the same free account revoke each other's
certificates.

tvsign is not affiliated with Apple. Each user signs apps with their own Apple ID; tvsign does not
host or distribute any app content.

### Requirements

- A Linux host, **amd64 or arm64** (Raspberry Pi needs a 64-bit OS). Docker Engine with Compose v2.
- **The same local network (same L2 segment / VLAN) as the Apple TV.** tvsign finds Apple TVs with
  mDNS/Bonjour (`_remotepairing._tcp`, UDP 5353), which does not cross routers or VLANs. Across VLANs
  you can use an mDNS reflector (UniFi "Multicast DNS", `avahi-daemon` with `enable-reflector=yes`),
  **or skip it**: give tvsign the TV's IP address (see "When discovery is blocked" below) and it
  queries the TV directly, which only needs UDP/TCP to be routed between the two.
- `network_mode: host` (multicast does not reach a bridge network; with a bridge network the
  address fallback below still works).
- Outgoing Internet access: Apple (`gsa.apple.com`, `developerservices2.apple.com`), the anisette
  server (`ani.jkcoxson.com` by default, `ani2.stikstore.app` as automatic fallback, changeable in
  Settings), `api.github.com` for releases.
- A correct clock (NTP).
- A system version the app can run on: each app has its own minimum (`MinimumOSVersion`), which tvsign
  checks against the device; the suggested NuvioTV app needs tvOS 26 or later. iPhone and iPad need
  iOS / iPadOS 17 or later for the wireless renewal.
- Not supported: Docker Desktop on Windows/macOS (use the Windows edition), 32-bit ARM, TrueNAS CORE.

No `NET_ADMIN`, no `/dev/net/tun` and no root are needed: the encrypted tunnel to the device runs
entirely in user space inside `plumesign`. The server runs as an unprivileged user. An Apple TV needs
no IPv6; an iPhone or iPad may announce itself with a link-local IPv6 address only (see
[iPhone and iPad](#iphone-and-ipad)).

### Install

Check the host, then install into `/opt/tvsign`:

```sh
sh install.sh              # checks only, changes nothing
sh install.sh --install    # writes /opt/tvsign/docker-compose.yml and starts tvsign
```

`install.sh` is in this repository: `git clone https://github.com/esalwey/tvsign && cd tvsign`, or just
`curl -fsSLO https://raw.githubusercontent.com/esalwey/tvsign/main/install.sh`. It needs root or
`sudo` for `/opt/tvsign`; on a shell with neither (many NAS shells) it installs into `~/tvsign`
instead, or into any folder you give: `sh install.sh --install "$HOME/tvsign"`.

Or by hand with [`docker-compose.yml`](docker-compose.yml):

```sh
mkdir tvsign && cd tvsign      # put docker-compose.yml here
docker compose up -d
docker compose logs tvsign     # the one-time setup code is printed here
```

Set `PUID`/`PGID` to the owner of the data folder (`id -u`, `id -g`), and `TZ` to your time zone
(`timedatectl`), in the file or in a `.env` file next to it. Without them the container runs in UTC
as uid/gid 1000: journal and renewal times are then in UTC, and a data folder owned by someone else
cannot be written. The compose file also caps the container log (10 MB x 3 files).

### Platform notes

**Raspberry Pi.** Raspberry Pi OS 64-bit (Bookworm/Trixie), Pi 3B+/4/5/Zero 2 W. tvsign uses under
200 MB of RAM. Prefer Ethernet; on Wi-Fi turn power saving off (`iw dev wlan0 set power_save off`,
or `wifi.powersave = 2` in NetworkManager) or discovery becomes intermittent. Install Docker with
`curl -fsSL https://get.docker.com | sh`.

**Synology DSM 7.2+.** x86 models and ARMv8 models (e.g. rtd1296, rtd1619b); old ARMv7 models are
not supported. Container Manager → Project → Create, paste the compose file, keep "host" network.
Use a folder such as `/volume1/docker/tvsign` and keep it out of shared folders.
`PUID`/`PGID`: the uid/gid of the DSM user that owns that folder.

**QNAP (Container Station 3).** Applications → Create → paste the YAML; Host network mode is
accepted. Port 8765 may clash with a QNAP service: change `TVSIGN_PORT`.

**Unraid 6.12 / 7.** Add a container: Repository `ghcr.io/esalwey/tvsign:1`, Network Type `Host`,
path `/mnt/user/appdata/tvsign` → `/data`, variables `PUID=99`, `PGID=100`, `TZ`, WebUI
`http://[IP]:[PORT:8765]`. Back up with the Appdata Backup plugin.

**TrueNAS SCALE 24.10+.** Apps → Discover → ⋮ → Install via YAML, host network, a dataset such as
`apps/tvsign` mounted on `/data`. Use `PUID=568`, `PGID=568` (the `apps` user). TrueNAS CORE
(FreeBSD) is not supported: use a Debian VM.

**Proxmox VE.** Simplest: a Debian VM bridged on `vmbr0` (same LAN as the Apple TV), then
`install.sh`. In an LXC container, enable `nesting=1,keyctl=1` for Docker and attach the NIC to the
bridge of the TV's LAN, not a NATed network. Unprivileged LXC is fine: no TUN device is needed.

### First run

1. Open `http://<host>:8765`.
2. Enter the **setup code** from `docker compose logs tvsign` (also in `<data>/setup-code.txt`) and
   choose the admin password. `TVSIGN_PASSWORD_FILE` can preset it instead.
3. The wizard then walks through: language (or **restore a backup**), environment checks, Apple ID
   sign-in (a dedicated free Apple ID is recommended; the password is never stored), device pairing
   (Apple TV: Settings › Remotes and Devices › Remote App and Devices, then type the PIN; iPhone / iPad:
   see below), and the app to keep installed.

The wizard says "all set" only once the first install has actually succeeded; until then it shows
the live state of the install (and the reason if it failed). By default tvsign only replaces its own
certificates: if the Apple ID already holds 2, signing stops with an "Apple account full" message until
you revoke one at developer.apple.com or allow it in Settings › Certificates. AltStore, SideStore and
Sideloadly can still revoke tvsign's certificate, which makes its apps stop launching: use an Apple ID
dedicated to tvsign. The machine running tvsign must stay awake and on the network (a PC or NAS that
sleeps renews nothing); only the Apple TV may sleep.

The Diagnostics page re-runs the environment checks at any time.

### Several Apple TVs

Pair every TV once (Devices › Pair). To keep the same app on a second TV, open the app's
**Settings**, choose the TV under "Same app on another Apple TV" and press *Add to this TV*: a
separate card is created for that TV, with its own status, signature and expiry (the downloaded
file is reused, and both TVs use the same Apple ID and the same App ID). Adding the same app twice
for the same account **and the same TV** is refused. Removing a TV is refused while an app is being
installed on it; if its pairing is already gone from the plumesign store, it is simply removed.

### When discovery is blocked (VLAN, Docker bridge network, firewall)

Symptoms: *No Apple TV waiting to be paired*, or renewals failing with "unreachable" although the TV
is on. The Diagnostics page says whether the cause looks like a Docker bridge network or a blocked
multicast, and whether the TV still answers directly.

- **Pairing:** in the pair dialog, open *TV not found? Enter its address*, type the TV's IP
  address (Apple TV: Settings › Network) and press *Search at this address*.
- **Already paired TVs:** tvsign remembers each TV's last address and queries it directly before
  browsing the network, so renewals keep working when multicast is filtered. If the TV gets a new
  address, press *Address* on its row, enter the new one and *Save and test*. Leave it empty to go
  back to automatic discovery.
- The direct query is a plain unicast mDNS question to UDP 5353 on the TV; the tunnel itself is TCP to the
  ports the TV announces. Both must be allowed between tvsign and the TV. `TVSIGN_UNICAST_HINT=0`
  turns the direct query off.

### Using the web UI

- **App cards** show the state of each app. tvsign checks (read-only) that an app is still on the
  Apple TV when you press **Test connection** (the periodic check, every `TVSIGN_INSTALL_CHECK_HOURS`,
  also reinstalls an app that disappeared); an app seen missing shows "Not found on the device" with a
  **Reinstall** button. A refused update (changed app identifier, bad checksum) shows its reason with
  **Retry this version** and, for a changed identifier, **Accept this change**.
- **Renew all** (header, with two apps or more) renews the active apps one after the other.
  An Apple TV can be renamed from its row. A GitHub repository can be pasted as a full address
  (`https://github.com/owner/project`).
- Errors are translated from stable codes (English and French). In English, the usual server log
  lines are translated too; unknown ones stay as the server wrote them.
- **Notifications**: Settings › Notifications explains ntfy and can generate a private topic. For
  external monitoring (Uptime Kuma, Home Assistant…), `GET /api/health` answers 200 when all is
  well and 503 otherwise, without signing in (details only with a session).
- The UI warns when it is served over plain `http://` on the network: the Apple ID password and
  codes then travel unencrypted. Use a VPN or an HTTPS reverse proxy (see Security). Added to a
  phone's home screen, the page opens like an app (manifest and icon included).

### iPhone and iPad

iPhones and iPads (**iOS / iPadOS 17 or later**) are kept alive like an Apple TV: tvsign signs with
your Apple ID and reinstalls over Wi-Fi before the 7 days are up. **Status: new. The renewal path is
the Apple TV one (same tunnel, same installer), but the one-time USB pairing step has not been
validated on every device and iOS release yet: treat it as experimental and report what you see.**

**What you need first.** The one-time pairing needs a computer with a USB port and the `plumesign` tool.
On Windows that is the Windows edition (`plumesign.exe` is in its install folder; install iTunes or the
Apple Devices app for the USB driver). On macOS or Linux the releases carry no binary yet: build it
(`cargo build --release -p plumesign` in `vendor/Impactor`, Rust required) or, on Linux, copy it out of the
Docker image (`docker create --name t ghcr.io/esalwey/tvsign:1 && docker cp t:/usr/local/bin/plumesign .
&& docker rm t`). If you only have a NAS and no such computer, an iPhone or iPad is the harder route
today; an Apple TV needs none of this.

**One-time setup, per device**

1. **Pair once.** An iPhone shows no PIN screen like an Apple TV, so the pairing keys are created over
   USB (`plumesign pair usb`). After that every renewal runs over Wi-Fi.
   - *Windows edition:* plug the device in, unlock it, tap **Trust**, then Pair › iPhone / iPad ›
     **Pair over USB**. This needs Apple's USB driver (Apple Mobile Device Service): install iTunes or
     the Apple Devices app from the Microsoft Store; tvsign does not bundle it.
   - *Docker:* the container has no USB on purpose (host networking only, no devices, no root).
     Pair on any computer that has usbmuxd (Windows with iTunes / Apple Devices, macOS, Linux with the
     `usbmuxd` package) with `plumesign pair usb` (`plumesign.exe` sits in the Windows edition's install
     folder; other systems: build it from `vendor/Impactor`, see [docs/building.md](docs/building.md)).
     Then in tvsign: Pair › iPhone / iPad › **Import a pairing file** and upload the `<UDID>.plist`
     created in plumesign's `remotepairing` folder (`%APPDATA%\PlumeImpactor\remotepairing` on
     Windows, `~/.config/PlumeImpactor/remotepairing` elsewhere), **not** the `<UDID>.info.plist` next
     to it. The UDID is read from the file name (`<UDID>.plist`, `plume_<UDID>.plist`), otherwise
     type it. A pairing file made by Impactor is accepted too.
2. **Install a first app** from tvsign. It installs even though Developer Mode is still off, but it
   may not launch yet.
3. **Trust the developer** once the app is installed: Settings › General › VPN & Device Management ›
   your Apple ID › Trust (once per Apple ID). tvsign cannot do it for you.
4. **Turn on Developer Mode:** Settings › Privacy & Security › Developer Mode, then restart the
   device. The option does not exist on a device that never ran a signed app, so it only appears
   after step 2; tvsign cannot reveal or enable it, but shows a banner while it is off.

**What is different from an Apple TV**

- **Away from home is normal.** A phone leaves, sleeps, goes into Low Power Mode. iPhones and iPads
  renew `TVSIGN_RENEW_BEFORE_HOURS_MOBILE` (96 h) before expiry instead of 60 h (never less than the
  general setting; a per-device value can be set through the API: `PATCH /api/devices/<id>` with
  `renew_before_hours`). While an app of a phone is due, tvsign looks for the phone on the network every
  `TVSIGN_PRESENCE_MINUTES` (3, `0` = off) and renews as soon as it shows up, instead of waiting for the
  next retry. "Not on the network" and "locked since it started" (unlock it once with its passcode) are
  transient and notify only when expiry gets close, like a sleeping TV.
- **No "close the app" wait.** iOS replaces a running app; a stalled install on a phone means a locked,
  busy or full device.
- **Network.** The phone only answers Bonjour when awake (screen on or charging helps), and may
  announce itself with a link-local IPv6 address only: a host with IPv6 disabled (some NAS and Docker
  setups) may then never see it. Wi-Fi client isolation and VLANs behave as for an Apple TV. There is
  no guarantee that an idle phone is reachable.
- **Free account limits** apply to the whole Apple ID, tvOS and iOS apps together: 10 App IDs per
  7 days (every extension, such as a widget or share sheet, costs one more) and 3 apps installed at a
  time per device. No push notifications, iCloud or associated domains. tvsign warns when an IPA has
  extensions or such entitlements (detected when the IPA carries its signature) and shows, per Apple
  ID, the apps and the estimated App IDs.
- **Use an Apple ID dedicated to tvsign.** AltStore, SideStore and Sideloadly on the same Apple ID revoke
  each other's certificates in a loop; tvsign says so after three revocations in 7 days.
- Releases that ship both an iOS and a tvOS `.ipa` are matched to the device (an iPad-only app is refused
  on an iPhone, `MinimumOSVersion` is checked against the device).

**Not supported:** iOS / iPadOS 16 and older (no wireless RemotePairing; pre-17 Wi-Fi sync is another
protocol), Apple Watch, Apple Vision Pro (recognised and refused for now), Mac, and pairing over
Wi-Fi alone without USB or an imported file.

### Backups

`/data` (`./tvsign-data` on the host) holds the Apple session, the development certificate's
private key, the Apple TV pairings and the database. **It is secret**: back it up, never share it,
never put it in a synced or network-shared folder in clear.

- Settings → Backup → **Export** downloads a `.tvsign-backup` archive (database, Apple sessions,
  keys, pairings; uploaded IPAs optional). Restore it from Settings or from the first-run wizard.
  The archive moves between the Docker and Windows editions without re-pairing the Apple TV.
- Restoring checks the archive first (integrity, tables and columns of the database, migration to
  the current schema) and only then replaces anything. The replacement is all-or-nothing: if a step
  fails (file locked, disk full), the steps already done are undone and the instance stays as it was.
  Renewals and the web interface pause while it runs. The previous state is kept aside for 30 days.
- The archive is **not encrypted**. On Windows the browser's Downloads folder is often synced by
  OneDrive: choose a folder that is not, or move the file to a safe place right away.
- Imported certificates (see *Signing with an imported certificate*) are not in the archive unless
  you tick *Include imported certificates and their private keys* (the box only appears when there is
  one).
- Or back up the whole data folder with the container stopped (Hyper Backup, Appdata Backup, restic…).
- Never run the old and the new instance at the same time after a move.

### Updates

```sh
docker compose pull && docker compose up -d
```

Tags: `:1` (recommended, stays on major version 1), `:1.2`, `:1.2.3`, `:latest`, `:edge` (main
branch, for testers). `:1` never moves to a new major version: to go to 2.x, change the tag in
`docker-compose.yml` (`ghcr.io/esalwey/tvsign:2`) before pulling. The database is copied before any schema change. Avoid updating during a
renewal: `stop_grace_period: 5m` and `TVSIGN_SHUTDOWN_DRAIN_SECONDS` (240) let an install in
progress finish before the container stops. Watchtower or Diun work but are optional. What changed in each
release: [docs/CHANGELOG.md](docs/CHANGELOG.md).

**Going back to an older version.** The database of 1.1 holds more than 1.0.x knows. 1.0.x ignores the
extra columns and starts normally, with your apps, accounts and pairings, but it also ignores the
per-app settings of 1.1: an app signed with an imported certificate would be signed again with the Apple
ID (and use a free-account slot), and a pinned version, "new versions only when I ask" and held-back
updates are forgotten. Before going back, switch such apps to the Apple ID and clear the pins. tvsign
copies the database before changing its schema (`tvsign.db.pre-migrate-<time>` next to it, kept 30 days):
with tvsign stopped, putting that copy back restores the state from before the upgrade.

tvsign looks once a day for a newer release of itself (one request to the GitHub API, nothing else
is sent) and shows a dismissible banner with the right instruction for your edition. It never
updates itself. The Windows edition also shows a notification and a tray menu item. Turn it off in
Settings (“Check for tvsign updates”) or with `TVSIGN_UPDATE_CHECK=off`; `TVSIGN_UPDATE_REPO` points it
at a fork.

### Windows

1. Download `tvsign-setup-<version>.exe` from the Releases page of this repository and run it. It
   installs for your Windows account only, no administrator password. If Windows says *Windows
   protected your PC*, click **More info → Run anyway**: the installer is not code-signed yet. On
   Windows 11 with Smart App Control turned on there is no *Run anyway*:
   [read this first](windows/README-windows.md#windows-blocks-the-installer-smart-app-control-antivirus).
2. Keep “Start tvsign with Windows” ticked. Closing the window does not quit tvsign: it keeps
   running next to the clock (tray icon) and renews your apps.
3. On the first pairing, Windows may ask about **plumesign** in the firewall: allow it on
   **private networks**, otherwise the Apple TV cannot be found.
4. The PC must stay **awake and online**: a sleeping or switched-off PC renews nothing (profiles are
   renewed 60 h before they expire, which leaves about two and a half days of margin).
5. **Updating:** run the new installer over the old one. It upgrades in place and keeps your apps,
   Apple session and pairings; tvsign shows a banner when a new version exists.
6. **Uninstalling:** Windows Settings → Apps → tvsign. Your data (`%LOCALAPPDATA%\tvsign`) is kept
   unless you tick “Also delete my tvsign data”.

The full guide (French) is [windows/README-windows.md](windows/README-windows.md).

### Security

- **Never expose tvsign to the Internet.** No port forwarding, no public reverse proxy. For remote
  access use a VPN (Tailscale, WireGuard) into your LAN.
- The web UI is protected by an admin password (scrypt), a one-time setup code for the first
  launch, signed `HttpOnly`/`SameSite=Strict` session cookies, CSRF and Origin checks, a Host
  allow-list and a login rate limit.
- `TVSIGN_AUTH=none` disables the password: only behind an authenticating proxy or VPN, and only
  with `TVSIGN_ALLOWED_HOSTS` set.
- Behind a reverse proxy on the same host: `TVSIGN_HOST=127.0.0.1`, `TVSIGN_TRUSTED_PROXIES=127.0.0.1`,
  HTTPS, and an upload limit of at least 1 GB (IPAs). The proxy must pass the original `Host`
  header: with nginx add `proxy_set_header Host $host;` (plus `X-Forwarded-For` and
  `X-Forwarded-Proto`) and `client_max_body_size 2g;`. Otherwise every login and write is refused
  with "unexpected origin" (Caddy and Traefik keep `Host` by default). If a page answers
  `host_not_allowed`, add the address you type in the browser to `TVSIGN_ALLOWED_HOSTS`.
- Use a dedicated Apple ID. The Apple password is passed to `plumesign` through its environment,
  never on a command line, and is not stored. A free Apple ID holds 2 development certificates; by
  default tvsign only replaces its own (Settings → Certificates). If you allow it to revoke any
  certificate, apps that AltStore, SideStore, Sideloadly or Xcode signed with that Apple ID (on an
  iPhone, say) stop launching. The first sign-in sheet asks you to confirm this, and a banner stays
  on the main page after such a revocation.
- Desktop edition without a password: any program or Windows account on that computer can reach
  tvsign. Settings says so; set a password if others use the computer.
- Notification secrets (webhook addresses, bot and access tokens, ntfy passwords) are stored in the
  database, are never sent back by the API (shown as `********`) and are masked in logs, the support
  bundle and error messages. Notifications are plain POSTs that never follow redirects.
- Imported certificates (an advanced, optional route) are stored encrypted and never returned by the
  API; see *Signing with an imported certificate*.

### What leaves your machine

- **Apple** (`gsa.apple.com`, `developerservices2.apple.com`): the Apple ID sign-in and the signing
  requests, as with any tool that signs with a free Apple ID.
- **The anisette server** (`ani.jkcoxson.com`, `ani2.stikstore.app` as fallback, or one you run
  yourself): a third party that supplies the device data Apple asks for at sign-in. Your Apple ID
  password is not sent to it.
- **GitHub** (`api.github.com` and the release downloads): the apps you follow, and once a day one
  request to look for a new release of tvsign itself (`TVSIGN_UPDATE_CHECK=off` or the Settings switch
  turns that one off). The only thing it carries is `tvsign/<version>` as User-Agent.
- **The notification services you add** (ntfy, Discord, Telegram, Gotify, a webhook): messages that name
  your Apple ID and your apps.
- Nothing else: no analytics, no telemetry, no account with tvsign.

### Configuration

Everything is optional. The main variables: `TZ`, `PUID`, `PGID`, `TVSIGN_PORT` (8765),
`TVSIGN_HOST` (0.0.0.0), `TVSIGN_PASSWORD_FILE`, `TVSIGN_ALLOWED_HOSTS`, `TVSIGN_NTFY_URL`,
`TVSIGN_RENEW_BEFORE_HOURS` (60), `TVSIGN_RENEW_BEFORE_HOURS_MOBILE` (96, iPhone / iPad),
`TVSIGN_PRESENCE_MINUTES` (3, how often a due phone is looked for; 0 = off), `TVSIGN_ANISETTE_URL`
(empty: `ani.jkcoxson.com`, fallback `ani2.stikstore.app`), `TVSIGN_GITHUB_TOKEN`, `TVSIGN_LANGUAGE`.
Settings set in the web UI are stored in the database; an environment variable always wins.

An app is only shown as valid when the Apple TV really has it: after each installation tvsign checks
the TV's app list, and every `TVSIGN_INSTALL_CHECK_HOURS` (6, 0 = never) it checks again. An app
uninstalled from the TV (or lost in a TV reset) turns red and is reinstalled at once. A sleeping TV
is simply checked again later. Renewals of different TVs run in parallel; an install waiting for an
open app only holds up its own TV.

App open on the Apple TV: tvsign installs on schedule and never tries to guess whether the app is
open (tvOS keeps closed apps in memory, so a running process says nothing). When the app really is
on screen, the Apple TV itself holds the update until it is closed: the app card then asks to close
the app, and tvsign gives up after `TVSIGN_APP_WAIT_MINUTES` (30) and retries later.

Followed sources: a GitHub release is reinstalled when its tag changes or when its `.ipa` is
replaced under the same tag (new checksum). A direct link is checked at each renewal (ETag,
`Last-Modified` and size sent by the server) and downloaded again when the file changed; a server
that sends none of them is followed by downloading the file again at renewal and comparing its
checksum (the app card says so). Links must be `https://`, cannot contain `user:password@`, and
cannot point to private or local addresses (including Tailscale/CGNAT `100.64.0.0/10`) unless
`TVSIGN_ALLOW_PRIVATE_URLS=1`; GitHub itself is never subject to that check. `TVSIGN_GITHUB_TOKEN`
lifts GitHub's limit of 60 requests per hour and also makes private repositories work (the token
is only ever sent to `api.github.com`). Shutdown: a renewal in progress may finish for up to
`TVSIGN_SHUTDOWN_DRAIN_SECONDS` (240) before it is interrupted.

Update control, in each app's Settings and on its card:

- **New versions**: installed automatically (default), or only when you ask. When off, the app is
  still renewed before it expires; the card shows “Update available” with the release notes and an
  **Install now** button.
- **Pinned version** (GitHub): follow one release tag only. A tag that does not exist in the
  repository is reported on the app's card (and in the log); the app keeps its installed version.
- **Previous version**: the last installed IPA is kept; the button reinstalls it and skips the
  version you left until another release comes out.
- If a new version cannot be signed or installed, tvsign tries the previous one and skips the
  broken release. An update that fails never turns a valid app red: the installed version stays
  valid and the update is retried later. Only a fault of the IPA itself (invalid bundle, entitlements,
  package refused by the Apple TV) counts: account limits, a full disk, the network or an open app never
  make tvsign give up a new version. An optional update waits for an open app at most
  `TVSIGN_UPDATE_WAIT_MINUTES` (5 by default, instead of 30 for a renewal).
- A release that needs a newer tvOS than your Apple TV runs (`MinimumOSVersion`) is held back, and
  installed by itself once the TV is updated; you get a notification. Adding an app that needs a newer
  tvOS than the chosen Apple TV is refused.
- An uploaded app is updated with **Replace the IPA** (card, Settings, or the “IPA missing” message
  after a restore): its history, Apple TV and account are kept.

Notifications: Settings → Notifications holds a list of destinations: **ntfy**, a generic **JSON
webhook** (Home Assistant, n8n, Node-RED: `title`, `message`, `event`, `priority`, `time`, `app`),
**Discord** (webhook), **Telegram** (bot token + chat ID) and **Gotify**. Each destination picks the
events it receives (renewal failed, expiry approaching, renewal recovered, app updates, Apple
account and certificates) and has a **Send a test** button. Messages follow the language chosen in
Settings (with "Automatic", the last language shown in the web interface); times carry their UTC
offset. `TVSIGN_NTFY_URL` still presets an ntfy destination, `TVSIGN_NTFY_EVENTS` its events
(comma separated, `none` for none). On the public ntfy.sh the topic name acts as a password and
messages name your Apple ID and your apps: pick a long random topic, or use a self-hosted ntfy. A
protected server works with `https://user:password@host/topic` (Basic authentication) or an access
token; a bare name such as `my-topic` means `https://ntfy.sh/my-topic`. The destination that comes from
the ntfy address of the settings (or `TVSIGN_NTFY_URL`) has no name or token of its own: put the login
in the address, or add a separate ntfy destination for an access token. Whatever the service (ntfy,
Discord, Telegram, Gotify, a webhook), messages name your Apple ID and your apps and are sent to it:
use a private channel or chat. For Telegram, the chat ID is the number after `"chat":{"id":` in
`https://api.telegram.org/bot<token>/getUpdates` once you have sent a message to your bot (or ask
@userinfobot). A failed test says what to check (wrong token, wrong address, unreachable server…). The
Windows desktop app also raises a Windows notification.

Logs: the access log is off (the page polls every 5 s); `TVSIGN_ACCESS_LOG=1` turns it on.

### Troubleshooting and support

- **Diagnostics** (Settings → Diagnostics) checks plumesign, mDNS, Apple, the anisette server,
  GitHub, the clock and the disk. A bad `Date` header from a proxy does not break it.
- **Apple TV not found / "unreachable".** The Apple TV must be on the same network (VLAN) as tvsign
  and answer mDNS. A Wi-Fi Apple TV in standby, or one switched off with the television (HDMI-CEC),
  often stops answering: prefer Ethernet, keep *Settings › General › Sleep After* long enough, turn
  CEC power-off off at night, or wake it with the remote before the renewal window. tvsign retries
  with a growing delay (45 minutes at most, 10 near expiry) and alerts at 24 h and 6 h.
- **The app is on the Apple TV but does not open.** Its profile may have expired (check its card),
  its certificate may have been revoked by another tool on the same Apple ID (tvsign re-signs it at
  the next check), or the installation may still be waiting because the app is open. Press **Renew**
  on its card, then restart the Apple TV. The app appears in the home screen a few seconds after the
  installation reports success.
- **Apple sign-in errors.** Check the clock (Diagnostics), the anisette server (the default,
  `ani.jkcoxson.com`, is shared and falls back to `ani2.stikstore.app`: a temporary outage is retried
  automatically), and that the
  account is not signed in on another tvsign.
- **Reporting a bug.** Open an issue at <https://github.com/esalwey/tvsign/issues> and attach the
  **support bundle** (Settings → Diagnostics → Support → Download): versions, checks, apps, journal,
  renewal history and the last plumesign outputs, with e-mail addresses, IP addresses, device
  identifiers and secrets masked (it holds no database, Apple session or key). *Copy support info*
  gives the short text version; *Download the log* (in the Log block) the journal alone.

Apple certificates: a free Apple ID holds at most 2 development certificates, and revoking one stops
the apps signed with it from launching (Xcode, SideStore, AltStore on your iPhone...). When the
account is full, tvsign only replaces certificates it created itself (named `tvsign-<id>`, kept
with the keys in `/data`) and dead ones; otherwise signing stops with a "certificate limit" message
listing the certificates, and you either revoke one at developer.apple.com or choose, in Settings
> Certificates, to let tvsign revoke any certificate (`TVSIGN_CERT_REVOKE=any`; `never` forbids
all revocation). A certificate is renewed 30 days before it expires and its end date is shown on
the account. Several Apple teams: the Team button of an account chooses the team used for signing;
it is remembered, so a change in Apple's ordering never moves the apps to another team (the team is
part of the app identifier). Several installs of tvsign (a NAS and a PC) should use different Apple
IDs; each one pairs with the Apple TV under its own name (`tvsign-xxxxxxxx`, or `TVSIGN_PAIR_HOST`).
A self-hosted anisette server over plain `http://` is supported; one over `https://` with a
self-signed certificate needs `PLUMESIGN_ANISETTE_INSECURE=1`.

### Signing in to Apple: the anisette server

Signing in to Apple needs "anisette" data, produced by an anisette-v3 server. Since 12 October 2026
Apple refuses the data of the servers that generate it as Android (error `-80045` at sign-in, then
`HTTP 503` pages), which includes the old default `ani.sidestore.app`. tvsign handles it as follows:

- tvsign uses `https://ani.jkcoxson.com`, which serves Windows anisette, and switches to
  `https://ani2.stikstore.app` by itself when it does not answer. **Leave the Settings field empty**
  for that. The client info and user agent sent to Apple are the ones the server announces.
- A server you choose yourself (Settings, or `TVSIGN_ANISETTE_URL`) is used without the default's
  fallback (several addresses: see "Several anisette servers"), and must serve Windows data too: a server
  that announces anything else is not used (tvsign says so and sends nothing to Apple, which would only
  answer `-80045` or a 503 page). A setting that names one of
  the old Android servers (`ani.sidestore.app`, `ani.stikstore.app`, `anisette.crystall1ne.dev`,
  `ani3server.fly.dev`, `ani.jaydenha.uk`, `omni.ethxn.xyz`) is refused when typed in Settings, and
  removed (replaced by the default if none is left) with a line in the log when it was stored before;
  the same value in an environment variable is ignored.
- The anisette identity is created again by itself (the old one, `state.plist` in the `PlumeImpactor`
  folder, is deleted) and provisioned again whenever the server or Apple says it is not valid.
- Accounts that were already connected keep renewing as long as Apple accepts their session. If it
  does not, the account turns to "Signed out" with the reason, with a single notification: sign it in
  again (password, then the two-factor code of a trusted Apple device).

If signing in still fails:

- **Two-factor code**: Apple often pushes the code to your trusted devices before asking for it; just
  type it. Codes by text message are refused by Apple for now (HTTP 403): use a trusted Apple device.
- **`-22411`**: this Apple ID has never signed in on an Apple device. Sign it in once on an iPhone,
  iPad, Mac or Apple TV, then try again.
- **`-80045`** with the default server: Apple refuses this account or this connection. It is not a
  tvsign problem and retrying makes it worse (Apple limits the attempts): wait several hours, from
  another network if you can, then sign in once. Apps already installed keep working until they expire.
- **HTTP 503**: Apple's servers are busy (tvsign tries once more, and a sign-in then takes the data of
  the other anisette server), or the anisette server is not accepted: check that the Settings field is
  empty. Settings › Diagnostics tells whether the anisette server answers and serves Windows data.
- **HTTP 429**: too many requests. Do not insist: tvsign waits several hours before trying again by itself,
  and the account does not need to be signed in again.

### How renewals behave

- Renewals run side by side: an app waiting for the Apple TV (or for you to close it) never delays
  the others, the expiry alerts or the account checks. The most urgent app is always started first.
- After Apple refuses a request outright (`-80045`, HTTP 429), tvsign waits much longer
  (`TVSIGN_APPLE_BACKOFF_MINUTES`, 180 by default; 30 min at most in the last 12 hours): every new
  attempt can lengthen Apple's block. Other failures are retried with a growing delay (10, 20, 40 min… up to `TVSIGN_RETRY_MINUTES`,
  45 by default; 5 to 15 min when the cause is passing, such as the TV asleep or the anisette server
  down; 10 min at most in the last 12 hours). A failure only you can fix (Apple session expired,
  pairing lost, free-account limit, no developer team, tvOS too old, no Apple TV or `.ipa`) shows **Action needed** on the card and
  is not retried blindly; reconnecting the account, pairing again, editing the app or pressing
  **Renew** resumes it at once.
- A session marked expired is checked again with Apple before an app is refused, and a failed
  check is not final: one passing network or anisette error cannot lock renewals for hours.
- The card shows the version actually installed on the Apple TV ("1.0 installed · 1.1 pending")
  until the new one is installed.
- **Renewal time window** (Settings): restrict non-urgent renewals and updates to certain hours,
  e.g. 02:00 to 06:00 (`TVSIGN_RENEW_WINDOW_START` / `TVSIGN_RENEW_WINDOW_END`, local time).
  It never applies in the last 24 hours before expiry, nor to a manual renewal. Enter both a start and
  an end hour (or neither); start = end means all day. While an app waits for the window, its card
  says "Waiting for the time window (from N h)" and shows when the window opens instead of a
  retry delay; it is not counted as late.
- Loop and limits for tuning: `TVSIGN_CHECK_INTERVAL` (600 s, at least 30), `TVSIGN_RETRY_MINUTES`
  (45, at least 5), `TVSIGN_ACCOUNT_CHECK_HOURS` (6, at least 1). An unreadable or out-of-range value
  is replaced by the default (or clamped) with a warning in the log.

### Free Apple ID limits

A free account allows **3 active apps per Apple TV** and **10 App IDs per 7 days** (each extension,
such as a TopShelf extension, uses one more). Apps installed with other tools (SideStore, AltStore,
Xcode) count too. tvsign shows "active apps 2/3" per Apple TV on the account card and warns when
you add a fourth. When Apple or the Apple TV refuses ("maximum number of installed apps…"), the
app shows **Action needed** with the explanation and tvsign waits 24 hours instead of using up more
App IDs. Free a slot (**Pause** an app, or remove it from the Apple TV) then press **Renew**.

### Signing with an imported certificate (advanced)

The free Apple ID is the default and the recommended route. If you already hold a signing certificate
and its provisioning profile, for example from your own Apple Developer account, an app can use them
instead: app settings → **Advanced** → *Method* → *Imported certificate*. tvsign never needs this, and
it does not recommend or contact any signing service.

- Import a `.p12` (with its password) and a `.mobileprovision` **for the app's device**. Nothing is
  saved until every check passes: neither file expired, the profile made for tvOS (Apple TV) or iOS
  (iPhone, iPad), the device's UDID listed in the profile (or an enterprise profile), the certificate
  one of the profile's, and the app's identifier covered by it (a wildcard profile such as `*` or
  `com.example.*`, or one made for exactly this app; an app with extensions needs a wildcard).
  Each refusal says what is wrong.
- A certificate belongs to one device and signs any app its profile covers. Each app chooses *Apple
  ID* or an imported certificate. The free-account limits (3 apps per Apple TV…) do not count these apps.
- **No 7-day renewal.** The app is signed again only to install a new version (or when you press
  **Renew**) and stays valid until the certificate or the profile ends, whichever comes first. You
  are warned (notification, tray, journal) **7 days** and **1 day** before, and when it ends;
  importing a new certificate as *Replaces…* signs the apps again at once.
- **Revocation.** Every 12 hours tvsign asks Apple's OCSP responder about the certificates that apps
  use (`TVSIGN_CERT_CHECK_HOURS`, 0 = never; only `*.apple.com` is contacted) and recognises a device
  refusing a revoked identity at install time. The app then shows **Action needed** with the reason
  and one alert is sent. A check that gets no answer concludes nothing.
- The key and the profile are kept encrypted (AES-256-GCM) in `certs/` of the data folder, the key
  in `vault.key`, both readable by the tvsign user only; the `.p12` and its password are not kept.
  **Backups leave them out** unless you tick *Include imported certificates and their private keys*
  at export (the archive then holds the keys in clear, like the Apple session). The support bundle
  never contains them.
- Switching an app from one route to the other installs it under another identifier (the free route
  adds the team to it): the previous copy stays on the device until you delete it.

### Several anisette servers

`TVSIGN_ANISETTE_URL` (or Settings › Signing service) accepts several addresses separated by spaces:
if the first server is down, the next one takes over and is tried first from then on (the default
server is used with its own fallback when the field is empty or lists it). Every server must serve
Windows data (see above). Diagnostics lists each server with its response time. To run your own, `docker compose --profile anisette up -d`
starts a local anisette-v3 server (`dadoum/anisette-v3-server`) on `127.0.0.1:6969`; put
`http://127.0.0.1:6969` in the setting (host network: tvsign reaches it on loopback). Its data lives
in the `anisette_data` Docker volume and the image is pinned to a tag and digest. If the server does
not answer, check `docker compose logs anisette`.
Like any other server it must announce Windows data to be used: Settings › Diagnostics says
whether tvsign accepts it.

### Forgot the admin password

Run on the host that runs tvsign, as the data owner (not root):

```sh
docker exec -it -u 1000:1000 tvsign python -m app reset-password     # your PUID:PGID
```

It asks for a new password and signs every browser out. (The Windows edition's own window is trusted:
Settings › Security changes the password without asking for the old one.) If the password comes from
`TVSIGN_PASSWORD` or `TVSIGN_PASSWORD_FILE`, change that value and restart instead. A
`TVSIGN_PASSWORD_FILE` that cannot be read (wrong path, a root-only secret while tvsign runs as
`PUID`) is reported in the log and in Diagnostics: the preset password is **not** applied then.

Sign out only ends the session of the browser you click it in (a copied cookie stops working);
**Sign out everywhere** also ends all the others. Backups do not contain the admin password hash.

### Pairing identity

Each installation presents its own host name to the Apple TV when pairing (`tvsign-xxxxxxxx`, picked
once and kept in the data folder) so that two instances do not share one identity; override
with `TVSIGN_PAIR_HOST`. Apple TVs already paired keep the name they were paired with.

Building from source: see [docs/building.md](docs/building.md).

---

## Français

### Démarrage rapide

- **Un PC Windows qui reste allumé (le plus simple) :** téléchargez `tvsign-setup-<version>.exe` depuis la
  [page des versions](https://github.com/esalwey/tvsign/releases), lancez-le et suivez l'assistant.
  L'installeur n'est pas encore signé numériquement : SmartScreen demande une confirmation
  (*Informations complémentaires → Exécuter quand même*), et sous Windows 11 avec **Smart App Control**
  activé il ne peut pas s'exécuter du tout, voir
  [Windows bloque l'installeur](windows/README-windows.md#windows-bloque-linstalleur-smart-app-control-antivirus).
- **Un NAS, un Raspberry Pi ou une machine Linux :** `sh install.sh --install` (Docker est nécessaire),
  puis ouvrez `http://<hôte>:8765`. Détails dans [Installation](#installation).
- Dans les deux cas : un Apple ID gratuit dédié, l'Apple TV (ou l'iPhone / l'iPad) sur le même réseau et
  une machine qui reste allumée. L'assistant guide le reste.

### Ce que c'est

tvsign garde en vie les apps tvOS installées hors App Store sur vos Apple TV. Une app signée avec un
Apple ID gratuit expire au bout de 7 jours ; tvsign la re-signe avec votre propre Apple ID et la
réinstalle par le réseau avant l'expiration, et installe les nouvelles versions des apps suivies
(releases GitHub, lien, ou `.ipa` envoyé). Tout tourne chez vous, piloté par une petite interface web.
Les iPhone et iPad (iOS / iPadOS 17+) fonctionnent de la même façon : voir [iPhone et iPad](#iphone-et-ipad).

Deux éditions, même code :

- **Auto-hébergée (cette page)** : un conteneur Docker pour une machine Linux allumée en
  permanence : NAS, Raspberry Pi, serveur maison.
- **Windows** : une petite application fenêtrée avec icône dans la zone de notification, pour un PC
  qui reste allumé. Installation, SmartScreen / Smart App Control, pare-feu et veille : voir le
  [guide Windows](windows/README-windows.md).

Un seul tvsign par Apple ID : deux instances sur le même compte gratuit révoquent mutuellement
leurs certificats.

tvsign n'est pas affilié à Apple. Chacun signe les apps avec son propre Apple ID ; tvsign
n'héberge ni ne distribue aucun contenu.

### Prérequis

- Un hôte Linux **amd64 ou arm64** (Raspberry Pi : OS 64 bits). Docker Engine avec Compose v2.
- **Le même réseau local (même segment L2 / VLAN) que l'Apple TV.** tvsign trouve les Apple TV par
  mDNS/Bonjour (`_remotepairing._tcp`, UDP 5353), qui ne traverse ni routeur ni VLAN. Entre VLAN,
  on peut utiliser un réflecteur mDNS (UniFi « Multicast DNS », `avahi-daemon` avec
  `enable-reflector=yes`), **ou s'en passer** : donner à tvsign l'adresse IP de la TV (voir « Quand la
  découverte est bloquée » plus bas) pour qu'il l'interroge directement ; il suffit que l'UDP et le
  TCP soient routés entre les deux.
- `network_mode: host` (le multicast n'atteint pas un réseau bridge ; avec un réseau bridge, la
  saisie de l'adresse ci-dessous fonctionne quand même).
- Accès Internet sortant : Apple (`gsa.apple.com`, `developerservices2.apple.com`), le serveur
  anisette (`ani.jkcoxson.com` par défaut, `ani2.stikstore.app` en repli automatique, modifiable dans
  les Réglages), `api.github.com`.
- Une horloge à l'heure (NTP).
- Une version du système que l'app peut utiliser : chaque app a son propre minimum
  (`MinimumOSVersion`), que tvsign compare à l'appareil ; l'app NuvioTV suggérée demande tvOS 26 ou plus
  récent. Un iPhone ou un iPad demande iOS / iPadOS 17 ou plus récent pour le renouvellement sans fil.
- Non pris en charge : Docker Desktop sous Windows/macOS (prendre l'édition Windows), ARM 32 bits,
  TrueNAS CORE.

Ni `NET_ADMIN`, ni `/dev/net/tun`, ni root : le tunnel chiffré vers l'appareil tourne entièrement en
espace utilisateur dans `plumesign`. Le serveur tourne sous un utilisateur non privilégié. Une
Apple TV n'a pas besoin d'IPv6 ; un iPhone ou un iPad peut ne s'annoncer qu'avec une adresse IPv6 locale
au lien (voir [iPhone et iPad](#iphone-et-ipad)).

### Installation

Vérifier l'hôte, puis installer dans `/opt/tvsign` :

```sh
sh install.sh              # vérifications seulement, ne modifie rien
sh install.sh --install    # écrit /opt/tvsign/docker-compose.yml et démarre tvsign
```

`install.sh` est dans ce dépôt : `git clone https://github.com/esalwey/tvsign && cd tvsign`, ou
simplement `curl -fsSLO https://raw.githubusercontent.com/esalwey/tvsign/main/install.sh`. Il
demande root ou `sudo` pour `/opt/tvsign` ; sur un shell qui n'a ni l'un ni l'autre (beaucoup de
NAS), il installe dans `~/tvsign`, ou dans le dossier donné : `sh install.sh --install "$HOME/tvsign"`.

Ou à la main avec [`docker-compose.yml`](docker-compose.yml) :

```sh
mkdir tvsign && cd tvsign      # y placer docker-compose.yml
docker compose up -d
docker compose logs tvsign     # le code d'installation à usage unique s'affiche ici
```

Mettre `PUID`/`PGID` au propriétaire du dossier de données (`id -u`, `id -g`) et `TZ` à votre fuseau
(`timedatectl`), dans le fichier ou dans un fichier `.env` à côté. Sans eux, le conteneur tourne en
UTC avec l'uid/gid 1000 : les heures du journal et des renouvellements sont en UTC, et un dossier de
données appartenant à quelqu'un d'autre n'est pas inscriptible. Le fichier compose limite aussi le
journal du conteneur (3 fichiers de 10 Mo).

### Notes par plateforme

**Raspberry Pi.** Raspberry Pi OS 64 bits (Bookworm/Trixie), Pi 3B+/4/5/Zero 2 W. tvsign utilise
moins de 200 Mo de RAM. Préférer l'Ethernet ; en Wi-Fi, couper l'économie d'énergie
(`iw dev wlan0 set power_save off`, ou `wifi.powersave = 2` dans NetworkManager), sinon la découverte
devient intermittente. Docker : `curl -fsSL https://get.docker.com | sh`.

**Synology DSM 7.2+.** Modèles x86 et ARMv8 (rtd1296, rtd1619b…) ; les anciens ARMv7 ne sont pas
pris en charge. Container Manager → Projet → Créer, coller le compose, garder le réseau « host ».
Dossier du type `/volume1/docker/tvsign`, hors des dossiers partagés. `PUID`/`PGID` : uid/gid de
l'utilisateur DSM propriétaire du dossier.

**QNAP (Container Station 3).** Applications → Créer → coller le YAML ; le mode réseau Host est
accepté. Le port 8765 peut entrer en conflit avec un service QNAP : changer `TVSIGN_PORT`.

**Unraid 6.12 / 7.** Ajouter un conteneur : Repository `ghcr.io/esalwey/tvsign:1`, Network Type
`Host`, chemin `/mnt/user/appdata/tvsign` → `/data`, variables `PUID=99`, `PGID=100`, `TZ`, WebUI
`http://[IP]:[PORT:8765]`. Sauvegarde avec le plugin Appdata Backup.

**TrueNAS SCALE 24.10+.** Apps → Discover → ⋮ → Install via YAML, réseau hôte, un dataset du type
`apps/tvsign` monté sur `/data`. `PUID=568`, `PGID=568` (utilisateur `apps`). TrueNAS CORE (FreeBSD)
n'est pas pris en charge : utiliser une VM Debian.

**Proxmox VE.** Le plus simple : une VM Debian sur `vmbr0` (même LAN que l'Apple TV), puis
`install.sh`. En conteneur LXC : activer `nesting=1,keyctl=1` pour Docker et relier l'interface au
pont du LAN de la TV, pas à un réseau NAT. Un LXC non privilégié convient : aucun périphérique TUN
n'est nécessaire.

### Premier lancement

1. Ouvrir `http://<hôte>:8765`.
2. Saisir le **code d'installation** affiché par `docker compose logs tvsign` (aussi dans
   `<données>/setup-code.txt`) et choisir le mot de passe administrateur. `TVSIGN_PASSWORD_FILE`
   permet de le prédéfinir.
3. L'assistant enchaîne : langue (ou **restaurer une sauvegarde**), vérification de l'environnement,
   connexion à l'Apple ID (un Apple ID gratuit dédié est conseillé ; le mot de passe n'est jamais
   enregistré), appairage de l'appareil (Apple TV : Réglages › Télécommandes et appareils › App
   Télécommande et appareils, puis saisir le code ; iPhone / iPad : voir plus bas), et l'app à garder
   installée.

L'assistant n'affiche « C'est prêt » qu'une fois la première installation réussie ; d'ici là il
montre l'avancement réel (et la raison en cas d'échec). Par défaut, tvsign ne remplace que ses propres
certificats : si l'Apple ID en a déjà 2, la signature s'arrête avec le message « Compte Apple plein »
jusqu'à ce que vous en révoquiez un sur developer.apple.com ou que vous l'autorisiez dans Réglages ›
Certificats. AltStore, SideStore et Sideloadly peuvent en revanche révoquer le certificat de tvsign, ce
qui empêche ses apps de se lancer : prendre un Apple ID dédié à tvsign. La machine qui fait tourner
tvsign doit rester allumée et connectée au réseau (un PC ou un NAS en veille ne renouvelle rien) ; seule
l'Apple TV peut être en veille.

La page Diagnostic relance les vérifications à tout moment.

### Plusieurs Apple TV

Appairer chaque TV une fois (Appareils › Appairer). Pour garder la même app sur une seconde
TV, ouvrir les **Réglages** de l'app, choisir la TV sous « La même app sur une autre Apple TV » puis
*Ajouter sur cette TV* : une fiche séparée est créée pour cette TV, avec son état, sa signature et
son échéance (le fichier téléchargé est repris ; les deux TV utilisent le même Apple ID et le même
identifiant d'app). Ajouter deux fois la même app pour le même compte **et la même TV** est refusé.
Retirer une TV est refusé pendant qu'une app s'y installe ; si son appairage a déjà disparu du store
plumesign, elle est simplement retirée.

### Quand la découverte est bloquée (VLAN, Docker en pont, pare-feu)

Symptômes : *Aucune Apple TV en attente d'appairage*, ou renouvellements en échec « injoignable »
alors que la TV est allumée. La page Diagnostic indique si la cause ressemble à un réseau Docker en
pont ou à un multicast bloqué, et si la TV répond encore en direct.

- **Appairage :** dans la fenêtre d'appairage, ouvrir *TV introuvable ? Saisis son adresse*, saisir
  l'adresse IP de la TV (Apple TV : Réglages › Réseau) puis *Rechercher à cette adresse*.
- **TV déjà appairées :** tvsign retient la dernière adresse de chaque TV et l'interroge directement
  avant de parcourir le réseau : les renouvellements continuent quand le multicast est filtré. Si la
  TV change d'adresse, utiliser *Adresse* sur sa ligne, saisir la nouvelle puis *Enregistrer et
  tester* ; vide, on revient à la découverte automatique.
- La requête directe est une simple question mDNS en unicast vers le port UDP 5353 de la TV ; le tunnel
  est en TCP vers les ports annoncés par la TV. Les deux doivent être autorisés entre tvsign et la TV.
  `TVSIGN_UNICAST_HINT=0` désactive la requête directe.

### Utiliser l'interface web

- Les **cartes d'app** montrent l'état de chaque app. tvsign vérifie (en lecture seule) qu'une app est
  toujours sur l'Apple TV à **Tester la connexion** (le contrôle périodique, toutes les `TVSIGN_INSTALL_CHECK_HOURS`,
  réinstalle aussi une app disparue) ; une app vue absente affiche « Introuvable sur l'appareil » avec un bouton **Réinstaller**.
  Une mise à jour refusée (identifiant d'app changé, empreinte
  fausse) affiche sa raison avec **Réessayer cette version** et, pour un identifiant changé,
  **Accepter ce changement**.
- **Tout renouveler** (en-tête, à partir de deux apps) renouvelle les apps actives l'une après l'autre.
  Une Apple TV se renomme depuis sa ligne. Un dépôt GitHub peut être collé en
  adresse complète (`https://github.com/owner/projet`).
- Les erreurs sont traduites à partir de codes stables (français et anglais). En anglais, les lignes
  habituelles du journal du serveur sont traduites aussi ; les inconnues restent telles que le
  serveur les a écrites.
- **Notifications** : Réglages › Notifications explique ntfy et peut générer un sujet privé. Pour une
  supervision externe (Uptime Kuma, Home Assistant…), `GET /api/health` répond 200 si tout va bien
  et 503 sinon, sans connexion (le détail seulement avec une session).
- L'interface avertit quand elle est servie en `http://` sur le réseau : le mot de passe de l'Apple ID
  et les codes circulent alors en clair. Utiliser un VPN ou un reverse proxy HTTPS (voir Sécurité).
  Ajoutée à l'écran d'accueil d'un téléphone, la page s'ouvre comme une app (manifeste et icône inclus).

### iPhone et iPad

Les iPhone et iPad (**iOS / iPadOS 17 ou plus récent**) sont gardés en vie comme une Apple TV :
tvsign signe avec votre Apple ID et réinstalle par le Wi-Fi avant la fin des 7 jours. **État :
nouveau. Le renouvellement est celui de l'Apple TV (même tunnel, même installateur), mais
l'appairage USB initial n'a pas encore été validé sur tous les appareils et versions d'iOS :
considérez-le comme expérimental et signalez ce que vous constatez.**

**À prévoir d'abord.** L'appairage, une seule fois, demande un ordinateur avec un port USB et l'outil
`plumesign`. Sous Windows, c'est l'édition Windows (`plumesign.exe` est dans son dossier d'installation ;
installez iTunes ou l'app Appareils Apple pour le pilote USB). Sous macOS ou Linux, les versions publiées
n'ont pas encore de binaire : compilez-le (`cargo build --release -p plumesign` dans `vendor/Impactor`,
Rust nécessaire) ou, sous Linux, copiez-le hors de l'image Docker (`docker create --name t
ghcr.io/esalwey/tvsign:1 && docker cp t:/usr/local/bin/plumesign . && docker rm t`). Si vous n'avez
qu'un NAS et aucun ordinateur de ce genre, un iPhone ou un iPad est aujourd'hui la voie la plus
difficile ; une Apple TV n'a besoin de rien de tout cela.

**Mise en place, une fois par appareil**

1. **Appairer une fois.** Un iPhone n'affiche pas d'écran de code comme l'Apple TV : les clés
   d'appairage sont créées par USB (`plumesign pair usb`). Ensuite, tous les renouvellements passent
   par le Wi-Fi.
   - *Édition Windows :* branchez l'appareil, déverrouillez-le, touchez **Se fier**, puis Appairer ›
     iPhone / iPad › **Appairer en USB**. Il faut le pilote USB d'Apple (Apple Mobile Device
     Service) : installez iTunes ou l'app Appareils Apple du Microsoft Store ; tvsign ne l'embarque pas.
   - *Docker :* le conteneur n'a volontairement pas d'USB (réseau hôte seul, aucun périphérique, pas
     de root). Faites l'appairage sur un ordinateur doté de usbmuxd (Windows avec iTunes / Appareils
     Apple, macOS, Linux avec le paquet `usbmuxd`) avec `plumesign pair usb` (`plumesign.exe` est dans
     le dossier d'installation de l'édition Windows ; ailleurs, le compiler depuis `vendor/Impactor`,
     voir [docs/building.md](docs/building.md)). Puis dans tvsign : Appairer › iPhone / iPad ›
     **Importer un fichier d'appairage** et envoyez le `<UDID>.plist` créé dans le dossier
     `remotepairing` de plumesign (`%APPDATA%\PlumeImpactor\remotepairing` sous Windows,
     `~/.config/PlumeImpactor/remotepairing` ailleurs), **pas** le `<UDID>.info.plist` qui est à côté.
     L'UDID est lu dans le nom du fichier (`<UDID>.plist`, `plume_<UDID>.plist`), sinon saisissez-le.
     Un fichier fait par Impactor est accepté aussi.
2. **Installer une première app** depuis tvsign. Elle s'installe même si le Mode développeur est
   encore désactivé, mais peut ne pas se lancer pour l'instant.
3. **Faire confiance au développeur** une fois l'app installée : Réglages › Général › VPN et
   gestion de l'appareil › votre Apple ID › Faire confiance (une fois par Apple ID). tvsign ne peut pas
   le faire à votre place.
4. **Activer le Mode développeur :** Réglages › Confidentialité et sécurité › Mode développeur, puis
   redémarrage de l'appareil. L'option n'existe pas sur un appareil qui n'a jamais exécuté d'app
   signée : elle n'apparaît qu'après l'étape 2. tvsign ne peut ni la faire apparaître ni l'activer,
   mais affiche une bannière tant qu'il est désactivé.

**Ce qui change par rapport à une Apple TV**

- **Être absent de la maison est normal.** Un téléphone sort, se met en veille, passe en économie
  d'énergie. Les iPhone et iPad se renouvellent `TVSIGN_RENEW_BEFORE_HOURS_MOBILE` (96 h) avant
  l'expiration au lieu de 60 h (jamais moins que le réglage général ; une valeur par appareil existe
  via l'API : `PATCH /api/devices/<id>` avec `renew_before_hours`). Tant qu'une app d'un téléphone est
  due, tvsign cherche le téléphone sur le réseau toutes les `TVSIGN_PRESENCE_MINUTES` (3, `0` =
  désactivé) et le renouvelle dès qu'il apparaît, sans attendre le prochain essai. « Hors du
  réseau » et « verrouillé depuis son démarrage » (le déverrouiller une fois avec son code) sont
  passagers et ne notifient qu'à l'approche de l'expiration, comme une TV en veille.
- **Pas d'attente « fermez l'app ».** iOS remplace une app ouverte ; une installation bloquée sur
  un téléphone signale un appareil verrouillé, occupé ou plein.
- **Réseau.** Le téléphone ne répond à Bonjour que réveillé (écran allumé ou en charge, c'est mieux)
  et peut ne s'annoncer qu'avec une adresse IPv6 locale au lien : un hôte sans IPv6 (certains NAS et
  configurations Docker) risque alors de ne jamais le voir. L'isolation des clients Wi-Fi et les
  VLAN se comportent comme pour une Apple TV. Rien ne garantit qu'un téléphone au repos soit joignable.
- **Limites du compte gratuit**, pour tout l'Apple ID, apps tvOS et iOS ensemble : 10 identifiants
  d'app par 7 jours (chaque extension, comme un widget ou le partage, en coûte un de plus) et 3 apps
  installées à la fois par appareil. Ni notifications push, ni iCloud, ni domaines associés. tvsign
  avertit quand une IPA contient des extensions ou ces droits (détecté quand l'IPA porte sa signature)
  et affiche, par Apple ID, les apps et les identifiants d'app estimés.
- **Utilisez un Apple ID dédié à tvsign.** AltStore, SideStore et Sideloadly sur le même Apple ID
  révoquent mutuellement leurs certificats en boucle ; tvsign le signale après trois révocations en 7 jours.
- Les releases qui publient une `.ipa` iOS et une `.ipa` tvOS sont associées à l'appareil (une app
  réservée à l'iPad est refusée sur un iPhone, `MinimumOSVersion` est comparée à l'appareil).

**Non pris en charge :** iOS / iPadOS 16 et plus ancien (pas de RemotePairing sans fil ; la synchro
Wi-Fi d'avant iOS 17 est un autre protocole), Apple Watch, Apple Vision Pro (reconnu et refusé pour
l'instant), Mac, et l'appairage par le seul Wi-Fi sans USB ni fichier importé.

### Sauvegardes

`/data` (`./tvsign-data` sur l'hôte) contient la session Apple, la clé privée du certificat de
développement, les appairages des Apple TV et la base. **C'est secret** : à sauvegarder, à ne jamais
partager, à ne jamais mettre en clair dans un dossier synchronisé ou partagé sur le réseau.

- Réglages → Sauvegarde → **Exporter** télécharge une archive `.tvsign-backup` (base, sessions
  Apple, clés, appairages ; IPA envoyées en option). Elle se restaure depuis les Réglages ou depuis
  l'assistant du premier lancement, et passe d'une édition à l'autre (Docker ↔ Windows) sans
  réappairer l'Apple TV.
- La restauration contrôle d'abord l'archive (intégrité, tables et colonnes de la base, migration
  vers le schéma actuel) et ne remplace rien avant. Le remplacement est tout ou rien : si une étape
  échoue (fichier verrouillé, disque plein), celles déjà faites sont annulées et l'instance reste
  comme elle était. Les renouvellements et l'interface se mettent en pause pendant ce temps.
  L'état précédent est gardé à côté pendant 30 jours.
- L'archive **n'est pas chiffrée**. Sous Windows, le dossier Téléchargements du navigateur est souvent
  synchronisé par OneDrive : choisir un dossier qui ne l'est pas, ou déplacer le fichier aussitôt.
- Les certificats importés (voir *Signature avec un certificat importé*) ne sont pas dans l'archive,
  sauf si vous cochez *Inclure les certificats importés et leurs clés privées* (la case n'apparaît que
  s'il y en a un).
- Ou sauvegarder tout le dossier de données, conteneur arrêté (Hyper Backup, Appdata Backup, restic…).
- Après un déménagement, ne jamais faire tourner l'ancienne et la nouvelle instance en même temps.

### Mises à jour

```sh
docker compose pull && docker compose up -d
```

Tags : `:1` (conseillé, reste en version majeure 1), `:1.2`, `:1.2.3`, `:latest`, `:edge` (branche
main, pour les testeurs). `:1` ne passe jamais à une autre version majeure : pour aller en 2.x, changer le tag
dans `docker-compose.yml` (`ghcr.io/esalwey/tvsign:2`) avant le pull. La base est copiée avant tout changement de schéma. Éviter de mettre à jour
pendant un renouvellement : `stop_grace_period: 5m` et `TVSIGN_SHUTDOWN_DRAIN_SECONDS` (240) laissent
finir une installation en cours avant l'arrêt du conteneur. Watchtower ou Diun fonctionnent mais
restent facultatifs. Ce qui change à chaque version : [docs/CHANGELOG.md](docs/CHANGELOG.md) (en anglais).

**Revenir à une version plus ancienne.** La base de la 1.1 contient plus que ce que connaît la 1.0.x. La
1.0.x ignore les colonnes en plus et démarre normalement, avec vos apps, comptes et appairages, mais elle
ignore aussi les réglages par app de la 1.1 : une app signée avec un certificat importé serait de nouveau
signée avec l'Apple ID (et prendrait une place du compte gratuit), et une version épinglée, « nouvelles
versions seulement sur demande » et les mises à jour mises de côté sont oubliées. Avant de revenir en
arrière, repassez ces apps sur l'Apple ID et retirez les épingles. tvsign copie la base avant de changer
son schéma (`tvsign.db.pre-migrate-<heure>` à côté d'elle, gardée 30 jours) : tvsign arrêté, remettre
cette copie rétablit l'état d'avant la mise à jour.

tvsign cherche une fois par jour s'il existe une version plus récente de lui-même (une requête à
l'API GitHub, rien d'autre n'est envoyé) et affiche une bannière qu'on peut écarter, avec la bonne
consigne pour votre édition. Il ne se met jamais à jour tout seul. L'édition Windows affiche aussi une
notification et une entrée dans le menu de l'icône. Désactivable dans les Réglages
(« Chercher les mises à jour de tvsign ») ou avec `TVSIGN_UPDATE_CHECK=off` ; `TVSIGN_UPDATE_REPO`
vise un fork.

### Windows

1. Télécharger `tvsign-setup-<version>.exe` depuis la page Releases de ce dépôt et le lancer. Il
   s'installe pour votre seul compte Windows, sans mot de passe administrateur. Si Windows affiche
   *Windows a protégé votre ordinateur*, cliquer sur **Informations complémentaires → Exécuter
   quand même** : l'installeur n'est pas encore signé numériquement. Sous Windows 11 avec Smart App
   Control activé, il n'y a pas de bouton *Exécuter quand même* :
   [lire d'abord ceci](windows/README-windows.md#windows-bloque-linstalleur-smart-app-control-antivirus).
2. Laisser coché « Démarrer tvsign avec Windows ». Fermer la fenêtre ne quitte pas tvsign : il
   continue près de l'horloge (icône de la zone de notification) et renouvelle vos apps.
3. Au premier appairage, Windows peut poser une question sur **plumesign** dans le pare-feu : l'autoriser
   sur les **réseaux privés**, sinon l'Apple TV reste introuvable.
4. Le PC doit rester **allumé et connecté** : un PC en veille ou éteint ne renouvelle rien (les
   profils sont renouvelés 60 h avant leur expiration, soit environ deux jours et demi de marge).
5. **Mettre à jour :** lancer le nouvel installeur par-dessus l'ancien. Il met à jour sur place et
   conserve vos apps, la session Apple et les appairages ; tvsign affiche une bannière quand une
   nouvelle version existe.
6. **Désinstaller :** Paramètres Windows → Applications → tvsign. Vos données
   (`%LOCALAPPDATA%\tvsign`) sont conservées, sauf si vous cochez « Supprimer aussi mes données tvsign ».

Le guide complet est [windows/README-windows.md](windows/README-windows.md).

### Sécurité

- **Ne jamais exposer tvsign sur Internet.** Pas de redirection de port, pas de reverse proxy
  public. Pour un accès à distance, passer par un VPN (Tailscale, WireGuard) vers le réseau local.
- L'interface est protégée par un mot de passe administrateur (scrypt), un code d'installation à
  usage unique au premier lancement, des cookies de session signés `HttpOnly`/`SameSite=Strict`,
  des contrôles CSRF et Origin, une liste d'hôtes autorisés et une limite de tentatives de connexion.
- `TVSIGN_AUTH=none` désactive le mot de passe : uniquement derrière un proxy authentifiant ou un
  VPN, et avec `TVSIGN_ALLOWED_HOSTS` défini.
- Derrière un reverse proxy sur le même hôte : `TVSIGN_HOST=127.0.0.1`,
  `TVSIGN_TRUSTED_PROXIES=127.0.0.1`, HTTPS, et une limite d'envoi d'au moins 1 Go (IPA). Le proxy
  doit transmettre l'en-tête `Host` d'origine : avec nginx, ajouter `proxy_set_header Host $host;`
  (et `X-Forwarded-For`, `X-Forwarded-Proto`) et `client_max_body_size 2g;`. Sinon chaque connexion
  et chaque écriture est refusée (« origine inattendue ») ; Caddy et Traefik gardent `Host`. Si une
  page répond `host_not_allowed`, ajouter l'adresse saisie dans le navigateur à `TVSIGN_ALLOWED_HOSTS`.
- Utiliser un Apple ID dédié. Le mot de passe Apple est transmis à `plumesign` par son
  environnement, jamais en ligne de commande, et n'est pas enregistré. Un Apple ID gratuit a 2 certificats
  de développement ; par défaut tvsign ne remplace que les siens (Réglages → Certificats). Si vous
  l'autorisez à révoquer n'importe lequel, les apps qu'AltStore, SideStore, Sideloadly ou Xcode ont
  signées avec cet Apple ID (sur un iPhone par exemple) cessent de se lancer. La première feuille de
  connexion demande de le confirmer, et un bandeau reste affiché sur la page principale après une
  telle révocation.
- Édition bureau sans mot de passe : tout programme ou compte Windows de l'ordinateur peut joindre
  tvsign. Les Réglages le disent ; choisir un mot de passe si d'autres personnes utilisent l'ordinateur.
- Les secrets des notifications (adresses de webhook, jetons de bot ou d'accès, mots de passe ntfy)
  sont stockés dans la base, jamais renvoyés par l'API (affichés `********`) et masqués dans les
  journaux, le dossier de support et les messages d'erreur. Les notifications sont de simples POST qui
  ne suivent jamais une redirection.
- Les certificats importés (voie avancée et facultative) sont stockés chiffrés et jamais renvoyés par
  l'API ; voir *Signature avec un certificat importé*.

### Ce qui sort de votre machine

- **Apple** (`gsa.apple.com`, `developerservices2.apple.com`) : la connexion de l'Apple ID et les demandes
  de signature, comme avec tout outil qui signe avec un Apple ID gratuit.
- **Le serveur anisette** (`ani.jkcoxson.com`, `ani2.stikstore.app` en repli, ou le vôtre) : un tiers qui
  fournit les données d'appareil qu'Apple demande à la connexion. Le mot de passe de votre Apple ID ne
  lui est pas envoyé.
- **GitHub** (`api.github.com` et les téléchargements de releases) : les apps que vous suivez, et une fois
  par jour une requête pour chercher une nouvelle version de tvsign lui-même (`TVSIGN_UPDATE_CHECK=off`
  ou l'interrupteur des Réglages la coupe). Elle ne porte que `tvsign/<version>` comme User-Agent.
- **Les services de notification que vous ajoutez** (ntfy, Discord, Telegram, Gotify, un webhook) : des
  messages qui citent votre Apple ID et vos apps.
- Rien d'autre : ni statistiques, ni télémétrie, ni compte chez tvsign.

### Configuration

Tout est facultatif. Variables principales : `TZ`, `PUID`, `PGID`, `TVSIGN_PORT` (8765),
`TVSIGN_HOST` (0.0.0.0), `TVSIGN_PASSWORD_FILE`, `TVSIGN_ALLOWED_HOSTS`, `TVSIGN_NTFY_URL`,
`TVSIGN_RENEW_BEFORE_HOURS` (60), `TVSIGN_RENEW_BEFORE_HOURS_MOBILE` (96, iPhone / iPad),
`TVSIGN_PRESENCE_MINUTES` (3, fréquence de recherche d'un téléphone dû ; 0 = jamais),
`TVSIGN_ANISETTE_URL` (vide : `ani.jkcoxson.com`, repli `ani2.stikstore.app`), `TVSIGN_GITHUB_TOKEN`,
`TVSIGN_LANGUAGE`.
Les réglages faits dans l'interface sont enregistrés en base ; une variable d'environnement a
toujours la priorité.

Une app n'est affichée « valide » que si l'Apple TV l'a vraiment : après chaque installation, tvsign
relit la liste des apps de la TV, puis la recontrôle toutes les `TVSIGN_INSTALL_CHECK_HOURS` (6, 0 =
jamais). Une app désinstallée de la TV (ou perdue lors d'une réinitialisation) passe en rouge et est
réinstallée aussitôt. Une TV en veille est simplement revue plus tard. Les renouvellements de TV
différentes s'exécutent en parallèle ; une installation qui attend une app ouverte ne retient que sa TV.

App ouverte sur l'Apple TV : tvsign installe à l'heure prévue et ne cherche pas à deviner si l'app
est ouverte (tvOS garde en mémoire les apps quittées : un processus ne prouve rien). Quand l'app est
vraiment à l'écran, c'est l'Apple TV elle-même qui garde la mise à jour en attente jusqu'à sa
fermeture : la carte de l'app demande alors de la fermer, et tvsign abandonne au bout de
`TVSIGN_APP_WAIT_MINUTES` (30) et réessaie plus tard.

Sources suivies : une release GitHub est réinstallée quand son tag change ou quand son `.ipa` est
remplacé sous le même tag (nouvelle empreinte). Un lien direct est vérifié à chaque renouvellement
(ETag, `Last-Modified` et taille envoyés par le serveur) et retéléchargé si le fichier a changé ; un
serveur qui n'envoie rien de tout cela est suivi en retéléchargeant le fichier au renouvellement
pour comparer son empreinte (la carte de l'app le signale). Les liens doivent être en `https://`,
ne peuvent pas contenir `utilisateur:motdepasse@`, ni viser une adresse privée ou locale (y compris
Tailscale/CGNAT `100.64.0.0/10`) sauf avec `TVSIGN_ALLOW_PRIVATE_URLS=1` ; GitHub, lui, n'est jamais
soumis à ce contrôle. `TVSIGN_GITHUB_TOKEN` relève la limite de GitHub (60 requêtes par heure) et
fait aussi fonctionner les dépôts privés (le jeton n'est envoyé qu'à `api.github.com`). À l'arrêt,
un renouvellement en cours peut finir pendant `TVSIGN_SHUTDOWN_DRAIN_SECONDS` (240) avant d'être
interrompu.

Contrôle des mises à jour, dans les Réglages de chaque app et sur sa carte :

- **Nouvelles versions** : installées automatiquement (par défaut), ou seulement sur demande. Coupé,
  l'app est quand même renouvelée avant son expiration ; la carte affiche « Mise à jour disponible »
  avec les notes de version et un bouton **Installer maintenant**.
- **Version épinglée** (GitHub) : ne suivre qu'un seul tag de release. Un tag qui n'existe pas dans le
  dépôt est signalé sur la carte de l'app (et au journal) ; l'app garde sa version installée.
- **Version précédente** : la dernière IPA installée est gardée ; le bouton la réinstalle et ignore la
  version quittée jusqu'à la prochaine release.
- Si une nouvelle version ne peut pas être signée ou installée, tvsign essaie la précédente et ignore
  la release défectueuse. Une mise à jour qui échoue ne met jamais en rouge une app encore valable :
  la version installée reste valide et la mise à jour est retentée plus tard. Seul un défaut de l'IPA
  elle-même (bundle invalide, droits, paquet refusé par l'Apple TV) compte : limites du compte, disque
  plein, réseau ou app ouverte ne font jamais abandonner une nouvelle version. Une mise à jour
  facultative n'attend une app ouverte que `TVSIGN_UPDATE_WAIT_MINUTES` (5 par défaut, au lieu de 30
  pour un renouvellement).
- Une release qui demande un tvOS plus récent que celui de l'Apple TV (`MinimumOSVersion`) est mise
  de côté, et installée d'elle-même une fois la TV mise à jour ; une notification vous prévient. Ajouter
  une app qui exige un tvOS plus récent que l'Apple TV choisie est refusé.
- Une app importée se met à jour avec **Remplacer l'IPA** (carte, Réglages, ou message « IPA absente »
  après une restauration) : son historique, son Apple TV et son compte sont conservés.

Notifications : Réglages → Notifications contient une liste de destinations : **ntfy**, un
**webhook JSON** générique (Home Assistant, n8n, Node-RED : `title`, `message`, `event`, `priority`,
`time`, `app`), **Discord** (webhook), **Telegram** (jeton de bot + identifiant de conversation) et
**Gotify**. Chaque destination choisit les événements qu'elle reçoit (renouvellement en échec,
expiration proche, renouvellement rétabli, mises à jour des apps, compte Apple et certificats) et a un
bouton **Envoyer un test**. Les messages suivent la langue choisie dans les Réglages (avec
« Automatique », la dernière langue affichée par l'interface) ; les heures portent leur décalage UTC.
`TVSIGN_NTFY_URL` préconfigure toujours une destination ntfy, `TVSIGN_NTFY_EVENTS` ses événements
(séparés par des virgules, `none` pour aucun). Sur ntfy.sh public, le nom du sujet fait office de mot
de passe et les messages citent votre Apple ID et vos apps : choisir un sujet long et aléatoire, ou un
ntfy auto-hébergé. Un serveur protégé s'utilise avec `https://utilisateur:motdepasse@hôte/sujet`
(authentification Basic) ou un jeton d'accès ; un simple nom comme `mon-sujet` vaut
`https://ntfy.sh/mon-sujet`. La destination issue de l'adresse ntfy des réglages (ou de
`TVSIGN_NTFY_URL`) n'a ni nom ni jeton propres : mettre l'identifiant dans l'adresse, ou ajouter une
destination ntfy distincte pour un jeton d'accès. Quel que soit le service (ntfy, Discord, Telegram,
Gotify, webhook), les messages citent votre Apple ID et vos apps et lui sont envoyés : utiliser un
salon ou une conversation privés. Pour Telegram, l'identifiant de conversation est le nombre qui suit
`"chat":{"id":` dans `https://api.telegram.org/bot<jeton>/getUpdates` après avoir écrit à son bot (ou
demander à @userinfobot). Un test échoué indique quoi vérifier (jeton faux, adresse fausse, serveur
injoignable…). L'application Windows affiche aussi une notification Windows.

Journaux : le journal d'accès est coupé (la page interroge le serveur toutes les 5 s) ;
`TVSIGN_ACCESS_LOG=1` le rétablit.

### Dépannage et assistance

- **Diagnostic** (Réglages → Diagnostic) vérifie plumesign, mDNS, Apple, le serveur anisette,
  GitHub, l'horloge et le disque. Un en-tête `Date` invalide renvoyé par un proxy ne le casse pas.
- **Apple TV introuvable / « injoignable ».** L'Apple TV doit être sur le même réseau (VLAN) que tvsign
  et répondre en mDNS. Une Apple TV en Wi-Fi en veille, ou éteinte avec le téléviseur (HDMI-CEC),
  cesse souvent de répondre : préférer l'Ethernet, garder *Réglages › Général › Mise en veille après*
  assez long, désactiver l'extinction CEC la nuit, ou la réveiller à la télécommande avant la fenêtre
  de renouvellement. tvsign réessaie avec un délai croissant (45 minutes au plus, 10 près de l'expiration) et alerte
  à 24 h et 6 h.
- **L'app est sur l'Apple TV mais ne s'ouvre pas.** Son profil a peut-être expiré (voir sa carte), son
  certificat a peut-être été révoqué par un autre outil sur le même Apple ID (tvsign la re-signe au
  contrôle suivant), ou l'installation attend encore parce que l'app est ouverte. Toucher **Renouveler**
  sur sa carte, puis redémarrer l'Apple TV. L'app apparaît sur l'écran d'accueil quelques secondes
  après que l'installation annonce son succès.
- **Erreurs de connexion Apple.** Vérifier l'horloge (Diagnostic), le serveur anisette (celui par
  défaut, `ani.jkcoxson.com`, est partagé et repasse sur `ani2.stikstore.app` : une panne passagère
  est retentée seule) et que le compte
  n'est pas connecté dans un autre tvsign.
- **Signaler un bogue.** Ouvrir un ticket sur <https://github.com/esalwey/tvsign/issues> en joignant
  le **dossier de support** (Réglages → Diagnostic → Assistance → Télécharger) : versions,
  vérifications, apps, journal, historique des renouvellements et dernières sorties de plumesign, avec
  adresses e-mail, adresses IP, identifiants d'appareil et secrets masqués (ni base, ni session Apple,
  ni clé). *Copier les informations de support* en donne la version courte ; *Télécharger le journal*
  (dans le bloc Journal) le journal seul.

Certificats Apple : un Apple ID gratuit n'a droit qu'à 2 certificats de développement, et en révoquer
un empêche les apps signées avec lui de se lancer (Xcode, SideStore, AltStore sur votre iPhone...).
Quand le compte est plein, tvsign ne remplace que les certificats qu'il a créés lui-même (nommés
`tvsign-<id>`, l'id est gardé avec les clés dans `/data`) et les certificats morts ; sinon la
signature s'arrête avec un message « limite de certificats » qui liste les certificats : vous en
révoquez un sur developer.apple.com, ou vous autorisez tvsign, dans Réglages > Certificats, à
révoquer n'importe lequel (`TVSIGN_CERT_REVOKE=any` ; `never` interdit toute révocation). Un
certificat est renouvelé 30 jours avant son expiration et sa date de fin s'affiche sur le compte.
Plusieurs équipes Apple : le bouton Équipe d'un compte choisit l'équipe de signature ; elle est
mémorisée, donc un changement d'ordre chez Apple ne déplace jamais les apps vers une autre équipe
(l'équipe fait partie de l'identifiant de l'app). Plusieurs installations de tvsign (un NAS et un PC)
doivent utiliser des Apple ID différents ; chacune s'appaire à l'Apple TV sous son propre nom
(`tvsign-xxxxxxxx`, ou `TVSIGN_PAIR_HOST`). Un serveur anisette auto-hébergé en `http://` simple est
accepté ; en `https://` avec un certificat auto-signé, il faut `PLUMESIGN_ANISETTE_INSECURE=1`.

### Connexion à Apple : le serveur anisette

La connexion à Apple a besoin de données « anisette », produites par un serveur anisette-v3. Depuis le
12 octobre 2026, Apple refuse les données des serveurs qui les génèrent comme Android (erreur `-80045`
à la connexion, puis pages `HTTP 503`), dont l'ancien serveur par défaut `ani.sidestore.app`. tvsign procède ainsi :

- tvsign utilise `https://ani.jkcoxson.com`, qui fournit des données anisette Windows, et bascule tout
  seul sur `https://ani2.stikstore.app` quand il ne répond pas. **Laissez le champ des Réglages vide**
  pour cela. Le client info et le user agent envoyés à Apple sont ceux que le serveur annonce.
- Un serveur choisi par vos soins (Réglages, ou `TVSIGN_ANISETTE_URL`) est utilisé sans le repli du
  serveur par défaut (plusieurs adresses : voir « Plusieurs serveurs anisette »), et doit lui aussi
  fournir des données Windows : un serveur qui annonce autre chose n'est pas utilisé (tvsign le dit et
  n'envoie rien à Apple, qui répondrait seulement `-80045` ou une page 503). Un serveur Android
  (`ani.sidestore.app`, `ani.stikstore.app`, `anisette.crystall1ne.dev`, `ani3server.fly.dev`,
  `ani.jaydenha.uk`, `omni.ethxn.xyz`) est refusé dans les Réglages, et retiré (remplacé par le serveur
  par défaut s'il n'en reste aucun) avec une ligne dans le journal quand il était enregistré avant ; la
  même valeur dans une variable d'environnement est ignorée.
- L'identité anisette est recréée toute seule (l'ancienne, `state.plist` dans le dossier
  `PlumeImpactor`, est supprimée) et provisionnée de nouveau chaque fois que le serveur ou Apple dit
  qu'elle n'est plus valide.
- Les comptes déjà connectés continuent d'être renouvelés tant qu'Apple accepte leur session. Sinon le
  compte passe à « Déconnecté » avec la raison, et une seule notification part : reconnectez-le (mot de
  passe, puis code à deux facteurs d'un appareil Apple de confiance).

Si la connexion échoue encore :

- **Code à deux facteurs** : Apple envoie souvent le code à vos appareils de confiance avant de le
  demander ; saisissez-le simplement. Les codes par SMS sont pour l'instant refusés par Apple (HTTP
  403) : utilisez un appareil Apple de confiance.
- **`-22411`** : cet Apple ID n'a jamais été connecté sur un appareil Apple. Connectez-le une fois sur
  un iPhone, un iPad, un Mac ou une Apple TV, puis réessayez.
- **`-80045`** avec le serveur par défaut : Apple refuse ce compte ou cette connexion. Ce n'est pas un
  problème de tvsign et insister l'aggrave (Apple limite les essais) : attendez plusieurs heures, depuis
  un autre réseau si possible, puis connectez-vous une seule fois. Les apps déjà installées continuent
  de fonctionner jusqu'à leur expiration.
- **HTTP 503** : les serveurs d'Apple sont chargés (tvsign réessaie une fois, puis une connexion prend les
  données de l'autre serveur anisette), ou le serveur anisette n'est pas accepté : vérifiez que le champ des
  Réglages est vide. Réglages › Diagnostic indique si le serveur anisette répond et fournit des données
  Windows.
- **HTTP 429** : trop de requêtes. N'insistez pas : tvsign attend plusieurs heures avant de réessayer
  seul, et il n'y a pas besoin de reconnecter le compte.

### Comportement des renouvellements

- Les renouvellements tournent en parallèle : une app qui attend l'Apple TV (ou que vous la fermiez)
  ne retarde jamais les autres, les alertes d'expiration ni les contrôles de compte. L'app la plus
  proche de l'expiration démarre toujours en premier.
- Quand Apple refuse une requête d'emblée (`-80045`, HTTP 429), tvsign attend beaucoup plus longtemps
  (`TVSIGN_APPLE_BACKOFF_MINUTES`, 180 par défaut ; 30 min au plus pendant les 12 dernières heures) :
  chaque nouvel essai peut prolonger le blocage d'Apple. Les autres échecs sont réessayés avec un délai
  croissant (10, 20, 40 min… jusqu'à
  `TVSIGN_RETRY_MINUTES`, 45 par défaut ; 5 à 15 min quand la cause est passagère, comme une TV en
  veille ou un serveur anisette en panne ; 10 min au plus pendant les 12 dernières heures). Un échec
  que vous seul pouvez régler (session Apple expirée, appairage perdu, limite du compte gratuit,
  pas d'équipe développeur, tvOS trop ancien, pas d'Apple TV ni d'`.ipa`) affiche **Action requise** sur la carte et n'est pas réessayé à
  l'aveugle ; reconnecter le compte, réappairer, modifier l'app ou toucher **Renouveler** le relance
  aussitôt.
- Une session marquée expirée est revérifiée auprès d'Apple avant de refuser une app, et une
  vérification ratée n'est pas définitive : une panne passagère du réseau ou de l'anisette ne peut
  plus bloquer les renouvellements pendant des heures.
- La carte affiche la version réellement installée sur l'Apple TV (« 1.0 installée · 1.1 en
  attente ») tant que la nouvelle n'est pas installée.
- **Plage horaire des renouvellements** (Réglages) : limite les renouvellements et mises à jour non
  urgents à certaines heures, par exemple de 2 h à 6 h (`TVSIGN_RENEW_WINDOW_START` /
  `TVSIGN_RENEW_WINDOW_END`, heure locale). Elle ne s'applique jamais dans les 24 dernières heures
  avant l'expiration, ni à un renouvellement manuel. Indiquer l'heure de début et l'heure de fin (ou
  aucune des deux) ; début = fin signifie toute la journée. Tant qu'une app attend la plage, sa carte
  affiche « En attente de la plage horaire (dès N h) » et l'ouverture de la plage au lieu d'un délai
  de nouvel essai ; elle n'est pas comptée en retard.
- Réglages de la boucle : `TVSIGN_CHECK_INTERVAL` (600 s, au moins 30), `TVSIGN_RETRY_MINUTES` (45,
  au moins 5), `TVSIGN_ACCOUNT_CHECK_HOURS` (6, au moins 1). Une valeur illisible ou hors limites est
  remplacée par le défaut (ou ramenée à la limite) avec un avertissement dans le journal.

### Limites d'un Apple ID gratuit

Un compte gratuit permet **3 apps actives par Apple TV** et **10 App ID par 7 jours** (chaque
extension, comme une extension TopShelf, en consomme un de plus). Les apps installées avec d'autres
outils (SideStore, AltStore, Xcode) comptent aussi. tvsign affiche « apps actives 2/3 » par Apple TV
sur la carte du compte et prévient à l'ajout d'une quatrième. Quand Apple ou l'Apple TV refuse
(« maximum number of installed apps… »), l'app affiche **Action requise** avec l'explication et
tvsign attend 24 heures au lieu d'épuiser d'autres App ID. Libérez une place (**Pause** d'une app,
ou suppression sur l'Apple TV) puis touchez **Renouveler**.

### Signature avec un certificat importé (avancé)

L'Apple ID gratuit reste la voie par défaut et conseillée. Si vous avez déjà un certificat de
signature et son profil de provisionnement, par exemple ceux de votre propre compte Apple Developer,
une app peut les utiliser à la place : réglages de l'app → **Avancé** → *Méthode* → *Certificat
importé*. tvsign n'en a jamais besoin, et il ne recommande ni ne contacte aucun service de signature.

- Importez un `.p12` (avec son mot de passe) et un `.mobileprovision` **pour l'appareil de l'app**.
  Rien n'est enregistré avant que toutes les vérifications passent : aucun des deux fichiers expiré,
  profil fait pour tvOS (Apple TV) ou iOS (iPhone, iPad), UDID de l'appareil dans le profil (ou profil
  d'entreprise), certificat figurant dans le profil, identifiant de l'app couvert par celui-ci (profil
  à joker comme `*` ou `com.exemple.*`, ou fait pour cette app précisément ; une app avec extensions
  demande un joker). Chaque refus dit ce qui ne va pas.
- Un certificat appartient à un appareil et signe toute app que son profil couvre. Chaque app choisit
  *Apple ID* ou un certificat importé. Les limites du compte gratuit (3 apps par Apple TV…) ne
  comptent pas ces apps.
- **Pas de renouvellement tous les 7 jours.** L'app n'est signée à nouveau que pour installer une
  nouvelle version (ou en touchant **Renouveler**) et reste valable jusqu'à la fin du certificat ou du
  profil, la plus proche des deux. Vous êtes prévenu (notification, icône, journal) **7 jours** et
  **1 jour** avant, puis à la fin ; importer un nouveau certificat avec *Remplace…* signe aussitôt les
  apps à nouveau.
- **Révocation.** Toutes les 12 heures, tvsign interroge le serveur OCSP d'Apple pour les certificats
  que des apps utilisent (`TVSIGN_CERT_CHECK_HOURS`, 0 = jamais ; seul `*.apple.com` est contacté) et
  reconnaît le refus d'une identité révoquée par l'appareil à l'installation. L'app affiche alors
  **Action requise** avec la raison et une alerte part. Un contrôle sans réponse ne conclut rien.
- La clé et le profil sont gardés chiffrés (AES-256-GCM) dans `certs/` du dossier de données, la clé
  dans `vault.key`, lisibles par le seul utilisateur de tvsign ; le `.p12` et son mot de passe ne sont
  pas gardés. **Les sauvegardes les laissent de côté** sauf si vous cochez *Inclure les certificats
  importés et leurs clés privées* à l'export (l'archive contient alors les clés en clair, comme la
  session Apple). Le dossier de support ne les contient jamais.
- Passer une app d'une voie à l'autre l'installe sous un autre identifiant (la voie gratuite y ajoute
  l'équipe) : l'ancien exemplaire reste sur l'appareil jusqu'à ce que vous le supprimiez.

### Plusieurs serveurs anisette

`TVSIGN_ANISETTE_URL` (ou Réglages › Service de signature) accepte plusieurs adresses séparées par
des espaces : si le premier serveur tombe, le suivant prend le relais et passe en premier ensuite (le
serveur par défaut garde son propre repli quand le champ est vide ou le nomme). Chaque serveur doit
fournir des données Windows (voir plus haut). Le diagnostic liste chaque serveur avec son temps de
réponse. Pour héberger le vôtre,
`docker compose --profile anisette up -d` lance un serveur anisette-v3 local
(`dadoum/anisette-v3-server`) sur `127.0.0.1:6969` ; indiquer `http://127.0.0.1:6969` dans le réglage
(réseau hôte : tvsign l'atteint en boucle locale). Ses données vivent dans le volume Docker
`anisette_data` et l'image est figée sur un tag et un digest. Si le serveur ne répond pas, consulter
`docker compose logs anisette`.
Comme tout autre serveur, il doit annoncer des données Windows pour être utilisé : Réglages ›
Diagnostic indique si tvsign l'accepte.

### Mot de passe administrateur oublié

À lancer sur la machine qui héberge tvsign, avec l'utilisateur propriétaire des données (pas root) :

```sh
docker exec -it -u 1000:1000 tvsign python -m app reset-password     # vos PUID:PGID
```

La commande demande un nouveau mot de passe et déconnecte tous les navigateurs. (La fenêtre de
l'édition Windows est de confiance : Réglages › Sécurité change le mot de passe sans demander
l'ancien.) Si le mot de passe vient de `TVSIGN_PASSWORD` ou `TVSIGN_PASSWORD_FILE`, modifier cette valeur et redémarrer.
Un `TVSIGN_PASSWORD_FILE` illisible (mauvais chemin, secret réservé à root alors que tvsign tourne
en `PUID`) est signalé dans le journal et dans le diagnostic : le mot de passe prévu n'est alors
**pas** appliqué.

« Se déconnecter » ne ferme que la session du navigateur utilisé (un cookie copié cesse de
fonctionner) ; **Déconnecter partout** ferme aussi toutes les autres. Les sauvegardes ne contiennent
pas l'empreinte du mot de passe administrateur.

### Identité d'appairage

Chaque installation présente son propre nom d'hôte à l'Apple TV lors de l'appairage (`tvsign-xxxxxxxx`,
choisi une fois et conservé dans le dossier de données) pour que deux instances ne partagent pas
la même identité ; `TVSIGN_PAIR_HOST` le remplace. Les Apple TV déjà appairées gardent le nom avec
lequel elles l'ont été.

Compiler soi-même : voir [docs/building.md](docs/building.md).

---

License: MIT, see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
