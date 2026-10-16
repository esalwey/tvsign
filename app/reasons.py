"""Causes connues des échecs, dites en anglais, pour les notifications (le serveur écrit ses erreurs en français).

Copie des CAUSES de web/errors.js (même ordre : la première qui correspond l'emporte) avec le titre anglais de
chacune dans web/i18n.js. Fichier généré par `python scripts/sync-reasons.py` : ne pas le modifier à la main, un test
vérifie qu'il suit l'interface.
"""
import re

CAUSES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"CERT_LIMIT"), "c_certlimit", "Apple account full: a certificate has to be freed"),
    (re.compile(r"NO_TEAM|MULTIPLE_TEAMS|TEAM_NOT_FOUND"), "c_team", "Apple developer team not found"),
    (re.compile(r"aucun certificat importé", re.I), "c_ic_none", "No imported certificate chosen"),
    (re.compile(r"certificat importé révoqué", re.I), "c_ic_revoked", "Imported certificate revoked"),
    (re.compile(r"(certificat|profil)( ou profil)? importé expiré", re.I), "c_ic_expired", "Imported certificate or profile expired"),
    (re.compile(r"certificat importé (introuvable|illisible)", re.I), "c_ic_missing", "Imported certificate not found"),
    (re.compile(r"certificat importé .{0,80}autre appareil", re.I), "c_ic_device", "Certificate made for another device"),
    (re.compile(r"profil importé .{0,80}(est fait pour|ne couvre)|profil importé refusé|refusés? par le profil importé", re.I), "c_ic_profile", "Imported profile refused"),
    (re.compile(r"adresse introuvable", re.I), "c_host_unresolved", "Address not found"),
    (re.compile(r"oubli de l'appairage", re.I), "c_forget_failed", "Pairing not erased"),
    (re.compile(r"recherche des Apple TV", re.I), "c_discover_failed", "Couldn't search for Apple TVs"),
    (re.compile(r"arrêté par le système|killed by the system", re.I), "c_oom", "plumesign stopped by the system"),
    (re.compile(r"ntfy|notification .*échouée|notify_failed", re.I), "c_ntfy", "Notification not sent"),
    (re.compile(r"maximum number of (installed )?(apps|app ?ids?)|too many (installed )?(apps|app ?ids?)|app ?id limit|limit of \d+ (apps|app ?ids)|limite du compte gratuit|free developer profile|reached the maximum number", re.I), "c_limit", "Free account limit reached"),
    (re.compile(r"limite de requêtes GitHub|rate limit exceeded", re.I), "c_gh_rate", "GitHub request limit reached"),
    (re.compile(r"jeton GitHub refusé", re.I), "c_gh_token", "GitHub token rejected"),
    (re.compile(r"dépôt GitHub introuvable", re.I), "c_gh_missing", "GitHub repository not found"),
    (re.compile(r"demande (tvOS|iOS|iPadOS)", re.I), "c_tvos", "Device too old for this app"),
    (re.compile(r"seules les apps (tvOS|iPhone)|réservée à l'iPad|pas encore pris en charge", re.I), "c_platform", "App doesn't fit the device"),
    (re.compile(r"aucune IPA trouvée"), "c_no_release", "No matching release"),
    (re.compile(r"8220|no devices from which", re.I), "c_8220", "Device not registered on the account"),
    (re.compile(r"ancienne version de tvsign", re.I), "c_legacy", "Pair again after the update"),
    (re.compile(r"usbmuxd is not reachable", re.I), "c_usbmuxd", "Apple's USB driver not found"),
    (re.compile(r"No (iPhone or iPad|device \S+) connected over USB", re.I), "c_usb_none", "No iPhone or iPad over USB"),
    (re.compile(r"\d+ devices are connected over USB", re.I), "c_usb_many", "Several devices plugged in over USB"),
    (re.compile(r"offers no tunnel service|iOS 17 or later is required", re.I), "c_ios17", "iOS / iPadOS 17 or later required"),
    (re.compile(r"appairage perdu|appairage absent|PAIRING_LOST|pairing record|not paired|No stored pairing", re.I), "c_lost", "Pairing lost"),
    (re.compile(r"aucune Apple TV", re.I), "c_no_tv", "No device assigned"),
    (re.compile(r"aucun compte Apple", re.I), "c_no_account", "No Apple ID"),
    (re.compile(r"compte .*déconnecté|reconnecte-le", re.I), "c_disconnected", "Apple ID signed out"),
    (re.compile(r"compte .*(introuvable|retiré)|account .*not found", re.I), "c_account_missing", "Apple ID not found"),
    (re.compile(r"-80045|cannot be completed at this time", re.I), "c_apple_blocked", "Apple is temporarily refusing the sign-in (-80045)"),
    (re.compile(r"(GrandSlam|GSA|developer services) (answered|returned) HTTP 429|Apple answered HTTP 429 to the anisette|Apple limite le nombre de requêtes|Too Many Requests", re.I), "c_rate_limited", "Apple is limiting requests (HTTP 429)"),
    (re.compile(r"(GrandSlam|GSA|developer services) (answered|returned) HTTP 50[234]|Apple answered HTTP 50[234] to the anisette|Apple répond HTTP 50[234]|50[234] Service Temporarily Unavailable", re.I), "c_gsa_503", "Apple refuses the request (HTTP 503)"),
    (re.compile(r"SMS 2FA refused|Access to this Apple Developer Service was denied|refuse les codes par SMS", re.I), "c_sms_denied", "Text-message code refused by Apple"),
    (re.compile(r"-22411\b"), "c_never_signed_in", "Apple ID never used on an Apple device (-22411)"),
    (re.compile(r"only accepts Windows anisette|ne fournit pas de données Windows", re.I), "c_anisette_unsupported", "Anisette server not accepted by Apple"),
    (re.compile(r"-22421\b|-22409\b|-80009\b|MID is invalid|-45061|not provisioned", re.I), "c_anisette_refused", "Apple refuses the anisette data"),
    (re.compile(r"anisette", re.I), "c_anisette", "Authentication service unavailable"),
    (re.compile(r"reconnexion|-22406|HTTP Some\(401\)|unauthori[sz]ed|session .*(expir|invalid)|not logged|log ?in required", re.I), "c_session", "Apple session expired"),
    (re.compile(r"plumesign occupé", re.I), "c_busy", "Apple ID busy"),
    (re.compile(r"verrouillé depuis son démarrage|locked since (boot|it started)", re.I), "c_locked", "Device locked since it started"),
    (re.compile(r"l'installation n'avance plus|installation bloquée à \d+ % depuis \d+ min \(appareil", re.I), "c_stalled", "Installation stuck"),
    (re.compile(r"ouverte sur l'Apple TV|app ouverte sur la TV|installd ne progresse plus|installation (bloquée|en attente)", re.I), "c_app_open", "Installation stalled on the Apple TV"),
    (re.compile(r"n'est plus installée|n'apparaît pas dans les apps", re.I), "c_not_installed", "App missing from the device"),
    (re.compile(r"plumesign trop ancien", re.I), "c_plumesign_old", "plumesign too old"),
    (re.compile(r"réseau ou Apple injoignable", re.I), "c_apple_net", "Apple temporarily unreachable"),
    (re.compile(r"identifiant d'app changé", re.I), "c_bundle", "New version refused: app identifier changed"),
    (re.compile(r"empreinte SHA-256", re.I), "c_digest", "New version refused: file altered"),
    (re.compile(r"certificat|certificate", re.I), "c_cert", "Apple certificate refused"),
    (re.compile(r"injoignable|unreachable|no route|connection refused|tunnel|introuvable sur le réseau|NOT_FOUND|discover", re.I), "c_unreachable", "Device unreachable"),
    (re.compile(r"interrompu", re.I), "c_interrupted", "Renewal interrupted"),
    (re.compile(r"délai dépassé|trop long|timed? ?out|timeout", re.I), "c_timeout", "Timed out"),
    (re.compile(r"plumesign.*(introuvable|not found)|No such file|cannot find the file", re.I), "c_plumesign", "Signing tool not found"),
    (re.compile(r"IPA d'origine absente"), "c_ipa_missing", "The app's IPA file is missing"),
    (re.compile(r"aucune IPA|HTTP Error 4\d\d|téléchargement|download|pas une app tvOS|IPA|https:\/\/ sont acceptées"), "c_source", "Problem with the app's source"),
]


def english(text: str) -> str | None:
    """Titre anglais de la première cause reconnue dans ce message d'erreur, ou None."""
    for rx, _key, title in CAUSES:
        if rx.search(text or ""):
            return title
    return None
