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
  server (`ani.sidestore.app` by default, changeable in Settings), `api.github.com` for releases.
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
- Restoring checks the archive first (integrity, tables and columns of the database, migration to
  the current schema) and only then replaces anything. The replacement is all-or-nothing: if a step
  fails (file locked, disk full), the steps already done are undone and the instance stays as it was.
  Renewals and the web interface pause while it runs. The previous state is kept aside for 30 days.
- The archive is **not encrypted**. On Windows the browser's Downloads folder is often synced by
  OneDrive: choose a folder that is not, or move the file to a safe place right away.
- Or back up the whole data folder with the container stopped (Hyper Backup, Appdata Backup, restic…).
- Never run the old and the new instance at the same time after a move.

### Updates

```sh
docker compose pull && docker compose up -d
```

Tags: `:1` (recommended, stays on major version 1), `:1.2`, `:1.2.3`, `:latest`, `:edge` (main
branch, for testers). The database is copied before any schema change. Avoid updating during a
renewal: `stop_grace_period: 5m` and `TVSIGN_SHUTDOWN_DRAIN_SECONDS` (240) let an install in
progress finish before the container stops. Watchtower or Diun work but are optional.

**New versions of your apps.** A followed GitHub release is downloaded, signed and installed when it
is published. Safety nets:

- *tvOS check.* The app's `MinimumOSVersion` is compared with the tvOS of the Apple TV. Adding an
  app that needs a newer tvOS is refused ("needs tvOS 26.0, your Apple TV runs 18.4"); a new release
  that needs one is skipped, the installed version keeps being renewed, and you get a notification.
  Updating the Apple TV lifts the refusal.
- *Automatic rollback.* tvsign keeps the previous version next to the new one until the new one is
  installed successfully. If the new version fails to sign or install twice for a reason that is not
  a network hiccup (new entitlement, App ID limit…), tvsign goes back to the previous version,
  skips the bad release and notifies you.
- *Not at any price.* A new version that cannot be installed right now (app open, Apple TV asleep,
  anisette down) never turns a valid app red and sends no failure alert: it waits (an optional update
  waits `TVSIGN_UPDATE_WAIT_MINUTES`, 5, for an open app, not 30). Alerts start when expiry nears.
- *Your choice.* In an app's Settings, turn off "Install new versions automatically": tvsign keeps
  renewing the installed version, shows "update available" with the release notes and notifies you;
  **Install now** installs it. **Roll back** goes back to the previous version and turns automatic
  updates off for that app (if the Apple TV refuses an older version, delete the app on it first).

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
  never on a command line, and is not stored. **Signing may revoke the development certificate of
  that Apple ID**: apps that AltStore, SideStore, Sideloadly or Xcode signed with it (on an iPhone,
  say) stop launching. The sign-in sheet asks you to confirm this, and a banner stays on the main
  page after a revocation.
- Desktop edition without a password: any program or Windows account on that computer can reach
  tvsign. Settings says so; set a password if others use the computer.
- Notification secrets (webhook addresses, bot and access tokens, ntfy passwords) are stored in the
  database, are never sent back by the API (shown as `********`) and are masked in logs, the support
  bundle and error messages. Notifications are plain POSTs that never follow redirects.

### Configuration

Everything is optional. The main variables: `TZ`, `PUID`, `PGID`, `TVSIGN_PORT` (8765),
`TVSIGN_HOST` (0.0.0.0), `TVSIGN_PASSWORD_FILE`, `TVSIGN_ALLOWED_HOSTS`, `TVSIGN_NTFY_URL`,
`TVSIGN_RENEW_BEFORE_HOURS` (60), `TVSIGN_ANISETTE_URL`, `TVSIGN_GITHUB_TOKEN`, `TVSIGN_LANGUAGE`.
Settings set in the web UI are stored in the database; an environment variable always wins.

App open on the Apple TV: tvsign installs on schedule and never tries to guess whether the app is
open (tvOS keeps closed apps in memory, so a running process says nothing). When the app really is
on screen, the Apple TV itself holds the update until it is closed: the app card then asks to close
the app, and tvsign gives up after `TVSIGN_APP_WAIT_MINUTES` (30) and retries later.

Followed sources: a GitHub release is reinstalled when its tag changes or when its `.ipa` is
replaced under the same tag (new checksum). A direct link is checked at each renewal (ETag,
`Last-Modified` and size sent by the server) and downloaded again when the file changed; a server
that sends none of them cannot be followed: the first download is kept. Shutdown: a renewal in
progress may finish for up to `TVSIGN_SHUTDOWN_DRAIN_SECONDS` (240) before it is interrupted.

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
token; a bare name such as `my-topic` means `https://ntfy.sh/my-topic`. The Windows desktop app also
raises a Windows notification.

Logs: the access log is off (the page polls every 5 s); `TVSIGN_ACCESS_LOG=1` turns it on.

### Troubleshooting and support

- **Diagnostics** (Settings → Diagnostics) checks plumesign, mDNS, Apple, the anisette server,
  GitHub, the clock and the disk. A bad `Date` header from a proxy does not break it.
- **Apple TV not found / "unreachable".** The Apple TV must be on the same network (VLAN) as tvsign
  and answer mDNS. A Wi-Fi Apple TV in standby, or one switched off with the television (HDMI-CEC),
  often stops answering: prefer Ethernet, keep *Settings › General › Sleep After* long enough, turn
  CEC power-off off at night, or wake it with the remote before the renewal window. tvsign retries
  every 45 minutes (10 near expiry) and alerts at 24 h and 6 h.
- **The app is on the Apple TV but does not open.** Its profile may have expired (check its card),
  its certificate may have been revoked by another tool on the same Apple ID (tvsign re-signs it at
  the next check), or the installation may still be waiting because the app is open. Try *Renew
  now*, then restart the Apple TV. The app appears in the home screen a few seconds after the
  installation reports success.
- **Apple sign-in errors.** Check the clock (Diagnostics), the anisette server (the default,
  `ani.sidestore.app`, is shared: a temporary outage is retried automatically), and that the
  account is not signed in on another tvsign.
- **Reporting a bug.** Open an issue at <https://github.com/esalwey/tvsign/issues> and attach the
  **support bundle** (Settings → Diagnostics → Support → Download): versions, checks, apps, journal,
  renewal history and the last plumesign outputs, with e-mail addresses, IP addresses, device
  identifiers and secrets masked (it holds no database, Apple session or key). *Copy support info*
  gives the short text version; *Download the log* (in the Log block) the journal alone.

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
  anisette (`ani.sidestore.app` par défaut, modifiable dans les Réglages), `api.github.com`.
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
- La restauration contrôle d'abord l'archive (intégrité, tables et colonnes de la base, migration
  vers le schéma actuel) et ne remplace rien avant. Le remplacement est tout ou rien : si une étape
  échoue (fichier verrouillé, disque plein), celles déjà faites sont annulées et l'instance reste
  comme elle était. Les renouvellements et l'interface se mettent en pause pendant ce temps.
  L'état précédent est gardé à côté pendant 30 jours.
- L'archive **n'est pas chiffrée**. Sous Windows, le dossier Téléchargements du navigateur est souvent
  synchronisé par OneDrive : choisir un dossier qui ne l'est pas, ou déplacer le fichier aussitôt.
- Ou sauvegarder tout le dossier de données, conteneur arrêté (Hyper Backup, Appdata Backup, restic…).
- Après un déménagement, ne jamais faire tourner l'ancienne et la nouvelle instance en même temps.

### Mises à jour

```sh
docker compose pull && docker compose up -d
```

Tags : `:1` (conseillé, reste en version majeure 1), `:1.2`, `:1.2.3`, `:latest`, `:edge` (branche
main, pour les testeurs). La base est copiée avant tout changement de schéma. Éviter de mettre à jour
pendant un renouvellement : `stop_grace_period: 5m` et `TVSIGN_SHUTDOWN_DRAIN_SECONDS` (240) laissent
finir une installation en cours avant l'arrêt du conteneur. Watchtower ou Diun fonctionnent mais
restent facultatifs.

**Nouvelles versions de vos apps.** Une release GitHub suivie est téléchargée, signée et installée
dès sa publication. Garde-fous :

- *Contrôle de la tvOS.* Le `MinimumOSVersion` de l'app est comparé à la tvOS de l'Apple TV. Ajouter
  une app qui exige une tvOS plus récente est refusé (« exige tvOS 26.0, ton Apple TV est en 18.4 ») ;
  une nouvelle release qui en exige une est écartée, la version installée continue d'être renouvelée,
  et une notification vous prévient. Mettre l'Apple TV à jour lève le refus.
- *Retour arrière automatique.* tvsign garde la version précédente à côté de la nouvelle jusqu'à ce
  que celle-ci soit installée avec succès. Si la nouvelle échoue deux fois à se signer ou s'installer
  pour une raison qui n'est pas une panne réseau (nouveau droit, limite d'App ID…), tvsign revient à
  la précédente, écarte la mauvaise release et vous prévient.
- *Pas à n'importe quel prix.* Une nouvelle version impossible à installer sur le moment (app ouverte,
  Apple TV en veille, anisette en panne) ne passe jamais une app valide au rouge et n'envoie aucune
  alerte d'échec : elle attend (une mise à jour facultative n'attend une app ouverte que
  `TVSIGN_UPDATE_WAIT_MINUTES`, 5, pas 30). Les alertes commencent quand l'expiration approche.
- *Votre choix.* Dans les Réglages d'une app, décocher « Installer les nouvelles versions
  automatiquement » : tvsign continue de renouveler la version installée, affiche « mise à jour
  disponible » avec les notes de version et vous prévient ; **Installer maintenant** l'installe.
  **Revenir** retourne à la version précédente et coupe les mises à jour automatiques de cette app
  (si l'Apple TV refuse une version plus ancienne, supprimer d'abord l'app dessus).

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
  environnement, jamais en ligne de commande, et n'est pas enregistré. **La signature peut révoquer
  le certificat de développement de cet Apple ID** : les apps qu'AltStore, SideStore, Sideloadly ou
  Xcode ont signées avec lui (sur un iPhone par exemple) cessent de se lancer. La feuille de
  connexion demande de le confirmer, et un bandeau reste affiché sur la page principale après une
  révocation.
- Édition bureau sans mot de passe : tout programme ou compte Windows de l'ordinateur peut joindre
  tvsign. Les Réglages le disent ; choisir un mot de passe si d'autres personnes utilisent l'ordinateur.
- Les secrets des notifications (adresses de webhook, jetons de bot ou d'accès, mots de passe ntfy)
  sont stockés dans la base, jamais renvoyés par l'API (affichés `********`) et masqués dans les
  journaux, le dossier de support et les messages d'erreur. Les notifications sont de simples POST qui
  ne suivent jamais une redirection.

### Configuration

Tout est facultatif. Variables principales : `TZ`, `PUID`, `PGID`, `TVSIGN_PORT` (8765),
`TVSIGN_HOST` (0.0.0.0), `TVSIGN_PASSWORD_FILE`, `TVSIGN_ALLOWED_HOSTS`, `TVSIGN_NTFY_URL`,
`TVSIGN_RENEW_BEFORE_HOURS` (60), `TVSIGN_ANISETTE_URL`, `TVSIGN_GITHUB_TOKEN`, `TVSIGN_LANGUAGE`.
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
serveur qui n'envoie rien de tout cela ne peut pas être suivi : le premier téléchargement est
conservé. À l'arrêt, un renouvellement en cours peut finir pendant `TVSIGN_SHUTDOWN_DRAIN_SECONDS`
(240) avant d'être interrompu.

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
`https://ntfy.sh/mon-sujet`. L'application Windows affiche aussi une notification Windows.

Journaux : le journal d'accès est coupé (la page interroge le serveur toutes les 5 s) ;
`TVSIGN_ACCESS_LOG=1` le rétablit.

### Dépannage et assistance

- **Diagnostic** (Réglages → Diagnostic) vérifie plumesign, mDNS, Apple, le serveur anisette,
  GitHub, l'horloge et le disque. Un en-tête `Date` invalide renvoyé par un proxy ne le casse pas.
- **Apple TV introuvable / « injoignable ».** L'Apple TV doit être sur le même réseau (VLAN) que tvsign
  et répondre en mDNS. Une Apple TV en Wi-Fi en veille, ou éteinte avec le téléviseur (HDMI-CEC),
  cesse souvent de répondre : préférer l'Ethernet, garder *Réglages › Général › Mise en veille après*
  assez long, désactiver l'extinction CEC la nuit, ou la réveiller à la télécommande avant la fenêtre
  de renouvellement. tvsign réessaie toutes les 45 minutes (10 près de l'expiration) et alerte à 24 h
  et 6 h.
- **L'app est sur l'Apple TV mais ne s'ouvre pas.** Son profil a peut-être expiré (voir sa carte), son
  certificat a peut-être été révoqué par un autre outil sur le même Apple ID (tvsign la re-signe au
  contrôle suivant), ou l'installation attend encore parce que l'app est ouverte. Essayer *Renouveler
  maintenant*, puis redémarrer l'Apple TV. L'app apparaît sur l'écran d'accueil quelques secondes
  après que l'installation annonce son succès.
- **Erreurs de connexion Apple.** Vérifier l'horloge (Diagnostic), le serveur anisette (celui par
  défaut, `ani.sidestore.app`, est partagé : une panne passagère est retentée seule) et que le compte
  n'est pas connecté dans un autre tvsign.
- **Signaler un bogue.** Ouvrir un ticket sur <https://github.com/esalwey/tvsign/issues> en joignant
  le **dossier de support** (Réglages → Diagnostic → Assistance → Télécharger) : versions,
  vérifications, apps, journal, historique des renouvellements et dernières sorties de plumesign, avec
  adresses e-mail, adresses IP, identifiants d'appareil et secrets masqués (ni base, ni session Apple,
  ni clé). *Copier les informations de support* en donne la version courte ; *Télécharger le journal*
  (dans le bloc Journal) le journal seul.

Compiler soi-même : voir [docs/building.md](docs/building.md).

---

License: MIT, see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
