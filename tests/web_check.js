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
    "field_language", "field_ntfy_url", "field_renew_before_hours", "field_anisette_url", "tr_auto", "tr_manual", "tr_added", "tr_new_version", "tr_new_source", "tr_new_dest"]) utilisees.add(k);
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
  for (const c of ["session_expired", "no_input_expected", "invalid_address", "invalid_url", "no_ntfy_url", "restore_busy", "hours_range", "bundle_changed"]) {
    const m = ev(`apiError(${JSON.stringify(c)}, 400)`);
    ok(m && m !== c && !/[éèàç]/.test(m), `${c} -> ${m}`);
  }
  eq(ev(`apiError("locked_by_env:TVSIGN_NTFY_URL", 400)`), "Set by TVSIGN_NTFY_URL: it can only be changed there.");
});
test("erreurs de réglages {champ: code}", () => {
  langue("en");
  eq(ev(`apiError({renew_before_hours: "hours_range", ntfy_url: "invalid_url"}, 400)`),
    "Renew before expiry : Between 12 and 150 hours., ntfy : Enter an address starting with http:// or https://.");
  langue("fr");
  eq(ev(`apiError({renew_before_hours: "hours_range"}, 400)`), "Renouveler avant expiration : Entre 12 et 150 heures.");
});
test("texte libre : cause connue traduite, sinon tel quel", () => {
  langue("en");
  const m = ev(`apiError("HTTP Error 403: rate limit exceeded", 400)`);
  ok(/rate limit/i.test(m) && /TVSIGN_GITHUB_TOKEN/.test(m), m);
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
  eq(cle("HTTP Error 403: rate limit exceeded"), "c_github_rate");
  eq(cle("HTTP Error 429: Too Many Requests"), "c_github_rate");
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
  eq(ev(`normaliserNtfy("mon-sujet")`), "https://ntfy.sh/mon-sujet");
  eq(ev(`normaliserNtfy(" https://ntfy.example.org/a ")`), "https://ntfy.example.org/a");
  eq(ev(`normaliserNtfy("")`), "");
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
  ok(/plumesign\.exe/.test(ev(`conseilEdition("diag_mdns_fix", "windows")`)));
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

console.log(`${faits - echecs}/${faits} contrôles web réussis`);
process.exit(echecs ? 1 : 0);
