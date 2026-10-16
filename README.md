<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/assets/banner-dark.svg">
  <img alt="tvsign: your Apple TV apps, signed and renewed before day 7" src=".github/assets/banner-light.svg" width="100%">
</picture>

<p align="center">
  <img alt="Docker amd64 / arm64" src="https://img.shields.io/badge/docker-amd64%20%C2%B7%20arm64-2f6bff?style=flat-square&logo=docker&logoColor=white">
  <img alt="Windows 10 / 11" src="https://img.shields.io/badge/windows-10%20%C2%B7%2011-111318?style=flat-square">
  <img alt="tvOS 26+" src="https://img.shields.io/badge/tvOS-26%2B-000?style=flat-square&logo=apple">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-555?style=flat-square"></a>
</p>

[English](#english) · [Français](#français)

---

## English

### What it is

tvsign keeps sideloaded tvOS apps alive on your Apple TVs. Apps signed with a free Apple ID expire
after 7 days; tvsign re-signs them with your own Apple ID and reinstalls them over Wi-Fi/Ethernet
before they expire, and installs new releases of the apps you follow (GitHub releases, a link, or
an uploaded `.ipa`). Everything runs on your own machine, managed from a small web interface.

Two editions share the same code:

- **Self-host (this page):** a Docker container for an always-on Linux box: NAS, Raspberry Pi,
  home server.
- **Windows desktop:** a small window app with a tray icon, for a PC that stays on.

Run one tvsign per Apple ID. Two instances on the same free account revoke each other's
certificates.

tvsign is not affiliated with Apple. Each user signs apps with their own Apple ID; tvsign does not
host or distribute any app content.

### Requirements

- A Linux host, **amd64 or arm64** (Raspberry Pi needs a 64-bit OS). Docker Engine with Compose v2.
- **The same local network (same L2 segment / VLAN) as the Apple TV.** tvsign finds Apple TVs with
  mDNS/Bonjour (`_remotepairing._tcp`, UDP 5353), which does not cross routers or VLANs. Across VLANs
  you need an mDNS reflector (UniFi "Multicast DNS", `avahi-daemon` with `enable-reflector=yes`).
- `network_mode: host` (multicast does not reach a bridge network).
- Outgoing Internet access: Apple (`gsa.apple.com`, `developerservices2.apple.com`), the anisette
  server (`ani.jkcoxson.com` by default, `ani2.stikstore.app` as automatic fallback, changeable in
  Settings), `api.github.com` for releases.
- A correct clock (NTP).
- Not supported: Docker Desktop on Windows/macOS (use the Windows edition), 32-bit ARM, TrueNAS CORE.

No `NET_ADMIN`, no `/dev/net/tun`, no IPv6 and no root are needed: the encrypted tunnel to the
Apple TV runs entirely in user space inside `plumesign`. The server runs as an unprivileged user.

### Install

Check the host, then install into `/opt/tvsign`:

```sh
sh install.sh              # checks only, changes nothing
sh install.sh --install    # writes /opt/tvsign/docker-compose.yml and starts tvsign
```

Or by hand with [`docker-compose.yml`](docker-compose.yml):

```sh
mkdir tvsign && cd tvsign      # put docker-compose.yml here
docker compose up -d
docker compose logs tvsign     # the one-time setup code is printed here
```

Set `PUID`/`PGID` to the owner of the data folder (`id -u`, `id -g`), and `TZ` to your time zone.

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
   sign-in (a dedicated free Apple ID is recommended; the password is never stored), Apple TV pairing
   (on the TV: Settings › Remotes and Devices › Remote App and Devices, then type the PIN), and the
   app to keep installed.

The Diagnostics page re-runs the environment checks at any time.

### Backups

`/data` (`./tvsign-data` on the host) holds the Apple session, the development certificate's
private key, the Apple TV pairings and the database. **It is secret**: back it up, never share it,
never put it in a synced or network-shared folder in clear.

- Settings → Backup → **Export** downloads a `.tvsign-backup` archive (database, Apple sessions,
  keys, pairings; uploaded IPAs optional). Restore it from Settings or from the first-run wizard.
  The archive moves between the Docker and Windows editions without re-pairing the Apple TV.
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
progress finish before the container stops. Watchtower or Diun work but are optional.

tvsign looks once a day for a newer release of itself (one request to the GitHub API, nothing else
is sent) and shows a dismissible banner with the right instruction for your edition. It never
updates itself. Turn it off in Settings (“Check for tvsign updates”) or with `TVSIGN_UPDATE_CHECK=off`.

### Windows

1. Download `tvsign-setup-<version>.exe` from the Releases page of this repository and run it. It
   installs for your Windows account only, no administrator password. If Windows says *Windows
   protected your PC*, click **More info → Run anyway**: the installer is not code-signed yet.
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
  never on a command line, and is not stored.

### Configuration

Everything is optional. The main variables: `TZ`, `PUID`, `PGID`, `TVSIGN_PORT` (8765),
`TVSIGN_HOST` (0.0.0.0), `TVSIGN_PASSWORD_FILE`, `TVSIGN_ALLOWED_HOSTS`, `TVSIGN_NTFY_URL`,
`TVSIGN_RENEW_BEFORE_HOURS` (60), `TVSIGN_ANISETTE_URL` (empty: `ani.jkcoxson.com`, fallback
`ani2.stikstore.app`), `TVSIGN_GITHUB_TOKEN`, `TVSIGN_LANGUAGE`.
Settings set in the web UI are stored in the database; an environment variable always wins.

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
- **Pinned version** (GitHub): follow one release tag only.
- **Previous version**: the last installed IPA is kept; the button reinstalls it and skips the
  version you left until another release comes out.
- If a new version cannot be signed or installed, tvsign tries the previous one and skips the
  broken release. An update that fails never turns a valid app red: the installed version stays
  valid and the update is retried later.
- A release that needs a newer tvOS than your Apple TV runs (`MinimumOSVersion`) is held back, and
  installed by itself once the TV is updated.
- An uploaded app is updated with **Replace IPA** (card, Settings, or the “IPA missing” message after
  a restore): its history, Apple TV and account are kept.

Notifications (ntfy): the topic name acts as a password on the public ntfy.sh, and messages name
your Apple ID and your apps: pick a long random topic, or use a self-hosted ntfy. A protected
server can be reached with `https://user:password@host/topic` (sent as Basic authentication).

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
`HTTP 503` pages), which includes the old default `ani.sidestore.app`. From version 1.0.1:

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

Sign out only ends the session of the browser you click it in (a copied cookie stops working, including one issued before this version);
**Sign out everywhere** also ends all the others. Backups no longer contain the admin password hash.

### Pairing identity

Each installation presents its own host name to the Apple TV when pairing (`tvsign-xxxxxxxx`, picked
once and kept in the data folder) so that two instances do not share one identity; override
with `TVSIGN_PAIR_HOST`. Apple TVs already paired keep the name they were paired with.

Building from source: see [docs/building.md](docs/building.md).

---

## Français

### Ce que c'est

tvsign garde en vie les apps tvOS installées hors App Store sur vos Apple TV. Une app signée avec un
Apple ID gratuit expire au bout de 7 jours ; tvsign la re-signe avec votre propre Apple ID et la
réinstalle par le réseau avant l'expiration, et installe les nouvelles versions des apps suivies
(releases GitHub, lien, ou `.ipa` envoyé). Tout tourne chez vous, piloté par une petite interface web.

Deux éditions, même code :

- **Auto-hébergée (cette page)** : un conteneur Docker pour une machine Linux allumée en
  permanence : NAS, Raspberry Pi, serveur maison.
- **Windows** : une petite application fenêtrée avec icône dans la zone de notification, pour un PC
  qui reste allumé.

Un seul tvsign par Apple ID : deux instances sur le même compte gratuit révoquent mutuellement
leurs certificats.

tvsign n'est pas affilié à Apple. Chacun signe les apps avec son propre Apple ID ; tvsign
n'héberge ni ne distribue aucun contenu.

### Prérequis

- Un hôte Linux **amd64 ou arm64** (Raspberry Pi : OS 64 bits). Docker Engine avec Compose v2.
- **Le même réseau local (même segment L2 / VLAN) que l'Apple TV.** tvsign trouve les Apple TV par
  mDNS/Bonjour (`_remotepairing._tcp`, UDP 5353), qui ne traverse ni routeur ni VLAN. Entre VLAN,
  il faut un réflecteur mDNS (UniFi « Multicast DNS », `avahi-daemon` avec `enable-reflector=yes`).
- `network_mode: host` (le multicast n'atteint pas un réseau bridge).
- Accès Internet sortant : Apple (`gsa.apple.com`, `developerservices2.apple.com`), le serveur
  anisette (`ani.jkcoxson.com` par défaut, `ani2.stikstore.app` en repli automatique, modifiable dans
  les Réglages), `api.github.com`.
- Une horloge à l'heure (NTP).
- Non pris en charge : Docker Desktop sous Windows/macOS (prendre l'édition Windows), ARM 32 bits,
  TrueNAS CORE.

Ni `NET_ADMIN`, ni `/dev/net/tun`, ni IPv6, ni root : le tunnel chiffré vers l'Apple TV tourne
entièrement en espace utilisateur dans `plumesign`. Le serveur tourne sous un utilisateur non privilégié.

### Installation

Vérifier l'hôte, puis installer dans `/opt/tvsign` :

```sh
sh install.sh              # vérifications seulement, ne modifie rien
sh install.sh --install    # écrit /opt/tvsign/docker-compose.yml et démarre tvsign
```

Ou à la main avec [`docker-compose.yml`](docker-compose.yml) :

```sh
mkdir tvsign && cd tvsign      # y placer docker-compose.yml
docker compose up -d
docker compose logs tvsign     # le code d'installation à usage unique s'affiche ici
```

Mettre `PUID`/`PGID` au propriétaire du dossier de données (`id -u`, `id -g`) et `TZ` à votre fuseau.

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
   enregistré), appairage de l'Apple TV (sur la TV : Réglages › Télécommandes et appareils › App
   Télécommande et appareils, puis saisir le code), et l'app à garder installée.

La page Diagnostic relance les vérifications à tout moment.

### Sauvegardes

`/data` (`./tvsign-data` sur l'hôte) contient la session Apple, la clé privée du certificat de
développement, les appairages des Apple TV et la base. **C'est secret** : à sauvegarder, à ne jamais
partager, à ne jamais mettre en clair dans un dossier synchronisé ou partagé sur le réseau.

- Réglages → Sauvegarde → **Exporter** télécharge une archive `.tvsign-backup` (base, sessions
  Apple, clés, appairages ; IPA envoyées en option). Elle se restaure depuis les Réglages ou depuis
  l'assistant du premier lancement, et passe d'une édition à l'autre (Docker ↔ Windows) sans
  réappairer l'Apple TV.
- Ou sauvegarder tout le dossier de données, conteneur arrêté (Hyper Backup, Appdata Backup, restic…).
- Après un déménagement, ne jamais faire tourner l'ancienne et la nouvelle instance en même temps.

### Mises à jour

```sh
docker compose pull && docker compose up -d
```

Tags : `:1` (conseillé, reste en version majeure 1), `:1.2`, `:1.2.3`, `:latest`, `:edge` (branche
main, pour les testeurs). `:1` ne passe jamais à une autre version majeure : pour aller en 2.x, change le tag
dans `docker-compose.yml` (`ghcr.io/esalwey/tvsign:2`) avant le pull. La base est copiée avant tout changement de schéma. Éviter de mettre à jour
pendant un renouvellement : `stop_grace_period: 5m` et `TVSIGN_SHUTDOWN_DRAIN_SECONDS` (240) laissent
finir une installation en cours avant l'arrêt du conteneur. Watchtower ou Diun fonctionnent mais
restent facultatifs.

tvsign cherche une fois par jour s'il existe une version plus récente de lui-même (une requête à
l'API GitHub, rien d'autre n'est envoyé) et affiche une bannière qu'on peut écarter, avec la bonne
consigne pour votre édition. Il ne se met jamais à jour tout seul. Désactivable dans les Réglages
(« Chercher les mises à jour de tvsign ») ou avec `TVSIGN_UPDATE_CHECK=off`.

### Windows

1. Télécharger `tvsign-setup-<version>.exe` depuis la page Releases de ce dépôt et le lancer. Il
   s'installe pour votre seul compte Windows, sans mot de passe administrateur. Si Windows affiche
   *Windows a protégé votre ordinateur*, cliquer sur **Informations complémentaires → Exécuter
   quand même** : l'installeur n'est pas encore signé numériquement.
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
  environnement, jamais en ligne de commande, et n'est pas enregistré.

### Configuration

Tout est facultatif. Variables principales : `TZ`, `PUID`, `PGID`, `TVSIGN_PORT` (8765),
`TVSIGN_HOST` (0.0.0.0), `TVSIGN_PASSWORD_FILE`, `TVSIGN_ALLOWED_HOSTS`, `TVSIGN_NTFY_URL`,
`TVSIGN_RENEW_BEFORE_HOURS` (60), `TVSIGN_ANISETTE_URL` (vide : `ani.jkcoxson.com`, repli
`ani2.stikstore.app`), `TVSIGN_GITHUB_TOKEN`, `TVSIGN_LANGUAGE`.
Les réglages faits dans l'interface sont enregistrés en base ; une variable d'environnement a
toujours la priorité.

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
- **Version épinglée** (GitHub) : ne suivre qu'un seul tag de release.
- **Version précédente** : la dernière IPA installée est gardée ; le bouton la réinstalle et ignore la
  version quittée jusqu'à la prochaine release.
- Si une nouvelle version ne peut pas être signée ou installée, tvsign essaie la précédente et ignore
  la release défectueuse. Une mise à jour qui échoue ne met jamais en rouge une app encore valable :
  la version installée reste valide et la mise à jour est retentée plus tard.
- Une release qui demande un tvOS plus récent que celui de l'Apple TV (`MinimumOSVersion`) est mise
  de côté, et installée d'elle-même une fois la TV mise à jour.
- Une app importée se met à jour avec **Remplacer l'IPA** (carte, Réglages, ou message « IPA absente »
  après une restauration) : son historique, son Apple TV et son compte sont conservés.

Notifications (ntfy) : sur ntfy.sh public, le nom du sujet fait office de mot de passe, et les messages
citent votre Apple ID et vos apps : choisir un sujet long et aléatoire, ou un ntfy auto-hébergé. Un
serveur protégé s'utilise avec `https://utilisateur:motdepasse@hôte/sujet` (envoyé en authentification Basic).

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
à la connexion, puis pages `HTTP 503`), dont l'ancien serveur par défaut `ani.sidestore.app`. À partir
de la version 1.0.1 :

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
fonctionner, y compris s'il date d'avant cette version) ; **Se déconnecter partout** ferme aussi toutes les autres. Les sauvegardes ne
contiennent plus l'empreinte du mot de passe administrateur.

### Identité d'appairage

Chaque installation présente son propre nom d'hôte à l'Apple TV lors de l'appairage (`tvsign-xxxxxxxx`,
choisi une fois et conservé dans le dossier de données) pour que deux instances ne partagent pas
la même identité ; `TVSIGN_PAIR_HOST` le remplace. Les Apple TV déjà appairées gardent le nom avec
lequel elles l'ont été.

Compiler soi-même : voir [docs/building.md](docs/building.md).

---

License: MIT, see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
