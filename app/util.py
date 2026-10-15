"""Small file helpers shared by the backend."""
import os
import re
import time
from pathlib import Path


class ContentError(RuntimeError):
    """Échec de signature ou d'installation dû au contenu de l'IPA (bundle, Info.plist, entitlements,
    paquet refusé par installd). Seul ce genre d'échec justifie d'abandonner une nouvelle version :
    les limites du compte Apple (App ID, certificats, appareils) ou un Apple TV plein la suivraient
    à l'identique avec l'ancienne."""


_CONTENT = re.compile(
    r"invalid (?:bundle|info\.plist|ipa|entitlement|signature|executable)|info\.plist|entitlement|"
    r"could not be verified|verification failed|ApplicationVerificationFailed|InvalidBundle|"
    r"MissingBundleExecutable|MismatchedApplicationIdentifier|not a valid (?:ipa|bundle|app)|"
    r"(?:installd|MobileInstallation|package)\b.{0,60}\b(?:reject|invalid|corrupt|malformed)|"
    r"corrupt|malformed|CFBundle", re.I)
_LIMITS = re.compile(
    r"app ?id|identifier.{0,20}(?:limit|maximum)|limit|quota|maximum|too many|exceed|no space|not enough|"
    r"disk|storage|certificate.{0,20}(?:limit|maximum|already)|device.{0,20}(?:limit|maximum)|"
    r"\brate\b|try again|session|sign(?:ed)? in|login|unauthori[sz]ed|forbidden", re.I)


def is_content_error(text: str) -> bool:
    """La sortie de plumesign désigne-t-elle l'IPA elle-même (et non le compte, l'espace ou le réseau) ?"""
    text = text or ""
    return bool(_CONTENT.search(text)) and not _LIMITS.search(text)


def replace_retry(src: Path, dst: Path, attempts: int = 6, delay: float = 0.25) -> None:
    """os.replace with retries: on Windows an antivirus or indexer briefly holding the target
    makes replace fail with PermissionError."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def write_private(path: Path, data: bytes | str) -> None:
    """Write a secret file readable by the owner only (0600 on POSIX), atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    replace_retry(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
