"""Familles d'appareils (Apple TV, iPhone, iPad…) : quelle IPA va sur quel appareil.

Une famille est enregistrée sur chaque appareil (devices.family) :
  tvos      Apple TV (DTPlatformName appletvos)
  ios       iPhone, iPod touch (iphoneos)
  ipados    iPad (iphoneos, UIDeviceFamily 2)
  visionos  Apple Vision Pro (xros) : reconnu mais pas encore pris en charge

Les apps tvOS passent par exactement le même chemin qu'avant ; une IPA est comparée à la famille
de l'appareil visé au moment de l'ajout, du téléchargement d'une nouvelle version et d'un
changement d'appareil.
"""
import re

FAMILIES = ("tvos", "ios", "ipados", "visionos")
MOBILE = ("ios", "ipados")                 # iPhone / iPad : joignables en Wi-Fi, mais pas toujours là
SUPPORTED = ("tvos", "ios", "ipados")      # familles pour lesquelles tvsign installe

# DTPlatformName de l'IPA -> familles d'appareils qui peuvent la recevoir
PLATFORM_FAMILIES = {
    "appletvos": ("tvos",),
    "iphoneos": ("ios", "ipados"),
}

LABEL = {"tvos": "Apple TV", "ios": "iPhone", "ipados": "iPad", "visionos": "Apple Vision Pro"}

# UDID d'un iPhone récent (8-16 hex) ou ancien (40 hex)
_UDID = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9A-Fa-f]{40})(?![0-9A-Fa-f])")


class PlatformMismatch(ValueError):
    """IPA incompatible avec l'appareil. `code` est le code d'erreur stable de l'API
    (« code » ou « code:paramètre »), le message est celui du journal."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def is_mobile(family: str | None) -> bool:
    return family in MOBILE


def label(family: str | None) -> str:
    return LABEL.get(family or "tvos", "Apple TV")


def family_of(product_type: str | None = None, device_class: str | None = None) -> str | None:
    """Famille d'après la classe d'appareil (DeviceClass) ou, à défaut, le modèle (ProductType).
    None si rien de reconnaissable."""
    for raw in (device_class, product_type):
        v = (raw or "").strip().lower()
        if v.startswith("appletv"):
            return "tvos"
        if v.startswith("ipad"):
            return "ipados"
        if v.startswith(("iphone", "ipod")):
            return "ios"
        if v.startswith(("realitydevice", "vision")):
            return "visionos"
    return None


def parse_ui_family(raw) -> list[int]:
    """UIDeviceFamily de l'Info.plist (liste d'entiers, parfois un entier seul) -> liste triée."""
    if isinstance(raw, (int, str)) and not isinstance(raw, bool):
        raw = [raw]
    out = set()
    for v in raw or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            pass
    return sorted(out)


def version_tuple(v: str | None) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", (v or "").split("-")[0])[:3])


def udid_from_name(name: str | None) -> str | None:
    """UDID contenu dans le nom d'un fichier d'appairage (« 00008110-001A2B3C00000000.plist »,
    « plume_<UDID>.plist »)."""
    m = _UDID.search(name or "")
    return m.group(0) if m else None


def check_platform(info: dict, family: str = "tvos", model: str | None = None) -> None:
    """Lève PlatformMismatch si l'IPA ne convient pas à un appareil de cette famille.

    info : résultat d'ipa.app_info (platform, ui_family). model : celui de l'appareil, quand il est connu.
    La version minimale du système (MinimumOSVersion) se contrôle à part, avec ipa.os_older : au moment
    de l'ajout (main._check_min_os), du téléchargement (ipa.download, OsTooOld) et avant la signature."""
    platform = info.get("platform")
    shown = platform or "inconnu"           # dans le message ; le code d'erreur de l'API dit « unknown »
    code_shown = platform or "unknown"
    if family == "visionos":
        raise PlatformMismatch("unsupported_device",
                               "Apple Vision Pro pas encore pris en charge : seuls l'Apple TV, l'iPhone et l'iPad le sont")
    if family not in PLATFORM_FAMILIES.get(platform, ()):
        if family == "tvos":
            raise PlatformMismatch(f"not_tvos:{code_shown}",
                                   f"app pour « {shown} », seules les apps tvOS sont acceptées sur une Apple TV")
        raise PlatformMismatch(f"not_ios:{code_shown}",
                               f"app pour « {shown} », seules les apps iPhone/iPad (iphoneos) sont acceptées "
                               f"sur {label(family)}")
    if family == "ios" and (model or "").lower().startswith("ipad"):
        family = "ipados"
    if family == "ios" and parse_ui_family(info.get("ui_family")) == [2]:
        raise PlatformMismatch("ipad_only", "app réservée à l'iPad (UIDeviceFamily 2), cet appareil est un iPhone")
