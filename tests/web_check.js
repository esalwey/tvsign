// Contrôles de l'interface web sans navigateur : traductions fr/en complètes, erreurs en clair,
// codes de l'API, connexion Apple, journal, dépôt GitHub, ntfy. Lancé par tests/test_web.py :
//     node tests/web_check.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const WEB = path.join(__dirname, "..", "web");
const lire = (f) => fs.readFileSync(path.join(WEB, f), "utf-8");

const ctx = vm.createContext({ console, URL, navigator: { languages: ["en"] } });
vm.runInContext(lire("i18n.js") + "\n" + lire("errors.js"), ctx, { filename: "web/i18n.js+errors.js" });
const ev = (code) => vm.runInContext(code, ctx);
const langue = (l) => ev(`setLang(${JSON.stringify(l)})`);

let echecs = 0, faits = 0;
function test(nom, fn) {
  faits++;
  try { fn(); } catch (e) { echecs++; console.log(`ECHEC ${nom}: ${e.message}`); }
}
const eq = (a, b, msg = "") => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${msg} attendu ${JSON.stringify(b)}, obtenu ${JSON.stringify(a)}`); };
const ok = (c, msg) => { if (!c) throw new Error(msg || "faux"); };

const I18N = ev("I18N");

// ---------- traductions ----------
test("chaque clé a un texte français et anglais non vides", () => {
  const vides = Object.entries(I18N).filter(([, v]) => !Array.isArray(v) || v.length !== 2 || !v[0] || !v[1]).map(([k]) => k);
  eq(vides, []);
});
test("mêmes variables {x} en français et en anglais", () => {
  const vars = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join(",");
  const diff = Object.entries(I18N).filter(([, [fr, en]]) => vars(fr) !== vars(en)).map(([k]) => k);
  eq(diff, []);
});
test("toutes les clés utilisées par le code existent", () => {
  const sources = ["app.js", "errors.js"].map(lire).join("\n");
  const html = lire("index.html");
  const utilisees = new Set();
  for (const m of sources.matchAll(/\bt\("([a-z0-9_]+)"[,)]/g)) utilisees.add(m[1]);
  for (const m of sources.matchAll(/\btn\("([a-z0-9_]+)"[,)]/g)) { utilisees.add(m[1] + "_one"); utilisees.add(m[1] + "_other"); }
  for (const m of sources.matchAll(/(?:titreEtape|conseilEdition)\("([a-z0-9_]+)"/g)) utilisees.add(m[1]);
  for (const m of sources.matchAll(/\bt\([^"()]*\?\s*"([a-z0-9_]+)"\s*:\s*"([a-z0-9_]+)"/g)) { utilisees.add(m[1]); utilisees.add(m[2]); }
  for (const m of html.matchAll(/data-i18n(?:-placeholder|-aria|-title)?="([a-z0-9_]+)"/g)) utilisees.add(m[1]);
  // clés construites : préfixe + valeur
  for (const k of ["st_checking", "st_signing", "st_uploading", "st_installing", "wiz_welcome", "wiz_check", "wiz_account", "wiz_tv", "wiz_app", "wiz_done",
    "diag_plumesign", "diag_mdns", "diag_apple", "diag_anisette", "diag_github", "diag_clock", "diag_disk", "diag_event_loop", "diag_ok", "diag_warn", "diag_error",
    "diag_plumesign_fix", "diag_mdns_fix", "diag_apple_fix", "diag_anisette_fix", "diag_github_fix", "diag_clock_fix", "diag_disk_fix", "diag_event_loop_fix",
    "tr_auto", "tr_manual", "tr_added", "tr_new_version", "tr_new_source", "tr_new_dest"]) utilisees.add(k);
  const causes = ev("CAUSES.map((c) => c[1])").concat(ev("LOGIN_CAUSES.map((c) => c[1])"), ["c_login_refused"]);
  for (const c of causes) { utilisees.add(c + "_t"); utilisees.add(c + "_a"); }
  for (const e of ev("EVENEMENTS.map((c) => c[1])")) utilisees.add(e);
  const manquantes = [...utilisees].filter((k) => !I18N[k]);
  eq(manquantes, []);
});
test("les variantes _win ont leur texte commun", () => {
  const sansBase = Object.keys(I18N).filter((k) => k.endsWith("_win") && !I18N[k.slice(0, -4)]);
  eq(sansBase, []);
});

// ---------- codes de l'API ----------
test("code simple et code avec paramètre", () => {
  langue("fr");
  eq(ev(`apiError("bad_password", 401)`), "Mot de passe incorrect.");
  eq(ev(`apiError("too_large:2048", 413)`), "Fichier trop gros (limite 2048 Mo).");
  langue("en");
  eq(ev(`apiError("not_tvos:iphoneos", 400)`), "This app is built for “iphoneos”: only tvOS apps are accepted.");
});
test("codes ajoutés pour les textes autrefois en français", () => {
  langue("en");
  for (const c of ["session_expired", "no_input_expected", "invalid_address", "url_expected", "busy", "not_an_upload", "bundle_mismatch:com.example.app", "tv_listing_empty", "nothing_to_accept"]) {
    const m = ev(`apiError(${JSON.stringify(c)}, 400)`);
    ok(m && m !== c && !/[éèàç]/.test(m), `${c} -> ${m}`);
  }
  eq(ev(`apiError("set_by_env:TVSIGN_NTFY_URL", 400)`), "Set by TVSIGN_NTFY_URL.");
  eq(ev(`apiError("range:12:150", 400)`), "A value between 12 and 150 is expected.");
});
test("erreurs de réglages {champ: code}", () => {
  langue("en");
  eq(ev(`apiError({renew_before_hours: "range:12:150", ntfy_url: "url_expected"}, 400)`),
    "Renew before expiry : A value between 12 and 150 is expected., ntfy : An http(s):// address is expected.");
  langue("fr");
  eq(ev(`apiError({renew_before_hours: "range:12:150"}, 400)`), "Renouveler avant expiration : Valeur attendue entre 12 et 150.");
});
test("texte libre : cause connue traduite, sinon tel quel", () => {
  langue("en");
  const m = ev(`apiError("HTTP Error 403: rate limit exceeded", 400)`);
  ok(/request limit/i.test(m) && /TVSIGN_GITHUB_TOKEN/.test(m), m);
  eq(ev(`apiError("quelque chose d'inattendu", 400)`), "quelque chose d'inattendu");
});
test("statuts sans détail", () => {
  langue("en");
  eq(ev(`apiError("", 404)`), "Not found");
  for (const s of [502, 503, 504]) ok(/did not answer in time/.test(ev(`apiError("", ${s})`)), String(s));      // proxy ou redémarrage
  ok(/server error/i.test(ev(`apiError("", 500)`)));
  eq(ev(`apiError("quelque chose", 502)`), "quelque chose");                  // un 502 du serveur garde son détail
  eq(ev(`apiError("", 418)`), "Error 418");
});

// ---------- causes connues ----------
const cle = (m) => ev(`enClair(${JSON.stringify(m)}).cle`);
test("limite de requêtes GitHub", () => {
  eq(cle("HTTP Error 403: rate limit exceeded"), "c_gh_rate");
  eq(cle("limite de requêtes GitHub atteinte"), "c_gh_rate");
  eq(cle("HTTP Error 404: Not Found"), "c_source");
});
test("installation bloquée : plusieurs causes possibles, pas « app ouverte » affirmé", () => {
  const msg = "installd ne progresse plus depuis 30 min sur « TV », installation abandonnée : Gate est probablement ouverte sur l'Apple TV (ferme-la pour terminer la mise à jour), sinon vérifie l'espace de stockage";
  eq(cle(msg), "c_app_open");
  langue("en");
  const e = ev(`enClair(${JSON.stringify(msg)})`);
  ok(!/^App open/i.test(e.titre), e.titre);
  ok(/storage/i.test(e.conseil) && /open/i.test(e.conseil), e.conseil);
  ok(/storage/i.test(ev(`t("wait_app", {tv: "x", when: "y"})`)));
  ok(!/^Close /.test(ev(`t("wait_app_t", {name: "Gate"})`)));
});
test("appairage absent du store", () => eq(cle("appairage absent du store plumesign"), "c_lost"));
test("c_session reste pour les renouvellements", () => eq(cle("session Apple expirée, reconnexion nécessaire"), "c_session"));

// ---------- connexion Apple ----------
const connexion = (log) => ev(`causeConnexion(${JSON.stringify(log)})`);
const LOG = (code, msg) => `[2026-10-15T10:00:00Z INFO plumesign::commands::account] Signing in\n[2026-10-15T10:00:02Z ERROR plumesign] Authentication SRP error ${code}: ${msg}`;
test("mauvais mot de passe : jamais « session expirée »", () => {
  langue("en");
  for (const code of ["-22406", "-20101"]) {
    const c = connexion(LOG(code, "Your Apple ID or password is incorrect."));
    eq(c.cle, "c_login_bad_password", code);
    ok(!/expired/i.test(c.titre), c.titre);
  }
  eq(connexion("HTTP Some(401) unauthorized").cle, "c_login_bad_password");
});
test("compte verrouillé, deux facteurs, conditions, code faux", () => {
  eq(connexion(LOG("-20209", "This Apple ID has been locked for security reasons.")).cle, "c_login_locked");
  eq(connexion(LOG("-22421", "Two-factor authentication is required for this account.")).cle, "c_login_no_2fa");
  eq(connexion(LOG("-20123", "You must accept the terms and conditions.")).cle, "c_login_terms");
  eq(connexion(LOG("-21669", "Incorrect verification code.")).cle, "c_login_bad_code");
});
test("erreur SRP inconnue : message d'Apple repris", () => {
  langue("en");
  const c = connexion(LOG("-99999", "Something odd"));
  eq(c.cle, "c_login_refused");
  ok(c.conseil.includes("Something odd"), c.conseil);
});
test("le bruit du journal ne change pas le diagnostic", () => {
  eq(connexion(LOG("-22406", "Your Apple ID or password is incorrect.").replace("Signing in", "anisette disabled, device locked")).cle, "c_login_bad_password");
});
test("sans ligne d'erreur SRP, un mot égaré du journal ne fait pas un diagnostic", () => {
  langue("en");
  const sansSrp = (bruit) => connexion(`[INFO plumesign] ${bruit}`);
  eq(sansSrp("mutex locked, waiting").cle, null);                       // pas « compte verrouillé »
  eq(sansSrp("Two-factor prompt displayed").cle, null);                 // pas « pas de 2FA »
  eq(sansSrp("accept the terms of the agreement").cle, null);
  eq(sansSrp("operation timed out").cle, "c_timeout");                  // les causes générales restent
});
test("erreur SRP inconnue : le bruit du journal ne remplace pas le message d'Apple", () => {
  langue("en");
  const c = connexion(LOG("-99999", "Something odd").replace("Signing in", "anisette download IPA timeout"));
  eq(c.cle, "c_login_refused");
});
test("journal de session sans horodatage", () => {
  eq(ev(`journalSession(["[2026-10-15T10:00:00Z INFO plumesign] bonjour", "sans préfixe"])`), "bonjour\nsans préfixe");
});

// ---------- 2FA par SMS ----------
test("numéros de téléphone de confiance", () => {
  const p = "[INFO] Enter the code sent to your device, or type 'sms' to receive it by text message (numbers ending in 12, 34; 'sms 12' picks one): ";
  eq(ev(`numerosSms(${JSON.stringify(p)})`), ["12", "34"]);
  eq(ev(`numerosSms("Enter the code sent to your device, or type 'sms' to receive it by text message: ")`), []);
  eq(ev(`numerosSms("Enter the code sent via SMS: ")`), []);
});

// ---------- dépôt GitHub ----------
test("adresse collée du navigateur -> owner/projet", () => {
  const n = (v) => ev(`normaliserDepot(${JSON.stringify(v)})`);
  eq(n("owner/project"), "owner/project");
  eq(n("https://github.com/owner/project"), "owner/project");
  eq(n("https://www.github.com/owner/project/"), "owner/project");
  eq(n("https://github.com/owner/project.git"), "owner/project");
  eq(n("https://github.com/owner/project/releases/tag/v1.2"), "owner/project");
  eq(n("http://github.com/owner/my.repo?tab=readme"), "owner/my.repo");
  eq(n("git@github.com:owner/project.git"), "owner/project");
  eq(n("  owner/project.git "), "owner/project");
  ok(!ev(`depotValide(normaliserDepot("pas un dépôt"))`));
  ok(ev(`depotValide(normaliserDepot("https://github.com/o/p"))`));
});

// ---------- ntfy ----------
test("sujet ntfy", () => {
  const u = ev(`sujetNtfy([0, 1, 35, 36, 255, 7, 8, 9, 10, 11])`);
  ok(/^https:\/\/ntfy\.sh\/tvsign-[a-z0-9]{10}$/.test(u), u);
});

// ---------- journal ----------
test("journal en anglais : lignes connues traduites", () => {
  langue("en");
  const e = (m) => ev(`evenementTexte(${JSON.stringify(m)})`);
  eq(e("App ajoutée : NuvioTV 1.0 (com.nuvio.tv)"), "App added: NuvioTV 1.0 (com.nuvio.tv)");
  eq(e("Compte Apple a@b.c connecté"), "Apple ID a@b.c signed in");
  eq(e("NuvioTV : téléchargement de v1.2 (empreinte SHA-256 vérifiée)"), "NuvioTV: downloading v1.2");
  eq(e("NuvioTV : renouvelée (auto), valable jusqu'au 20/10 10:00, prochain renouvellement auto le 18/10 22:00"),
    "NuvioTV: renewed (automatic), valid until 20/10 10:00, next automatic renewal on 18/10 22:00");
  eq(e("NuvioTV : installation sur « Salon »"), "NuvioTV: installing on “Salon”");
  ok(/on hold/.test(e("NuvioTV : installation en attente, l'app est sans doute ouverte sur « Salon » ; elle se terminera dès sa fermeture")));
  eq(e("ligne inconnue du serveur"), null);
});
test("journal en français : rien n'est modifié", () => {
  langue("fr");
  eq(ev(`evenementTexte("App ajoutée : NuvioTV 1.0 (com.nuvio.tv)")`), null);
});
// Les lignes écrites par le serveur depuis la 1.1 (appairage USB, retour arrière, versions, anisette…), telles qu'il les écrit.
test("journal en anglais : lignes du serveur de la 1.1", () => {
  langue("en");
  const e = (m) => ev(`evenementTexte(${JSON.stringify(m)})`);
  const cas = [
    ["iPad « iPad de Zoé » appairé en USB", "iPad “iPad de Zoé” paired over USB"],
    ["Appairage USB annulé", "USB pairing cancelled"],
    ["Appairage USB échoué : usbmuxd is not reachable (x)", "USB pairing failed: Apple's USB driver not found"],
    ["iPhone « Mon iPhone » : fichier d'appairage importé", "iPhone “Mon iPhone”: pairing file imported"],
    ["Appairage de Salon annulé", "Pairing of Salon cancelled"],
    ["Infos de Salon non récupérées pour l'instant : Apple TV « Salon » introuvable sur le réseau (éteinte ou en veille ?)", "Details of Salon not read yet: Device unreachable"],
    ["App ajoutée : Nuvio 1.0 (com.nuvio.tv), copie pour une autre Apple TV", "App added: Nuvio 1.0 (com.nuvio.tv), copy for another Apple TV"],
    ["IPA remplacée : Nuvio 2.0", "IPA replaced: Nuvio 2.0"],
    ["Nuvio : version v2.0 disponible (mises à jour automatiques désactivées)", "Nuvio: version v2.0 is available (automatic updates are off)"],
    ["Nuvio : déjà à jour", "Nuvio: already up to date"],
    ["Nuvio : version 2.0 installée", "Nuvio: version 2.0 installed"],
    ["Nuvio : retour à la version précédente 1.0", "Nuvio: back to the previous version 1.0"],
    ["Nuvio : retour à la version précédente", "Nuvio: back to the previous version"],
    ["Nuvio : la version v2 n'a pas pu être installée (installd ne progresse plus depuis 30 min), nouvel essai avec la version précédente v1",
      "Nuvio: version v2 could not be installed (Installation stalled on the Apple TV), trying the previous version v1"],
    ["Nuvio : la version v2 n'a pas pu être installée (installd ne progresse plus depuis 30 min) ; la version précédente est réinstallée et la nouvelle ignorée",
      "Nuvio: version v2 could not be installed (Installation stalled on the Apple TV); the previous version is installed again and the new one skipped"],
    ["Nuvio : la version épinglée « v9 » est introuvable dans o/r (tag mal écrit, release supprimée ou sans IPA ?), version actuelle conservée",
      "Nuvio: the pinned version “v9” does not exist in o/r, current version kept"],
    ["Nuvio : mise à jour en attente, Apple TV « Salon » introuvable sur le réseau", "Nuvio: update on hold, Device unreachable"],
    ["Nuvio : mise à jour impossible, limite du compte gratuit atteinte", "Nuvio: update not possible, Free account limit reached"],
    ["Nuvio : tvOS 27.1 sur l'Apple TV, la version refusée (tvOS 27.0 minimum) est de nouveau candidate",
      "Nuvio: tvOS 27.1, the refused version (tvOS 27.0 minimum) is a candidate again"],
    ["Nuvio : installation en attente sur « iPhone » (appareil verrouillé, occupé ou presque plein ?)",
      "Nuvio: installation on hold on “iPhone” (device locked, busy or nearly full?)"],
    ["Nuvio : le profil obtenu expire le Sat 20 Oct 10:00, dans la fenêtre de renouvellement (60 h) : prochain essai dans 6 h",
      "Nuvio: the profile obtained expires on Sat 20 Oct 10:00, inside the renewal window (60 h): next attempt in 6 h"],
    ["« Salon » : mode développeur désactivé, les apps signées ne s'y lanceront pas (Réglages › Confidentialité et sécurité › Mode développeur)",
      "“Salon”: Developer Mode is off, signed apps will not launch there (Settings › Privacy & Security › Developer Mode)"],
    ["« iPhone de Zoé » est de retour sur le réseau : renouvellement de Gate", "“iPhone de Zoé” is back on the network: renewing Gate"],
    ["tvsign 1.2.0 est disponible (version installée : 1.1.0)", "tvsign 1.2.0 is available (installed version: 1.1.0)"],
    ["Connexion Apple a@b.c échouée : Error: Authentication SRP error -22406: Your Apple ID or password was entered incorrectly.",
      "Apple sign-in of a@b.c failed: Apple ID or password refused"],
    ["Connexion Apple a@b.c annulée", "Apple sign-in of a@b.c cancelled"],
    ["Compte Apple a@b.c valide : reprise de 2 renouvellement(s) en attente", "Apple ID a@b.c valid: 2 waiting renewal(s) resumed"],
    ["notification ntfy échouée : HTTP 404", "Notification to ntfy failed: HTTP 404"],
    ["serveur anisette https://a.example en échec, essai avec https://b.example", "Anisette server https://a.example failed, trying https://b.example"],
    ["Serveur anisette ani.sidestore.app retiré, remplacé par le serveur par défaut (https://ani.jkcoxson.com) : Apple refuse les données des serveurs anisette Android depuis le 12 octobre 2026 (erreur -80045)",
      "Anisette server ani.sidestore.app removed: Apple refuses the data of Android anisette servers since 12 October 2026"],
    ["Identité anisette réinitialisée (Apple refuse les données anisette : signature) : une nouvelle sera créée au prochain essai",
      "Anisette identity reset (Apple refused the anisette data): a new one will be created at the next attempt"],
    ["Équipe ABCDE12345 choisie pour a@b.c : les prochaines signatures l'utilisent", "Team ABCDE12345 chosen for a@b.c: the next signatures use it"],
    ["Nuvio : signature avec le certificat importé « Mon cert »", "Nuvio: signing with the imported certificate “Mon cert”"],
    ["Nuvio : signature avec a@b.c", "Nuvio: signing with a@b.c"],
  ];
  for (const [fr, en] of cas) eq(e(fr), en, fr);
  ok(/^Android anisette identity dropped/.test(e("Identité anisette Android abandonnée (Apple refuse ses données depuis le 12 octobre 2026) : une identité Windows sera créée au prochain contact avec Apple")));
  ok(/^Restore refused \(restore_failed\): /.test(e("Restauration refusée (restore_failed) : restauration annulée, rien n'a changé : [WinError 5]")));
  for (const [, en] of cas) ok(!/[{}]/.test(en), en);
});
test("journal : chaque entrée a ses variables en français et en anglais", () => {
  const motifs = ev("EVENEMENTS");
  for (const [, k, noms] of motifs) {
    const [fr, en] = I18N[k];
    for (const n of noms) ok(fr.includes(`{${n}}`) && en.includes(`{${n}}`), `${k} : {${n}}`);
  }
});

// ---------- diagnostic ----------
test("détails de diagnostic traduits d'après leur code", () => {
  langue("en");
  const d = (c) => ev(`diagDetail(${JSON.stringify(c)}, {data_dir: "/data"})`);
  eq(d({ code: "free", value: 7.2, detail: "7.2 Go libres dans /data" }), "7.2 GB free in /data");
  eq(d({ code: "skew", value: 3, detail: "écart +3 s avec l'heure d'Apple" }), "+3 s off Apple's time");
  eq(d({ code: "found", value: { total: 2, ours: 1, pairable: 1 }, detail: "x" }), "2 announcement(s), 1 paired with tvsign, 1 waiting to be paired");
  eq(d({ code: null, detail: "https://github.com/ (HTTP 200)" }), "https://github.com/ (HTTP 200)");
});
test("conseils propres à l'édition", () => {
  langue("en");
  ok(/docker/i.test(ev(`conseilEdition("restart_hint", "docker")`)));
  ok(/right-click/.test(ev(`conseilEdition("restart_hint", "windows")`)));
  ok(/plumesign/.test(ev(`conseilEdition("diag_mdns_fix", "windows")`)));
  ok(!/tvsign through the firewall/.test(ev(`conseilEdition("diag_mdns_fix", "docker")`)));
  eq(ev(`conseilEdition("diag_clock_fix", "windows")`), ev(`conseilEdition("diag_clock_fix", "docker")`));
  eq(ev(`conseilEdition("n_existe_pas", "windows")`), "");
});
test("assistant : la machine ne peut pas être « en veille » (veille = pas de renouvellement)", () => {
  langue("en");
  for (const ed of ["windows", "docker"]) {
    const txt = ev(`conseilEdition("wiz_done_p2", ${JSON.stringify(ed)})`);
    ok(/renews nothing|doesn't run|nothing gets renewed/.test(txt), txt);
    ok(!/on or asleep/.test(txt), txt);
  }
  langue("fr");
  ok(!/allumées ou en veille/.test(ev(`conseilEdition("wiz_done_p2", "docker")`)));
});

// ---------- structure de la page ----------
test("la page charge errors.js entre i18n.js et app.js et #resume n'est plus traduit par data-i18n", () => {
  const html = lire("index.html");
  ok(html.indexOf("i18n.js") < html.indexOf("errors.js") && html.indexOf("errors.js") < html.indexOf("app.js"));
  ok(!/id="resume"[^>]*data-i18n/.test(html));
});
test("le titre de l'assistant est focalisable (focus clavier entre les étapes)", () => {
  ok(/id="wiz-titre" tabindex="-1"/.test(lire("app.js")));
});

// ---------- relecture de la 1.1 ----------
// La feuille d'une destination de notification n'est pas validée par le navigateur : le serveur renvoie un webhook ou un
// lien Discord sous la forme « ******** » (secret), qu'un champ type=url refusait (« Please enter a URL ») au moment
// d'enregistrer les seuls événements ; un sujet ntfy sans « https:// » (« mon-sujet ») était refusé de la même façon.
test("feuille des notifications : pas de validation du navigateur, le serveur valide", () => {
  const html = lire("index.html");
  ok(/<form id="form-notif"[^>]*\bnovalidate\b/.test(html), "form-notif sans novalidate");
  const champs = lire("extras.js").match(/NOTIF_CHAMPS = \{[\s\S]*?\n\};/)[0];
  ok(/webhook: \[\["url", "url", true/.test(champs) && /discord: \[\["url", "url", true/.test(champs));    // les champs secrets de type url
  for (const f of ["form-reglages", "form-mdp"]) ok(!new RegExp(`<form id="${f}"(?![^>]*novalidate)`).test(html), f);
});
test("réglages : les certificats importés ne s'offrent à la sauvegarde que s'il y en a", () => {
  ok(/<div id="backup-certs-row" hidden>/.test(lire("index.html")));
  ok(/\$\("#backup-certs-row"\)\.hidden = !avecCerts/.test(lire("app.js")));
});
test("assistant : l'étape de l'appareil ne renvoie pas à une feuille qui n'est pas ouverte", () => {
  ok(/<ol class="consigne">\$\{t\("pair_steps_wiz"\)\}<\/ol>/.test(lire("app.js")));
  langue("en");
  ok(!/this sheet/i.test(ev(`t("pair_steps_wiz")`)));
  ok(/this sheet/i.test(ev(`t("pair_steps")`)));            // la feuille, elle, est ouverte
});
test("assistant : les textes de l'installation valent pour une Apple TV, un iPhone et un iPad", () => {
  for (const l of ["fr", "en"]) {
    langue(l);
    for (const k of ["wiz_app_installed", "wiz_app_failed_hint", "wiz_skip_app_warning", "wiz_tv_done", "wiz_app_wait_phone", "wiz_done_open_phone"]) {
      ok(!/Apple TV|la TV|the TV/.test(ev(`t(${JSON.stringify(k)})`)), `${l} ${k}`);
    }
  }
  const { wiz_done_open_phone: [fr, en] } = { wiz_done_open_phone: I18N.wiz_done_open_phone };
  ok(fr.includes("{device}") && en.includes("{device}"));
  const app = lire("app.js");
  ok(/estMobile\(premiere\.device_family\)/.test(app) && /phone_trust_t/.test(app) && /wiz_done_dev_t/.test(app));   // confiance + mode développeur
});
test("menus cités : « Appareils › Appairer », jamais une section « Apple TV »", () => {
  for (const [k, [fr, en]] of Object.entries(I18N)) {
    ok(!/section Apple TV|Apple TV section|écran principal › Apple TV|main screen › Apple TV|Apple TV › (?:Appairer|Pair|Adresse|Address)/.test(fr + " " + en), k);
  }
  langue("en");
  ok(/main screen › Devices › Pair/.test(ev(`t("wiz_missing_tv")`)));
  ok(/Devices section › Pair/.test(ev(`t("c_legacy_a")`)));
});
test("renouvellement : jamais « renouvelée tous les 7 jours », les profils durent 7 jours", () => {
  for (const [k, [fr, en]] of Object.entries(I18N)) {
    if (k === "ic_note") continue;          // « n'est pas renouvelée tous les 7 jours » : la négation est voulue
    ok(!/renews? every 7 days|renewal every 7 days|renouvel\S* tous les 7 jours/i.test(fr + " " + en), k);
  }
  langue("en");
  ok(/last 7 days and are renewed before they expire/.test(ev(`t("welcome_p3")`)));
});
test("certificats : le texte de l'assistant dit ce que fait le réglage par défaut", () => {
  langue("en");
  const t_ = ev(`t("wiz_account_revoke")`);
  ok(/only replaces its own certificates/.test(t_) && /Apple account full/.test(t_) && /dedicated Apple ID/.test(t_), t_);
  ok(!/revokes their certificate when it signs/.test(t_));
  langue("fr");
  ok(/ne remplace que ses propres certificats/.test(ev(`t("wiz_account_revoke")`)));
});
test("certificat importé : une voie facultative, aucun service de signature n'est évoqué", () => {
  for (const [k, [fr, en]] of Object.entries(I18N)) {
    if (!/^ic_|^c_ic_|^err_cert_|backup_certs/.test(k)) continue;
    ok(!/provider|fournisseur|service de signature (de|d')/i.test(fr + " " + en) || /neither recommends|ne recommande/.test(fr + " " + en), k);
  }
  langue("en");
  const note = ev(`t("ic_note")`);
  ok(/optional/.test(note) && /supported route is the free Apple ID/.test(note) && /neither recommends nor contacts any signing service/.test(note), note);
  ok(!/provider you chose/.test(note));
  langue("fr");
  ok(/ne recommande ni ne contacte aucun service de signature/.test(ev(`t("ic_note")`)));
});
test("canal « Bêtas aussi » : le même libellé partout", () => {
  langue("en");
  for (const k of ["err_no_release_beta", "c_no_release_a"]) ok(/Betas too/.test(ev(`t(${JSON.stringify(k)}, {n: "v1"})`)), k);
  langue("fr");
  for (const k of ["err_no_release_beta", "c_no_release_a"]) ok(/Bêtas aussi/.test(ev(`t(${JSON.stringify(k)}, {n: "v1"})`)), k);
});
test("version épinglée introuvable : la carte l'explique et renvoie aux réglages", () => {
  const app = lire("app.js");
  ok(/a\.update_block === "pin_missing"/.test(app) && /pin_missing_t/.test(app));
  langue("en");
  ok(/Pinned version not found: v9/.test(ev(`t("pin_missing_t", {tag: "v9"})`)));
});
test("pied de page des apps : un seul délai, ou la fourchette quand les appareils diffèrent", () => {
  langue("en");
  ok(/Automatic renewal 60 h before expiry\./.test(ev(`t("apps_footer", {h: 60, m: 45})`)));
  ok(/Automatic renewal 60 to 96 h before expiry/.test(ev(`t("apps_footer_range", {h: 60, hp: 96, m: 45})`)));
  ok(/Math\.min\(\.\.\.delais\)/.test(lire("app.js")));
});
test("le registre du texte de l'interface est le tutoiement partout", () => {
  for (const k of ["pair_none_host_hint"]) {
    const fr = I18N[k][0];
    ok(!/\bvous\b|\bvotre\b|\bvos\b/i.test(fr), `${k} : ${fr}`);
  }
});

console.log(`${faits - echecs}/${faits} contrôles web réussis`);
process.exit(echecs ? 1 : 0);
