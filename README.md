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

Notifications (ntfy): the topic name acts as a password on the public ntfy.sh, and messages name
your Apple ID and your apps: pick a long random topic, or use a self-hosted ntfy. A protected
server can be reached with `https://user:password@host/topic` (sent as Basic authentication).

### How renewals behave

- Renewals run side by side: an app waiting for the Apple TV (or for you to close it) never delays
  the others, the expiry alerts or the account checks. The most urgent app is always started first.
- After a failure tvsign retries with a growing delay (10, 20, 40 min… up to `TVSIGN_RETRY_MINUTES`,
  45 by default; 5 to 15 min when the cause is passing, such as the TV asleep or the anisette server
  down; 10 min at most in the last 12 hours). A failure only you can fix (Apple session expired,
  pairing lost, free-account limit, no Apple TV or `.ipa`) shows **Action needed** on the card and
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
if the first server is down, the next one takes over and is tried first from then on. Diagnostics
lists each server with its response time. To run your own, `docker compose --profile anisette up -d`
starts a local anisette-v3 server (`dadoum/anisette-v3-server`) on `127.0.0.1:6969`; put
`http://127.0.0.1:6969` in the setting (host network: tvsign reaches it on loopback). Its data lives
in the `anisette_data` Docker volume and the image is pinned to a tag and digest. If the server does
not answer, check `docker compose logs anisette`.

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

Each installation presents its own host name to the Apple TV when pairing (`tvsign-xxxx`, picked once
and kept with the database and backups) so that two instances do not share one identity; override
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

Notifications (ntfy) : sur ntfy.sh public, le nom du sujet fait office de mot de passe, et les messages
citent votre Apple ID et vos apps : choisir un sujet long et aléatoire, ou un ntfy auto-hébergé. Un
serveur protégé s'utilise avec `https://utilisateur:motdepasse@hôte/sujet` (envoyé en authentification Basic).

### Comportement des renouvellements

- Les renouvellements tournent en parallèle : une app qui attend l'Apple TV (ou que vous la fermiez)
  ne retarde jamais les autres, les alertes d'expiration ni les contrôles de compte. L'app la plus
  proche de l'expiration démarre toujours en premier.
- Après un échec, tvsign réessaie avec un délai croissant (10, 20, 40 min… jusqu'à
  `TVSIGN_RETRY_MINUTES`, 45 par défaut ; 5 à 15 min quand la cause est passagère, comme une TV en
  veille ou un serveur anisette en panne ; 10 min au plus pendant les 12 dernières heures). Un échec
  que vous seul pouvez régler (session Apple expirée, appairage perdu, limite du compte gratuit,
  pas d'Apple TV ni d'`.ipa`) affiche **Action requise** sur la carte et n'est pas réessayé à
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
des espaces : si le premier serveur tombe, le suivant prend le relais et passe en premier ensuite. Le
diagnostic liste chaque serveur avec son temps de réponse. Pour héberger le vôtre,
`docker compose --profile anisette up -d` lance un serveur anisette-v3 local
(`dadoum/anisette-v3-server`) sur `127.0.0.1:6969` ; indiquer `http://127.0.0.1:6969` dans le réglage
(réseau hôte : tvsign l'atteint en boucle locale). Ses données vivent dans le volume Docker
`anisette_data` et l'image est figée sur un tag et un digest. Si le serveur ne répond pas, consulter
`docker compose logs anisette`.

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

Chaque installation présente son propre nom d'hôte à l'Apple TV lors de l'appairage (`tvsign-xxxx`,
choisi une fois et conservé avec la base et les sauvegardes) pour que deux instances ne partagent pas
la même identité ; `TVSIGN_PAIR_HOST` le remplace. Les Apple TV déjà appairées gardent le nom avec
lequel elles l'ont été.

Compiler soi-même : voir [docs/building.md](docs/building.md).

---

License: MIT, see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
