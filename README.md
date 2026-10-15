<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/assets/banner-dark.svg">
  <img alt="tvsign: your Apple TV apps, signed and renewed before day 7" src=".github/assets/banner-light.svg" width="100%">
</picture>

<p align="center">
  <img alt="Docker amd64 / arm64" src="https://img.shields.io/badge/docker-amd64%20%C2%B7%20arm64-2f6bff?style=flat-square&logo=docker&logoColor=white">
  <img alt="Windows 10 / 11" src="https://img.shields.io/badge/windows-10%20%C2%B7%2011-111318?style=flat-square">
  <img alt="tvOS 26+" src="https://img.shields.io/badge/tvOS-26%2B-000?style=flat-square&logo=apple">
  <img alt="iOS / iPadOS 17+" src="https://img.shields.io/badge/iOS%20%C2%B7%20iPadOS-17%2B-000?style=flat-square&logo=apple">
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
iPhones and iPads (iOS / iPadOS 17+) work the same way: see [iPhone and iPad](#iphone-and-ipad).

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
   sign-in (a dedicated free Apple ID is recommended; the password is never stored), device pairing
   (Apple TV: Settings › Remotes and Devices › Remote App and Devices, then type the PIN; iPhone / iPad:
   see below), and the app to keep installed.

The Diagnostics page re-runs the environment checks at any time.

### iPhone and iPad

iPhones and iPads (**iOS / iPadOS 17 or later**) are kept alive like an Apple TV: tvsign signs with
your Apple ID and reinstalls over Wi-Fi before the 7 days are up. **Status: new. The renewal path is
the Apple TV one (same tunnel, same installer), but the one-time USB pairing step has not been
validated on every device and iOS release yet: treat it as experimental and report what you see.**

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
`TVSIGN_RENEW_BEFORE_HOURS` (60), `TVSIGN_RENEW_BEFORE_HOURS_MOBILE` (96, iPhone / iPad),
`TVSIGN_PRESENCE_MINUTES` (3, how often a due phone is looked for; 0 = off), `TVSIGN_ANISETTE_URL`,
`TVSIGN_GITHUB_TOKEN`, `TVSIGN_LANGUAGE`.
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

Notifications (ntfy): the topic name acts as a password on the public ntfy.sh, and messages name
your Apple ID and your apps: pick a long random topic, or use a self-hosted ntfy. A protected
server can be reached with `https://user:password@host/topic` (sent as Basic authentication).

Building from source: see [docs/building.md](docs/building.md).

---

## Français

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
   enregistré), appairage de l'appareil (Apple TV : Réglages › Télécommandes et appareils › App
   Télécommande et appareils, puis saisir le code ; iPhone / iPad : voir plus bas), et l'app à garder
   installée.

La page Diagnostic relance les vérifications à tout moment.

### iPhone et iPad

Les iPhone et iPad (**iOS / iPadOS 17 ou plus récent**) sont gardés en vie comme une Apple TV :
tvsign signe avec votre Apple ID et réinstalle par le Wi-Fi avant la fin des 7 jours. **État :
nouveau. Le renouvellement est celui de l'Apple TV (même tunnel, même installateur), mais
l'appairage USB initial n'a pas encore été validé sur tous les appareils et versions d'iOS :
considérez-le comme expérimental et signalez ce que vous constatez.**

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
`TVSIGN_RENEW_BEFORE_HOURS` (60), `TVSIGN_RENEW_BEFORE_HOURS_MOBILE` (96, iPhone / iPad),
`TVSIGN_PRESENCE_MINUTES` (3, fréquence de recherche d'un téléphone dû ; 0 = jamais),
`TVSIGN_ANISETTE_URL`, `TVSIGN_GITHUB_TOKEN`, `TVSIGN_LANGUAGE`.
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

Notifications (ntfy) : sur ntfy.sh public, le nom du sujet fait office de mot de passe, et les messages
citent votre Apple ID et vos apps : choisir un sujet long et aléatoire, ou un ntfy auto-hébergé. Un
serveur protégé s'utilise avec `https://utilisateur:motdepasse@hôte/sujet` (envoyé en authentification Basic).

Compiler soi-même : voir [docs/building.md](docs/building.md).

---

License: MIT, see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
