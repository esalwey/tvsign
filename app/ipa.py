"""Lecture des IPA et récupération depuis une source (GitHub, URL https)."""
import hashlib
import http.client
import ipaddress
import json
import base64
import os
import plistlib
import re
import shutil
import socket
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from xml.parsers.expat import ExpatError
from datetime import datetime, timezone
from pathlib import Path

from .config import ALLOW_PRIVATE_URLS, GITHUB_TOKEN, MAX_UPLOAD_MB, PRESETS_FILE, VERSION
from .util import replace_retry


class SourceRejected(ValueError):
    """Release refusée pour de bon (IPA invalide, empreinte fausse, bundle ID changé) :
    ne pas la retélécharger à chaque essai.
    code : identifiant stable (« code » ou « code:paramètre ») que l'API renvoie tel quel et que
    l'interface traduit ; le message reste en français pour le journal."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def _text(value) -> str | None:
    """Champ d'Info.plist utilisable comme texte : une chaîne non vide, sinon rien (un tableau ou un
    nombre à cet endroit ne doit pas finir dans la base)."""
    return value.strip() if isinstance(value, str) and value.strip() else None


class UrlRejected(SourceRejected):
    """Adresse de lien refusée avant tout accès réseau. `code` : clé d'interface (https_only, url_userinfo, url_private)."""

    def __init__(self, message: str, code: str):
        super().__init__(message, code)


class OsTooOld(SourceRejected):
    """L'IPA demande un tvOS plus récent que celui de l'Apple TV (MinimumOSVersion). Contrairement
    aux autres refus, celui-ci se lève de lui-même quand la TV est mise à jour."""

    def __init__(self, min_os: str, have: str):
        super().__init__(f"cette app demande tvOS {min_os} ou plus, l'Apple TV est en tvOS {have}")
        self.min_os, self.have = min_os, have


class SourceUnavailable(RuntimeError):
    """GitHub répond, mais pas avec ce qu'il faut (quota dépassé, dépôt introuvable ou privé) : passager
    ou à corriger par l'utilisateur, jamais une raison de refuser une release. `code` : clé d'interface."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def os_tuple(version) -> tuple[int, ...]:
    """« 26.1 » -> (26, 1) ; () si illisible."""
    nums = re.findall(r"\d+", str(version or "").split("(")[0])
    return tuple(int(n) for n in nums[:4])


def os_older(have, need) -> bool:
    """La version `have` est-elle strictement plus ancienne que `need` ? Faux si l'une est inconnue."""
    h, n = os_tuple(have), os_tuple(need)
    if not h or not n:
        return False
    width = max(len(h), len(n))
    return h + (0,) * (width - len(h)) < n + (0,) * (width - len(n))


def app_info(ipa: Path) -> dict:
    """Infos de l'app d'une IPA. ValueError si le fichier n'est pas une IPA exploitable
    (Info.plist absent, illisible, ni dictionnaire, ou sans identifiant d'app)."""
    with zipfile.ZipFile(ipa) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
        if not names:
            raise ValueError("ce fichier n'est pas une IPA (Payload/*.app/Info.plist absent)")
        try:
            info = plistlib.loads(z.read(names[0]))
        except (plistlib.InvalidFileException, ExpatError, ValueError) as e:
            raise ValueError(f"Info.plist illisible ({e})") from e
    if not isinstance(info, dict):
        raise ValueError("Info.plist inattendu (dictionnaire attendu)")
    bundle_id = _text(info.get("CFBundleIdentifier"))
    if not bundle_id:
        raise ValueError("Info.plist sans identifiant d'app (CFBundleIdentifier)")
    return {
        "name": _text(info.get("CFBundleDisplayName")) or _text(info.get("CFBundleName")) or Path(names[0]).parent.stem,
        "bundle_id": bundle_id,
        "version": _text(info.get("CFBundleShortVersionString")) or _text(info.get("CFBundleVersion")),
        "platform": _text(info.get("DTPlatformName")),
        "min_os": _text(info.get("MinimumOSVersion")),
    }


def check_tvos(info: dict) -> None:
    if info["platform"] != "appletvos":
        raise ValueError(f"app pour « {info['platform'] or 'inconnu'} », seules les apps tvOS sont acceptées")


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


def _children(buf: bytes, start: int, length: int) -> list[tuple[int, int, int]]:
    """Éléments DER enfants (tag, début du contenu, longueur) du contenu [start, start+length)."""
    out, i, end = [], start, start + length
    while i < end:
        tag, cstart, n = _der(buf, i)
        out.append((tag, cstart, n))
        i = cstart + n
    return out


def _int(buf: bytes, el: tuple[int, int, int]) -> int:
    return int.from_bytes(buf[el[1]:el[1] + el[2]], "big")


def key_modulus(pem: str) -> int | None:
    """Module RSA d'une clé privée PEM (PKCS#8 « PRIVATE KEY » ou PKCS#1 « RSA PRIVATE KEY »)."""
    m = re.search(r"-----BEGIN (RSA )?PRIVATE KEY-----(.*?)-----END (RSA )?PRIVATE KEY-----", pem, re.S)
    if not m:
        return None
    der = base64.b64decode("".join(m.group(2).split()))
    tag, start, n = _der(der, 0)
    parts = _children(der, start, n)
    if m.group(1):                                   # PKCS#1 : SEQUENCE { 0, n, e, … }
        return _int(der, parts[1])
    inner = parts[2]                                 # PKCS#8 : SEQUENCE { 0, algo, OCTET STRING { PKCS#1 } }
    _, istart, ilen = _der(der[inner[1]:inner[1] + inner[2]], 0)
    blob = der[inner[1]:inner[1] + inner[2]]
    return _int(blob, _children(blob, istart, ilen)[1])


def cert_modulus(der: bytes) -> int | None:
    """Module RSA de la clé publique d'un certificat X.509 DER."""
    _, cert, clen = _der(der, 0)
    tbs = _children(der, cert, clen)[0]
    fields = _children(der, tbs[1], tbs[2])
    if fields and fields[0][0] == 0xA0:              # version explicite
        fields = fields[1:]
    spki = fields[5]                                 # serial, algo, émetteur, validité, sujet, puis la clé publique
    algo_and_key = _children(der, spki[1], spki[2])
    bits = algo_and_key[1]                           # BIT STRING : octet d'incomplétude puis RSAPublicKey
    inner = der[bits[1] + 1:bits[1] + bits[2]]
    _, istart, ilen = _der(inner, 0)
    return _int(inner, _children(inner, istart, ilen)[0])


def signing_serial(signed_ipa: Path, key_pem: str | None = None) -> str | None:
    """Numéro de série du certificat qui a réellement signé l'app. Le profil liste tous les certificats de
    l'équipe (Xcode, SideStore…), seul celui-ci compte pour savoir si l'app est encore lançable. Source :
    la signature de l'exécutable lui-même (signer_serial) ; à défaut, le certificat du profil dont la clé
    publique est celle de notre clé privée (key_pem). None si on ne peut pas conclure."""
    found = signer_serial(signed_ipa)
    if found or not key_pem:
        return found
    try:
        mine = key_modulus(key_pem)
        if not mine:
            return None
        found = set()
        for c in (_profile(signed_ipa) or {}).get("DeveloperCertificates") or []:
            if cert_modulus(c) == mine:
                found.add(cert_serial(c))
        return found.pop() if len(found) == 1 else None
    except (ValueError, IndexError, TypeError, zipfile.BadZipFile, plistlib.InvalidFileException, ExpatError):
        return None


def _ber(buf: bytes, i: int) -> tuple[int, int, int, int]:
    """(tag, début du contenu, fin du contenu, élément suivant) de l'élément BER/DER à la position i.
    Les longueurs indéfinies (0x80 … 00 00), que produisent les outils de signature d'Apple, sont gérées."""
    tag, n = buf[i], buf[i + 1]
    i += 2
    if n == 0x80:
        start, j = i, i
        while buf[j:j + 2] != b"\0\0":
            j = _ber(buf, j)[3]
        return tag, start, j, j + 2
    if n & 0x80:
        size = n & 0x7F
        n = int.from_bytes(buf[i:i + size], "big")
        i += size
    return tag, i, i + n, i + n


def _kids(buf: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    out, i = [], start
    while i < end and buf[i:i + 2] != b"\0\0":
        tag, s, e, nxt = _ber(buf, i)
        out.append((tag, s, e))
        i = nxt
    return out


def cms_signer_serial(cms: bytes) -> str | None:
    """Numéro de série (hex majuscule, sans zéros de tête) du certificat qui a signé un CMS SignedData.
    ContentInfo { OID, [0] SignedData { version, digestAlgorithms, encapContentInfo, [0] certificates?,
    [1] crls?, signerInfos { SignerInfo { version, IssuerAndSerialNumber { issuer, serial }, … } } } }"""
    _, s, e, _ = _ber(cms, 0)
    top = _kids(cms, s, e)
    if len(top) < 2 or top[1][0] != 0xA0:
        return None
    signed = _kids(cms, top[1][1], top[1][2])
    if not signed or signed[0][0] != 0x30:
        return None
    parts = _kids(cms, signed[0][1], signed[0][2])
    infos = next((p for p in reversed(parts) if p[0] == 0x31), None)   # SET OF SignerInfo : le dernier SET
    if not infos:
        return None
    signers = _kids(cms, infos[1], infos[2])
    if not signers:
        return None
    fields = _kids(cms, signers[0][1], signers[0][2])
    if len(fields) < 2 or fields[1][0] != 0x30:
        return None                       # identifiant par SubjectKeyIdentifier : non géré
    issuer_serial = _kids(cms, fields[1][1], fields[1][2])
    if len(issuer_serial) < 2 or issuer_serial[1][0] != 0x02:
        return None
    return format(int.from_bytes(cms[issuer_serial[1][1]:issuer_serial[1][2]], "big"), "X")


def _macho_signature(f) -> bytes | None:
    """Bloc de signature (LC_CODE_SIGNATURE) d'un Mach-O lu en flux (f.read / f.seek en avant)."""
    head = f.read(8)
    base = 0
    if head[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):          # binaire universel (gros-boutiste)
        big = head[:4] == b"\xca\xfe\xba\xbf"
        count, size = int.from_bytes(head[4:8], "big"), 32 if big else 20
        table = f.read(count * size)
        if not count:
            return None
        base = int.from_bytes(table[8:16] if big else table[8:12], "big")   # première architecture
        f.seek(base)
        head = f.read(8)
    if head[:4] not in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):       # Mach-O 64 / 32 bits, petit-boutiste
        return None
    header = head + f.read(24 if head[:4] == b"\xcf\xfa\xed\xfe" else 20)
    ncmds, sizeofcmds = int.from_bytes(header[16:20], "little"), int.from_bytes(header[20:24], "little")
    cmds = f.read(sizeofcmds)
    off = 0
    for _ in range(ncmds):
        if off + 8 > len(cmds):
            return None
        cmd, size = int.from_bytes(cmds[off:off + 4], "little"), int.from_bytes(cmds[off + 4:off + 8], "little")
        if cmd == 0x1D and size >= 16:                              # LC_CODE_SIGNATURE
            dataoff = int.from_bytes(cmds[off + 8:off + 12], "little")
            datasize = int.from_bytes(cmds[off + 12:off + 16], "little")
            if base + dataoff < f.tell():
                return None                   # la lecture en flux ne revient pas en arrière
            f.seek(base + dataoff)
            return f.read(datasize)
        off += max(size, 8)
    return None


def signer_serial(signed_ipa: Path) -> str | None:
    """Numéro de série du certificat qui a réellement signé l'app (lu dans la signature de son exécutable
    principal), ou None si illisible. Le profil, lui, liste tous les certificats de l'équipe : y chercher le
    nôtre ne dit rien quand un autre outil (AltStore, Xcode…) a aussi un certificat sur le compte."""
    try:
        with zipfile.ZipFile(signed_ipa) as z:
            plists = [n for n in z.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
            if not plists:
                return None
            exe = plistlib.loads(z.read(plists[0])).get("CFBundleExecutable")
            if not exe:
                return None
            with z.open(plists[0].rsplit("/", 1)[0] + "/" + exe) as f:
                blob = _macho_signature(f)
        # SuperBlob (gros-boutiste) : magic, longueur, nombre d'entrées, puis (type, offset) par entrée
        if not blob or int.from_bytes(blob[:4], "big") != 0xFADE0CC0:
            return None
        for i in range(int.from_bytes(blob[8:12], "big")):
            kind = int.from_bytes(blob[12 + 8 * i:16 + 8 * i], "big")
            off = int.from_bytes(blob[16 + 8 * i:20 + 8 * i], "big")
            if kind == 0x10000 and int.from_bytes(blob[off:off + 4], "big") == 0xFADE0B01:   # signature CMS
                length = int.from_bytes(blob[off + 4:off + 8], "big")
                return cms_signer_serial(blob[off + 8:off + length]) if length > 8 else None
    except Exception:  # noqa: BLE001 - signature atypique ou fichier tronqué : on retombe sur le profil
        return None
    return None


# ---------- réseau ----------

# Hôtes de GitHub (API, releases, stockage des assets) : fixes, de confiance, jamais soumis au filtre
# d'adresses locales. Il vise les liens saisis par l'utilisateur, pas ces hôtes : NAT64/DNS64, DNS « fake-ip »
# (Clash, Surge) ou puits DNS les font sinon résoudre vers des plages « privées » et bloquent toute mise à jour.
def _trusted_host(host: str | None) -> bool:
    host = (host or "").lower().rstrip(".")
    return host in ("api.github.com", "github.com") or host.endswith(".githubusercontent.com")


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _public(addr) -> bool:
    """Adresse d'Internet, hors réseau local ? Les formes qui en enveloppent une IPv4 (IPv4-mappée,
    NAT64, 6to4) sont jugées sur l'IPv4 qu'elles portent. 100.64.0.0/10 (Tailscale, CGNAT) n'est pas globale."""
    if addr.version == 6:
        if addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        elif addr in _NAT64:
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        elif addr.sixtofour:
            addr = addr.sixtofour
    return addr.is_global and not addr.is_multicast


def _resolve_public(host: str, port: int) -> list:
    """Résout `host` et vérifie que toutes ses adresses sont publiques ; renvoie les résultats de getaddrinfo."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ConnectionError(f"nom d'hôte introuvable : {host} ({e})") from e
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if not _public(addr):
            raise UrlRejected(f"adresse locale refusée ({addr}) : TVSIGN_ALLOW_PRIVATE_URLS=1 pour l'autoriser",
                              "url_private")
    return infos


def redact(url: str) -> str:
    """Adresse sans identifiants ni paramètres (jetons de liens signés) : pour le journal et l'affichage."""
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{p.port}" if p.port else ""
        return f"{p.scheme}://{host}{port}{p.path}" + ("?…" if p.query else "")
    except ValueError:
        return "(adresse illisible)"


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Se connecte à l'adresse vérifiée, pas à une seconde résolution du nom (DNS rebinding : un
    DNS hostile répond une adresse publique au contrôle, puis 127.0.0.1 à la connexion).
    Le nom reste utilisé pour SNI, la validation du certificat et l'en-tête Host."""

    def connect(self):
        if ALLOW_PRIVATE_URLS or _trusted_host(self.host) or self._tunnel_host:
            return super().connect()          # mandataire : c'est lui qui résout la destination
        last = None
        for family, kind, proto, _, addr in _resolve_public(self.host, self.port):
            try:
                sock = socket.create_connection(addr[:2], self.timeout, self.source_address)
            except OSError as e:
                last = e
                continue
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            return None
        raise last or ConnectionError(f"connexion impossible à {self.host}")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")     # le jeton GitHub ne suit jamais une redirection vers un autre hôte
        return new


_OPENER = urllib.request.build_opener(_HttpsOnly, _PinnedHTTPSHandler)


def _check_url(url: str) -> None:
    """Sources « lien » : https uniquement, sans identifiants dans l'adresse, et pas d'adresse
    privée/locale (SSRF). Les hôtes de GitHub ne sont pas soumis au filtre d'adresses."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise UrlRejected("seules les adresses https:// sont acceptées", "https_only")
    if parts.username is not None or "@" in parts.netloc:
        raise UrlRejected("les adresses avec identifiant:mot de passe@ ne sont pas prises en charge : "
                          "utilise un lien à jeton (paramètre dans l'adresse) ou un autre hébergement",
                          "url_userinfo")
    try:
        port = parts.port or 443
    except ValueError:
        raise SourceRejected("port invalide dans l'adresse") from None
    if ALLOW_PRIVATE_URLS or _trusted_host(parts.hostname):
        return
    _resolve_public(parts.hostname, port)


def _get(url: str, timeout: int = 30, headers: dict | None = None):
    _check_url(url)
    h = {"User-Agent": f"tvsign/{VERSION}", "Accept": "*/*"}
    if GITHUB_TOKEN and urllib.parse.urlsplit(url).hostname == "api.github.com":
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    h.update(headers or {})
    return _OPENER.open(urllib.request.Request(url, headers=h), timeout=timeout)


# Requêtes conditionnelles : une réponse 304 ne consomme pas le quota GitHub (60 req/h/IP).
_ETAGS: dict[str, tuple[str, object]] = {}


def _github_error(e: urllib.error.HTTPError) -> SourceUnavailable | None:
    """Traduit le refus de GitHub en message exploitable ; None si ce n'est pas un cas connu."""
    hint = "" if GITHUB_TOKEN else " ; TVSIGN_GITHUB_TOKEN relève la limite à 5000 requêtes par heure"
    limited = e.headers.get("X-RateLimit-Remaining") == "0" or e.headers.get("Retry-After")
    if e.code == 429 or (e.code == 403 and limited):
        when = ""
        try:
            reset = int(e.headers.get("X-RateLimit-Reset") or 0)
            if reset:
                when = " (reprise vers " + datetime.fromtimestamp(reset).strftime("%H:%M") + ")"
        except (ValueError, OSError):
            pass
        return SourceUnavailable(f"limite de requêtes GitHub atteinte{when}{hint}", "gh_rate_limit")
    if e.code == 401 and GITHUB_TOKEN:
        return SourceUnavailable("jeton GitHub refusé : vérifie ou renouvelle TVSIGN_GITHUB_TOKEN "
                                 "(expiré, révoqué ou sans accès à ce dépôt)", "gh_bad_token")
    if e.code in (401, 404):
        return SourceUnavailable("dépôt GitHub introuvable ou privé : vérifie owner/projet (dépôt renommé ?)"
                                 + ("" if GITHUB_TOKEN else ", ou fournis TVSIGN_GITHUB_TOKEN pour un dépôt privé"),
                                 "gh_not_found")
    return None


def _get_json(url: str):
    cached = _ETAGS.get(url)
    headers = {"Accept": "application/vnd.github+json"}
    if cached:
        headers["If-None-Match"] = cached[0]
    try:
        with _get(url, headers=headers) as r:
            data = json.load(r)
            if r.headers.get("ETag"):
                _ETAGS[url] = (r.headers["ETag"], data)
            return data
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached:
            return cached[1]
        translated = _github_error(e)
        if translated:
            raise translated from e
        raise


def _asset_url(asset: dict) -> str:
    """Adresse de téléchargement d'un asset. Avec un jeton, l'API des assets (seule à accepter le jeton, et
    donc seule à servir un dépôt privé) ; sinon l'adresse publique."""
    return asset["url"] if GITHUB_TOKEN and asset.get("url") else asset["browser_download_url"]


def _download_headers(url: str) -> dict | None:
    return {"Accept": "application/octet-stream"} if urllib.parse.urlsplit(url).hostname == "api.github.com" else None


def _sha256_of(rel: dict, asset: dict) -> str | None:
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1].lower()
    # sinon un fichier « <asset>.sha256 » publié à côté
    side = next((a for a in rel.get("assets", []) if a["name"].lower() == asset["name"].lower() + ".sha256"), None)
    if side:
        url = _asset_url(side)
        with _get(url, headers=_download_headers(url)) as r:
            m = re.search(rb"\b([0-9a-fA-F]{64})\b", r.read(4096))
        if m:
            return m.group(1).decode().lower()
    return None


_REPO_URL = re.compile(r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/.*)?(?:[?#].*)?$", re.I)
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", re.ASCII)


def normalize_repo(value: str | None) -> str | None:
    """« owner/projet » valide, ou None. Accepte aussi une adresse https://github.com/owner/projet
    (avec ou sans .git) collée telle quelle ; refuse les segments « . » et « .. »."""
    v = (value or "").strip()
    m = _REPO_URL.match(v)
    if m:       # adresse collée du navigateur (/releases/tag/…, ?tab=…, git@github.com:owner/projet)
        v = f"{m[1]}/{m[2]}"
    v = v.rstrip("/")
    if v.lower().endswith(".git"):
        v = v[:-4]
    if not _REPO.fullmatch(v):
        return None
    if any(seg in (".", "..") for seg in v.split("/")):
        return None
    return v


def latest_github(repo: str, tag_prefixes: list[str] | str | None, prereleases: bool = False,
                  asset_name: str | None = None, pinned_tag: str | None = None) -> dict | None:
    """Release la plus récente qui correspond : {tag, url, sha256, key, prerelease, asset, notes}.
    tag_prefixes : le tag doit commencer par l'un d'eux (aucun : tous les tags).
    prereleases : False (canal stable) les ignore, True (canal beta) les inclut.
    pinned_tag : version épinglée par l'utilisateur ; seule cette release est considérée (préfixes et canal ignorés)."""
    if isinstance(tag_prefixes, str):
        tag_prefixes = [tag_prefixes]
    tag_prefixes = [p for p in (tag_prefixes or []) if p]
    repo = normalize_repo(repo)
    if not repo:
        raise SourceRejected("dépôt GitHub attendu sous la forme owner/projet")
    releases = _get_json(f"https://api.github.com/repos/{repo}/releases?per_page=100")
    # L'API ne renvoie pas toujours la plus récente en premier : on trie par date de publication.
    releases = sorted(releases, key=lambda r: r.get("published_at") or r.get("created_at") or "", reverse=True)
    for rel in releases:
        if rel.get("draft"):
            continue
        if pinned_tag:
            if rel["tag_name"] != pinned_tag:
                continue
        else:
            if tag_prefixes and not rel["tag_name"].startswith(tuple(tag_prefixes)):
                continue
            if rel.get("prerelease") and not prereleases:
                continue
        ipas = [a for a in rel.get("assets", [])
                if a["name"].lower().endswith(".ipa") and a.get("state", "uploaded") == "uploaded"]
        if asset_name:
            ipas = [a for a in ipas if a["name"].lower() == asset_name.lower()]
        # plusieurs IPA (iOS + tvOS) : on préfère celle qui annonce tvOS dans son nom
        ipas.sort(key=lambda a: not re.search(r"tv", a["name"], re.I))
        if ipas:
            sha256 = _sha256_of(rel, ipas[0])
            # identité du contenu : un tag fixe (nightly, latest) dont l'IPA est remplacée change de clé
            key = sha256 or f"{ipas[0].get('id')}:{ipas[0].get('updated_at')}:{ipas[0].get('size')}"
            return {"tag": rel["tag_name"], "url": _asset_url(ipas[0]),
                    "sha256": sha256, "key": key, "prerelease": bool(rel.get("prerelease")),
                    "asset": ipas[0]["name"], "notes": (rel.get("body") or "")[:4000], "name": rel.get("name") or rel["tag_name"]}
    return None


def url_key(url: str) -> str | None:
    """Identité du fichier d'un lien (ETag, date de modification, taille), lue dans les en-têtes
    sans télécharger le corps. None si le serveur n'en donne aucun : on ne peut alors pas savoir
    si le fichier a changé (le renouvellement compare alors l'empreinte d'un nouveau téléchargement)."""
    with _get(url, timeout=30) as r:
        parts = [r.headers.get(h) for h in ("ETag", "Last-Modified", "Content-Length")]
    return "|".join(p or "" for p in parts) if any(parts) else None


def keep_copy(src: Path, dst: Path, copy: bool = True) -> None:
    """Copie `src` vers `dst` (remplacée), en lien physique quand le système de fichiers le permet :
    le remplacement de `src` par un nouveau fichier laisse alors `dst` intact, sans dupliquer l'IPA.
    copy=False : pas de copie de repli (lien impossible = pas de sauvegarde)."""
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        if copy:
            shutil.copy2(src, dst)


def download(url: str, dest: Path, sha256: str | None = None, expect_bundle: str | None = None,
             max_os: str | None = None, unchanged_sha: str | None = None) -> dict | None:
    """Télécharge, vérifie (taille, empreinte, IPA tvOS, bundle ID, tvOS minimal de la TV) puis remplace
    dest ; l'ancienne IPA est conservée en cas d'échec. Renvoie les infos de l'app, ou None si le fichier
    a la même empreinte que `unchanged_sha` (dest n'est alors pas touché)."""
    part = dest.with_suffix(".part")
    try:
        h = hashlib.sha256()
        with _get(url, timeout=600, headers=_download_headers(url)) as r, open(part, "wb") as f:
            expected = int(r.headers.get("Content-Length") or 0)
            limit = MAX_UPLOAD_MB * 1024 * 1024    # même plafond que les envois : un lien ne remplit pas le disque
            if expected > limit:
                raise SourceRejected(f"fichier trop gros (> {MAX_UPLOAD_MB} Mo)", code=f"too_large:{MAX_UPLOAD_MB}")
            got = 0
            while chunk := r.read(1 << 20):
                if got + len(chunk) > limit:
                    raise SourceRejected(f"fichier trop gros (> {MAX_UPLOAD_MB} Mo)", code=f"too_large:{MAX_UPLOAD_MB}")
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
        if expected and got != expected:
            raise ConnectionError(f"téléchargement incomplet ({got}/{expected} octets)")
        if sha256 and h.hexdigest() != sha256.lower():
            raise SourceRejected(f"empreinte SHA-256 différente de celle publiée ({h.hexdigest()[:12]}… ≠ {sha256[:12]}…)")
        if unchanged_sha and h.hexdigest() == unchanged_sha.lower():
            return None
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
            check_tvos(info)
        except ValueError as e:
            raise SourceRejected(str(e)) from e
        if expect_bundle and info["bundle_id"] != expect_bundle:
            raise SourceRejected(f"identifiant d'app changé ({expect_bundle} → {info['bundle_id']}) : "
                                 "version refusée par sécurité ; supprime et rajoute l'app pour l'accepter")
        if max_os and os_older(max_os, info.get("min_os")):
            raise OsTooOld(info["min_os"], max_os)
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
