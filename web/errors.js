// Erreurs en clair, sans DOM : codes stables de l'API, causes connues de plumesign et du serveur,
// connexion Apple, journal, diagnostic. Chargé après i18n.js et avant app.js ; testé par tests/test_web.py.

// ---------- erreurs de l'API ----------
// Le serveur renvoie un code stable (« code » ou « code:paramètre »), ou {champ: code} pour les réglages.
// Un texte libre (message de plumesign, d'un téléchargement…) passe par les causes connues ci-dessous.
function apiError(detail, status) {
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    return Object.entries(detail).map(([k, v]) => `${I18N["field_" + k] ? t("field_" + k) : k} : ${apiError(String(v))}`).join(", ");
  }
  if (Array.isArray(detail)) detail = detail.map((d) => d?.msg || "").filter(Boolean).join(", ");
  const brut = String(detail || "");
  const [code, ...reste] = brut.split(":");
  const arg = reste.join(":").trim();
  if (I18N["err_" + code.trim()]) return t("err_" + code.trim(), { n: arg, env: arg });
  if (brut) {
    const c = enClair(brut);
    if (c.connu) return c.conseil ? `${c.titre}. ${c.conseil}` : c.titre;
    return brut;
  }
  return status >= 500 ? t("err_server") : status === 404 ? t("err_not_found") : status === 422 ? t("err_invalid") : t("err_status", { n: status });
}

// ---------- causes connues ----------
// Chaque cause connue (messages du serveur et de plumesign) : un titre lisible et ce que l'utilisateur peut faire.
const CAUSES = [
  [/ntfy/i, "c_ntfy"],
  [/8220|no devices from which/i, "c_8220"],
  [/NO_TEAM|MULTIPLE_TEAMS/, "c_team"],
  [/ancienne version de tvsign/i, "c_legacy"],
  [/appairage perdu|appairage absent|PAIRING_LOST|pairing record|not paired|No stored pairing/i, "c_lost"],
  [/aucune Apple TV/i, "c_no_tv"],
  [/aucun compte Apple/i, "c_no_account"],
  [/compte .*déconnecté|reconnecte-le/i, "c_disconnected"],
  [/compte .*(introuvable|retiré)|account .*not found/i, "c_account_missing"],
  [/anisette/i, "c_anisette"],
  [/reconnexion|-22406|HTTP Some\(401\)|unauthori[sz]ed|session .*(expir|invalid)|not logged|log ?in required/i, "c_session"],
  [/plumesign occupé/i, "c_busy"],
  [/ouverte sur l'Apple TV|app ouverte sur la TV|installd ne progresse plus|installation (bloquée|en attente)/i, "c_app_open"],
  [/plumesign trop ancien/i, "c_plumesign_old"],
  [/réseau ou Apple injoignable/i, "c_apple_net"],
  [/rate.?limit|HTTP Error 429|too many requests/i, "c_github_rate"],
  [/maximum (number )?of (apps|app ids)|too many (apps|app ids)|app id limit|limit of \d+ (apps|app ids)/i, "c_limit"],
  [/identifiant d'app changé/i, "c_bundle"],
  [/empreinte SHA-256/i, "c_digest"],
  [/certificat|certificate/i, "c_cert"],
  [/injoignable|unreachable|no route|connection refused|tunnel|introuvable sur le réseau|NOT_FOUND|discover/i, "c_unreachable"],
  [/interrompu/i, "c_interrupted"],
  [/délai dépassé|trop long|timed? ?out|timeout/i, "c_timeout"],
  [/plumesign.*(introuvable|not found)|No such file|cannot find the file/i, "c_plumesign"],
  [/IPA d'origine absente/, "c_ipa_missing"],
  [/aucune IPA|HTTP Error 4\d\d|téléchargement|download|pas une app tvOS|IPA|https:\/\/ sont acceptées/, "c_source"],
];
function nettoyer(msg) {
  return String(msg || "")
    .replace(/^[^:]{1,60} : échec, /, "")
    .replace(/\s*\|\s*Caused by:[\s\S]*$/, "")
    .replace(/\[URL: [^\]]*\]/g, "")
    .replace(/https?:\/\/\S+/g, "")
    .replace(/^(\w[\w ]{0,20} : )?(Error: )?(Core error: )?/, "$1")
    .replace(/\s+/g, " ").trim();
}
function enClair(msg) {
  const brut = String(msg || "");
  for (const [re, k] of CAUSES) if (re.test(brut)) return { titre: t(k + "_t"), conseil: t(k + "_a"), cle: k, brut, connu: true };
  const court = nettoyer(brut);
  return { titre: court.length > 110 ? court.slice(0, 107) + "…" : court || t("unknown_error"), conseil: "", cle: null, brut, connu: false };
}

// ---------- connexion Apple ----------
// plumesign termine par « Authentication SRP error <code>: <message> ». Dans la feuille de connexion,
// un identifiant refusé est un mauvais mot de passe, pas une « session expirée ».
const LOGIN_CAUSES = [
  [/-20209|-20752|locked|disabled|too many (attempts|failed)|account.*(suspended|restricted)/i, "c_login_locked"],
  [/-21669|(incorrect|invalid|wrong|expired)[^.\n]{0,25}(verification|2fa|security) code|code[^.\n]{0,15}(incorrect|invalid|wrong|expired)/i, "c_login_bad_code"],
  [/terms|agreement|conditions d'utilisation/i, "c_login_terms"],
  [/two-factor|2fa[^.\n]{0,20}(not|disabled|required)|trusted (device|phone)[^.\n]{0,20}(none|no |not)/i, "c_login_no_2fa"],
  [/-22406|-20101|-22410|incorrect apple id|apple id or password|invalid (credential|password|username)|bad (password|credential)|wrong password/i, "c_login_bad_password"],
];
function srpError(log) {
  const m = String(log || "").match(/Authentication SRP error\s*(-?\d+)\s*:\s*([^\n]*)/i);
  return m ? { code: m[1], message: m[2].trim() } : null;
}
function causeConnexion(log) {
  const brut = String(log || "");
  const srp = srpError(brut);
  const texte = srp ? `${srp.code} ${srp.message}` : brut;     // la ligne d'erreur d'abord : le reste du journal est du bruit
  for (const [re, k] of LOGIN_CAUSES) if (re.test(texte)) return { titre: t(k + "_t"), conseil: t(k + "_a"), cle: k, connu: true };
  const e = enClair(brut);
  if (e.cle === "c_session") return { titre: t("c_login_bad_password_t"), conseil: t("c_login_bad_password_a"), cle: "c_login_bad_password", connu: true };
  if (e.connu) return e;
  if (srp) return { titre: t("c_login_refused_t"), conseil: t("c_login_refused_a", { why: srp.message || srp.code }), cle: "c_login_refused", connu: true };
  return e;
}
// Journal technique d'une session : sans l'horodatage et le module de chaque ligne.
const journalSession = (lines) => (lines || []).map((l) => String(l).replace(/^\[[^\]]*\]\s*/, "")).join("\n");

// 2FA : « numbers ending in 12, 34; 'sms 34' picks one » -> ["12", "34"]
function numerosSms(prompt) {
  const m = String(prompt || "").match(/numbers? ending in ([\d, ]+?)\s*[;:)]/i);
  return m ? [...new Set(m[1].split(/[,\s]+/).filter(Boolean))] : [];
}

// ---------- saisie du dépôt GitHub ----------
// Adresse collée du navigateur (https://github.com/owner/projet, .git, /releases…) -> owner/projet
function normaliserDepot(v) {
  v = String(v || "").trim();
  const m = v.match(/^(?:https?:\/\/(?:www\.)?github\.com\/|git@github\.com:)([\w.-]+)\/([\w.-]+?)(?:\.git)?(?:\/.*)?(?:[?#].*)?$/i);
  if (m) return `${m[1]}/${m[2]}`;
  v = v.replace(/\/+$/, "");
  return /\.git$/i.test(v) ? v.slice(0, -4) : v;
}
const depotValide = (v) => /^[\w.-]+\/[\w.-]+$/.test(v);

// ---------- journal : lignes du serveur, écrites en français ----------
// [motif, clé i18n, noms des groupes]. En français la ligne reste telle quelle (null) ; en anglais, les lignes
// connues sont traduites, les autres restent comme le serveur les a écrites.
const EVENEMENTS = [
  [/^App ajoutée : (.+) \((.+)\)$/, "ev_app_added", ["name", "id"]],
  [/^App supprimée : (.+)$/, "ev_app_deleted", ["name"]],
  [/^App remplacée : (.+)$/, "ev_app_replaced", ["name"]],
  [/^Changement d'identifiant d'app accepté$/, "ev_bundle_accepted", []],
  [/^Compte Apple (.+) connecté$/, "ev_account_in", ["email"]],
  [/^Compte Apple (.+) de nouveau valide$/, "ev_account_valid", ["email"]],
  [/^Compte Apple (.+) déconnecté : reconnexion nécessaire$/, "ev_account_out", ["email"]],
  [/^Apple TV « (.+) » appairée$/, "ev_tv_paired", ["name"]],
  [/^Apple TV « (.+) » : appairage plumesign existant repris$/, "ev_tv_pairing_kept", ["name"]],
  [/^Appairage existant repris : (.+)$/, "ev_pairing_kept", ["name"]],
  [/^(.+) : téléchargement de (.+?)(?: \(fichier mis à jour sous le même nom\))?(?: \(empreinte SHA-256 vérifiée\))?$/, "ev_download", ["name", "tag"]],
  [/^(.+) : signature du (.+) réutilisée$/, "ev_sign_reused", ["name", "date"]],
  [/^(.+) : signature avec (.+)$/, "ev_sign", ["name", "email"]],
  [/^(.+) : installation sur « (.+) »$/, "ev_install", ["name", "tv"]],
  [/^(.+) : renouvelée \((.+)\), valable jusqu'au (.+), prochain renouvellement auto le (.+)$/, "ev_renewed", ["name", "trigger", "until", "next"]],
  [/^(.+) : installation en attente, .*$/, "ev_install_waiting", ["name"]],
  [/^(.+) : certificat révoqué, nouvelle signature$/, "ev_cert_revoked", ["name"]],
  [/^(.+) : profil expiré le (.+)$/, "ev_expired", ["name", "date"]],
  [/^(.+) : renouvellement trop long, abandonné$/, "ev_too_long", ["name"]],
  [/^(.+) : aucune release ne correspond plus dans (.+?) \(.*\), version actuelle conservée$/, "ev_no_release", ["name", "repo"]],
  [/^Sauvegarde restaurée \((\d+) app\(s\), (\d+) Apple TV\)$/, "ev_restored", ["apps", "tvs"]],
  [/^Interface sécurisée par mot de passe$/, "ev_secured_pw", []],
  [/^Interface sécurisée \(accès local, sans mot de passe\)$/, "ev_secured_local", []],
  [/^Mot de passe de l'interface modifié$/, "ev_pw_changed", []],
  [/^Mot de passe de l'interface retiré$/, "ev_pw_removed", []],
  [/^Arrêt demandé : fin des renouvellements en cours avant de s'arrêter$/, "ev_stopping", []],
];
function evenementTexte(message) {
  const brut = String(message || "");
  if (typeof LANG !== "undefined" && LANG === "fr") return null;
  for (const [re, k, noms] of EVENEMENTS) {
    const m = brut.match(re);
    if (!m) continue;
    const vars = {};
    noms.forEach((n, i) => (vars[n] = m[i + 1]));
    if (vars.trigger) vars.trigger = declencheur(vars.trigger);
    return t(k, vars);
  }
  return null;
}
// déclencheurs enregistrés par le serveur (en français) -> libellé traduit
const declencheur = (x) => ({ auto: t("tr_auto"), manuel: t("tr_manual"), ajout: t("tr_added"), "nouvelle version": t("tr_new_version"),
  "nouvelle source": t("tr_new_source"), "nouvelle destination": t("tr_new_dest") }[x] || x);

// ---------- diagnostic ----------
// Détail d'un contrôle : traduit d'après son code et sa valeur quand le serveur l'écrit en français.
function diagDetail(c, d) {
  const v = c.value;
  switch (c.code) {
    case "missing": return t("diag_d_plumesign_missing", { path: String(c.detail || "").replace(/^[^:]*: /, "").replace(/ \(TVSIGN_PLUMESIGN\)$/, "") });
    case "no_answer": return t("diag_d_plumesign_no_answer");
    case "found": case "none": return t("diag_d_mdns", { total: v?.total ?? 0, ours: v?.ours ?? 0, pairable: v?.pairable ?? 0 });
    case "free": return t("diag_d_disk", { gb: v, dir: d?.data_dir || "" });
    case "no_subprocess": return t("diag_d_loop", { name: String(c.detail || "").split(" : ")[0] });
    case "no_date": return t("diag_d_clock_none");
    case "skew": return t("diag_d_clock", { s: (v >= 0 ? "+" : "") + v });
    default: return c.detail;
  }
}
// Conseil propre à l'édition (clé + "_win" pour l'application Windows, sinon le texte commun).
function conseilEdition(cle, edition) {
  const k = edition === "windows" && I18N[cle + "_win"] ? cle + "_win" : cle;
  return I18N[k] ? t(k) : "";
}

// ---------- notifications ntfy ----------
// Un simple nom de sujet devient une adresse ntfy.sh ; une adresse complète reste telle quelle.
function normaliserNtfy(v) {
  v = String(v || "").trim();
  return /^[\w-]+$/.test(v) ? `https://ntfy.sh/${v}` : v;
}
// Sujet privé et difficile à deviner, à partir d'octets aléatoires (crypto.getRandomValues).
function sujetNtfy(octets) {
  const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789";
  return "https://ntfy.sh/tvsign-" + Array.from(octets, (b) => alphabet[b % alphabet.length]).join("");
}
