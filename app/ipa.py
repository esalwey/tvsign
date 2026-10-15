"""Lecture des IPA et récupération depuis une source (GitHub, URL https)."""
import hashlib
import ipaddress
import json
import plistlib
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import families
from .config import ALLOW_PRIVATE_URLS, GITHUB_TOKEN, MAX_UPLOAD_MB, PRESETS_FILE, VERSION
from .util import replace_retry


class SourceRejected(ValueError):
    """Release refusée pour de bon (IPA invalide, empreinte fausse, bundle ID changé) :
    ne pas la retélécharger à chaque essai."""


class PlatformRejected(SourceRejected):
    """L'IPA est valide mais ne convient pas à l'appareil visé (autre plateforme, iOS trop ancien) :
    une autre IPA de la même release (iOS + tvOS) peut convenir, on essaie la suivante.
    `code` : code d'erreur stable de l'API (voir families.PlatformMismatch)."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


# Droits qu'un compte gratuit refuse (poussée, iCloud, domaines associés) : repérés dans la signature
# de l'exécutable principal quand l'IPA en porte une.
_RISKY_ENTITLEMENTS = (
    (b"aps-environment", "push"),
    (b"com.apple.developer.icloud-", "icloud"),
    (b"com.apple.developer.ubiquity-", "icloud"),
    (b"com.apple.developer.associated-domains", "associated-domains"),
)
_SCAN_MAX = 256 * 1024 * 1024


def _risks(z: zipfile.ZipFile, app_dir: str, executable: str | None) -> list[str]:
    """Limites du compte gratuit visibles dans l'IPA : « appex:N » (chaque extension coûte un
    identifiant d'app de plus) et « push » / « icloud » / « associated-domains »."""
    found = []
    appex = {n[len(app_dir) + 1:].split("/")[1] for n in z.namelist()
             if n.startswith(f"{app_dir}/PlugIns/") and n[len(app_dir) + 1:].count("/") >= 2
             and n[len(app_dir) + 1:].split("/")[1].endswith(".appex")}
    if appex:
        found.append(f"appex:{len(appex)}")
    try:
        member = f"{app_dir}/{executable}" if executable else None
        if member and member in z.namelist():
            needles = {n: k for n, k in _RISKY_ENTITLEMENTS}
            keep = max(len(n) for n in needles) - 1
            tail, read, hits = b"", 0, set()
            with z.open(member) as f:
                while read < _SCAN_MAX and (chunk := f.read(1 << 20)):
                    read += len(chunk)
                    window = tail + chunk
                    for needle, key in needles.items():
                        if needle in window:
                            hits.add(key)
                    tail = window[-keep:]
            found += sorted(hits)
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError):   # IPA abîmée : sans incidence ici
        pass
    return found


def app_info(ipa: Path) -> dict:
    with zipfile.ZipFile(ipa) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
        if not names:
            raise ValueError("ce fichier n'est pas une IPA (Payload/*.app/Info.plist absent)")
        info = plistlib.loads(z.read(names[0]))
        app_dir = names[0].rsplit("/", 1)[0]
        risks = _risks(z, app_dir, info.get("CFBundleExecutable")) if info.get("DTPlatformName") == "iphoneos" else []
    return {
        "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or Path(names[0]).parent.stem,
        "bundle_id": info.get("CFBundleIdentifier"),
        "version": info.get("CFBundleShortVersionString") or info.get("CFBundleVersion"),
        "platform": info.get("DTPlatformName"),
        "min_os": info.get("MinimumOSVersion"),
        "ui_family": families.parse_ui_family(info.get("UIDeviceFamily")),
        "warnings": risks,
    }


def check_tvos(info: dict) -> None:
    """Ancien nom, conservé : une IPA pour Apple TV seulement."""
    families.check_platform(info, "tvos")


check_platform = families.check_platform


def _profile(signed_ipa: Path) -> dict | None:
    """Plist du profil intégré par la signature (embedded.mobileprovision)."""
    with zipfile.ZipFile(signed_ipa) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"Payload/[^/]+\.app/embedded\.mobileprovision", n)]
        if not names:
            return None
        blob = z.read(names[0])
    m = re.search(rb"<\?xml.*?</plist>", blob, re.S)
    return plistlib.loads(m.group(0)) if m else None


def profile_expiry(signed_ipa: Path) -> float | None:
    """Date d'expiration du profil intégré par la signature."""
    exp = (_profile(signed_ipa) or {}).get("ExpirationDate")
    if not isinstance(exp, datetime):
        return None
    # plistlib renvoie une date naïve en UTC : sans tzinfo, .timestamp() la lirait en heure locale
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp.timestamp()


def _der(buf: bytes, i: int) -> tuple[int, int, int]:
    """(tag, début du contenu, longueur) de l'élément DER à la position i."""
    tag, n = buf[i], buf[i + 1]
    i += 2
    if n & 0x80:
        size = n & 0x7F
        n = int.from_bytes(buf[i:i + size], "big")
        i += size
    return tag, i, n


def cert_serial(der: bytes) -> str:
    """Numéro de série d'un certificat X.509 DER (hex majuscule, sans zéros de tête).
    Certificate ::= SEQUENCE { tbsCertificate SEQUENCE { [0] version OPTIONAL, serialNumber INTEGER, … } }"""
    _, cert, _ = _der(der, 0)
    _, i, _ = _der(der, cert)
    tag, start, n = _der(der, i)
    if tag == 0xA0:                       # version explicite : on la saute
        tag, start, n = _der(der, start + n)
    if tag != 0x02:
        raise ValueError("certificat DER inattendu")
    return format(int.from_bytes(der[start:start + n], "big"), "X")


def profile_cert_serials(signed_ipa: Path) -> set[str]:
    """Numéros de série (hex majuscule, sans zéros de tête) des certificats autorisés par le profil."""
    certs = (_profile(signed_ipa) or {}).get("DeveloperCertificates") or []
    return {cert_serial(c) for c in certs}


# ---------- réseau ----------

class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnly)


def _check_url(url: str) -> None:
    """Sources « lien » : https uniquement, et pas d'adresse privée/locale (SSRF)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise SourceRejected("seules les adresses https:// sont acceptées")
    if ALLOW_PRIVATE_URLS:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ConnectionError(f"nom d'hôte introuvable : {parts.hostname} ({e})") from e
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            raise SourceRejected(f"adresse locale refusée ({addr}) : TVSIGN_ALLOW_PRIVATE_URLS=1 pour l'autoriser")


def _get(url: str, timeout: int = 30, headers: dict | None = None):
    _check_url(url)
    h = {"User-Agent": f"tvsign/{VERSION}", "Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN and urllib.parse.urlsplit(url).hostname == "api.github.com":
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    h.update(headers or {})
    return _OPENER.open(urllib.request.Request(url, headers=h), timeout=timeout)


# Requêtes conditionnelles : une réponse 304 ne consomme pas le quota GitHub (60 req/h/IP).
_ETAGS: dict[str, tuple[str, object]] = {}


def _get_json(url: str):
    cached = _ETAGS.get(url)
    try:
        with _get(url, headers={"If-None-Match": cached[0]} if cached else None) as r:
            data = json.load(r)
            if r.headers.get("ETag"):
                _ETAGS[url] = (r.headers["ETag"], data)
            return data
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached:
            return cached[1]
        raise


def _sha256_of(rel: dict, asset: dict) -> str | None:
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1].lower()
    # sinon un fichier « <asset>.sha256 » publié à côté
    side = next((a for a in rel.get("assets", []) if a["name"].lower() == asset["name"].lower() + ".sha256"), None)
    if side:
        with _get(side["browser_download_url"]) as r:
            m = re.search(rb"\b([0-9a-fA-F]{64})\b", r.read(4096))
        if m:
            return m.group(1).decode().lower()
    return None


# Noms d'assets : « NuvioTV.ipa », « App-tvOS.ipa », « app_ios.ipa »… Un « tv » au milieu d'un mot
# (« settings.ipa ») ne compte pas.
_TV_WORD = re.compile(r"(?<![a-z])(apple[-_ ]?tv|tvos|tv)(?![a-z])", re.I)
_TV_CAMEL = re.compile(r"(?<=[a-z])TV(?![a-z])")           # NuvioTV
_IOS_NAME = re.compile(r"(?<![a-z])(ios|iphone|iphoneos|ipad|ipados)(?![a-z])", re.I)


def _name_rank(name: str, family: str = "tvos") -> int:
    """0 : le nom annonce la bonne famille, 1 : neutre, 2 : annonce l'autre famille."""
    stem = name.rsplit(".", 1)[0]
    tv = bool(_TV_CAMEL.search(stem) or _TV_WORD.search(stem))
    ios = bool(_IOS_NAME.search(stem))
    wants_tv = family == "tvos"
    mine, other = (tv, ios) if wants_tv else (ios, tv)
    if mine and not other:
        return 0
    if other and not mine:
        return 2
    return 1


def latest_github(repo: str, tag_prefixes: list[str] | str | None, prereleases: bool = False,
                  asset_name: str | None = None, family: str = "tvos") -> dict | None:
    """Release la plus récente qui correspond : {tag, url, sha256, key, prerelease, asset,
    published_at, candidates}.
    tag_prefixes : le tag doit commencer par l'un d'eux (aucun : tous les tags).
    prereleases : False (canal stable) les ignore, True (canal beta) les inclut.
    family : famille de l'appareil visé ; décide quelle IPA préférer quand une release en publie
    plusieurs (iOS + tvOS). candidates : toutes les IPA utilisables de la release, par ordre de
    préférence (la première est celle des champs ci-dessus).

    Une release plus récente que celle renvoyée mais sans IPA (publication en cours) est signalée par
    « newer_without_ipa » : l'appelant ne doit pas y voir une nouvelle version, ni revenir en arrière."""
    if isinstance(tag_prefixes, str):
        tag_prefixes = [tag_prefixes]
    tag_prefixes = [p for p in (tag_prefixes or []) if p]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        raise SourceRejected("dépôt GitHub attendu sous la forme owner/projet")
    releases = _get_json(f"https://api.github.com/repos/{repo}/releases?per_page=100")
    # L'API ne renvoie pas toujours la plus récente en premier : on trie par date de publication.
    releases = sorted(releases, key=lambda r: r.get("published_at") or r.get("created_at") or "", reverse=True)
    without_ipa = None
    for rel in releases:
        if rel.get("draft") or (tag_prefixes and not rel["tag_name"].startswith(tuple(tag_prefixes))):
            continue
        if rel.get("prerelease") and not prereleases:
            continue
        ipas = [a for a in rel.get("assets", [])
                if a["name"].lower().endswith(".ipa") and a.get("state", "uploaded") == "uploaded"]
        if asset_name:
            ipas = [a for a in ipas if a["name"].lower() == asset_name.lower()]
        # plusieurs IPA (iOS + tvOS) : on préfère celle dont le nom annonce la famille de l'appareil
        ipas.sort(key=lambda a: _name_rank(a["name"], family))
        if not ipas:
            if without_ipa is None:
                without_ipa = rel["tag_name"]      # la plus récente est en cours de publication
            continue
        candidates = []
        for a in ipas:
            sha256 = _sha256_of(rel, a)
            # identité du contenu : un tag fixe (nightly, latest) dont l'IPA est remplacée change de clé
            key = sha256 or f"{a.get('id')}:{a.get('updated_at')}:{a.get('size')}"
            candidates.append({"url": a["browser_download_url"], "sha256": sha256, "key": key, "asset": a["name"]})
        return {"tag": rel["tag_name"], **candidates[0], "prerelease": bool(rel.get("prerelease")),
                "published_at": rel.get("published_at") or rel.get("created_at"),
                "candidates": candidates, "newer_without_ipa": without_ipa,
                "notes": (rel.get("body") or "")[:4000]}
    return None


def url_key(url: str) -> str | None:
    """Identité du fichier d'un lien (ETag, date de modification, taille), lue dans les en-têtes
    sans télécharger le corps. None si le serveur n'en donne aucun : on ne peut alors pas savoir
    si le fichier a changé."""
    with _get(url, timeout=30, headers={"Accept": "*/*"}) as r:
        parts = [r.headers.get(h) for h in ("ETag", "Last-Modified", "Content-Length")]
    return "|".join(p or "" for p in parts) if any(parts) else None


def download(url: str, dest: Path, sha256: str | None = None, expect_bundle: str | None = None,
             family: str = "tvos", os_version: str | None = None, model: str | None = None) -> dict:
    """Télécharge, vérifie (taille, empreinte, plateforme de l'IPA selon la famille de l'appareil,
    bundle ID) puis remplace dest ; l'ancienne IPA est conservée en cas d'échec. Renvoie les infos
    de l'app. Sans précision, l'appareil est une Apple TV (comportement d'origine)."""
    part = dest.with_suffix(".part")
    try:
        h = hashlib.sha256()
        with _get(url, timeout=600) as r, open(part, "wb") as f:
            expected = int(r.headers.get("Content-Length") or 0)
            limit = MAX_UPLOAD_MB * 1024 * 1024    # même plafond que les envois : un lien ne remplit pas le disque
            if expected > limit:
                raise SourceRejected(f"fichier trop gros (> {MAX_UPLOAD_MB} Mo)")
            got = 0
            while chunk := r.read(1 << 20):
                if got + len(chunk) > limit:
                    raise SourceRejected(f"fichier trop gros (> {MAX_UPLOAD_MB} Mo)")
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
        if expected and got != expected:
            raise ConnectionError(f"téléchargement incomplet ({got}/{expected} octets)")
        if sha256 and h.hexdigest() != sha256.lower():
            raise SourceRejected(f"empreinte SHA-256 différente de celle publiée ({h.hexdigest()[:12]}… ≠ {sha256[:12]}…)")
        try:
            info = app_info(part)
        except zipfile.BadZipFile as e:
            if not expected:
                # taille inconnue : une archive illisible peut être un téléchargement tronqué
                raise ConnectionError(f"archive illisible, téléchargement peut-être tronqué ({e})") from e
            raise SourceRejected(f"ce fichier n'est pas une IPA ({e})") from e
        except ValueError as e:
            raise SourceRejected(str(e)) from e
        try:
            check_platform(info, family, os_version, model)
        except ValueError as e:
            raise PlatformRejected(str(e), getattr(e, "code", None)) from e
        if expect_bundle and info["bundle_id"] != expect_bundle:
            raise SourceRejected(f"identifiant d'app changé ({expect_bundle} → {info['bundle_id']}) : "
                                 "version refusée par sécurité ; supprime et rajoute l'app pour l'accepter")
        info["sha256"] = h.hexdigest()
        replace_retry(part, dest)
        return info
    finally:
        part.unlink(missing_ok=True)


# ---------- préréglages ----------

CHANNELS = ("stable", "beta")


def preset_channels(p: dict) -> dict[str, dict]:
    """Canaux d'un préréglage : {canal: {tag_prefixes: [...], prereleases: bool}}.

    Format actuel : "channels": {"stable": {"tag_prefixes": ["tvos-v"]}, "beta": {...}}
    (prereleases : faux pour stable, vrai sinon, sauf mention contraire).
    Ancien format : "tag_prefix": "custom-" et "channels": ["stable", "beta"]."""
    ch = p.get("channels")
    if isinstance(ch, dict):
        out = {}
        for name, c in ch.items():
            if name not in CHANNELS or not isinstance(c, dict):
                continue
            prefixes = c.get("tag_prefixes")
            if isinstance(prefixes, str):
                prefixes = [prefixes]
            out[name] = {"tag_prefixes": [x for x in (prefixes or []) if isinstance(x, str) and x],
                         "prereleases": bool(c.get("prereleases", name != "stable"))}
        if out:
            return out
    legacy = [p["tag_prefix"]] if p.get("tag_prefix") else []
    return {"stable": {"tag_prefixes": legacy, "prereleases": False},
            "beta": {"tag_prefixes": legacy, "prereleases": True}}


def presets() -> list[dict]:
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for p in data.get("presets", []):
        if not (isinstance(p, dict) and p.get("id") and p.get("repo")):
            continue
        channels = preset_channels(p)
        default = p.get("default_channel")
        out.append({**p, "channels": channels,
                    "platform": p.get("platform") or "appletvos",
                    "default_channel": default if default in channels else next(iter(channels))})
    return out


def preset(preset_id: str) -> dict | None:
    return next((p for p in presets() if p["id"] == preset_id), None)


def release_rule(app: dict) -> tuple[list[str], bool]:
    """(préfixes de tag, prereleases incluses) suivis par une app GitHub.

    App issue d'un préréglage : les préfixes du canal choisi, relus dans presets.json (une mise à
    jour des préréglages s'applique aux apps existantes). Un préfixe propre à l'app (réglages) qui
    n'est pas l'un de ceux du préréglage reste prioritaire. Les apps créées avant les canaux par
    préréglage gardent leur préfixe (ex. « custom- ») en source_filter : il fait partie du canal
    beta, elles suivent donc le préréglage."""
    channel = "beta" if app.get("channel") == "beta" else "stable"
    own = (app.get("source_filter") or "").strip()
    p = preset(app["preset"]) if app.get("preset") else None
    if p:
        channels = p["channels"]
        known = {x for c in channels.values() for x in c["tag_prefixes"]}
        if not own or own in known:
            c = channels.get(channel) or {"tag_prefixes": [own] if own else [], "prereleases": channel == "beta"}
            return c["tag_prefixes"], c["prereleases"]
    return ([own] if own else []), channel == "beta"
