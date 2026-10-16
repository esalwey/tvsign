const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* stockage indisponible */ } },
};

let STATE = null;
let AUTH = null;           // /api/auth/status
let FLASH = null;          // message éphémère (retour d'action)
let DECALAGE = 0;          // horloge serveur - horloge locale (s)
let ECHECS = 0, DEPUIS = null;   // polls /api/state ratés d'affilée
let VUE = null;            // auth | onboarding | main | settings | diagnostics
const BUSY = new Map();    // "attr=val" -> libellé pendant une action longue

const ICONS = {
  app: '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="4"/><path d="M9 12h6M12 9v6"/></svg>',
  tv: '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><rect x="2.5" y="4" width="19" height="12.5" rx="2"/><path d="M8 20h8M12 16.5V20"/></svg>',
  user: '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8.5" r="3.5"/><path d="M5 19.5c1.2-3.2 3.8-5 7-5s5.8 1.8 7 5"/></svg>',
  boucle: '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 0 1-13.7 5.7M4 12a8 8 0 0 1 13.7-5.7"/><path d="M18 2.5v4h-4M6 21.5v-4h4"/></svg>',
  cloche: '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/></svg>',
  alerte: '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5 2.5 20h19z"/><path d="M12 10v4.5M12 17.2v.1"/></svg>',
  cle: '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><circle cx="8" cy="15" r="4"/><path d="m11 12 8-8M16 7l2 2M14 9l2 2"/></svg>',
  ok: '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
};

// Dépôt GitHub : « owner/projet » ASCII sans segment « . » ni « .. » ; une adresse github.com collée est acceptée
// (le serveur applique la même règle).
function depotGithub(v) {
  let r = String(v || "").trim().replace(/^https?:\/\/(www\.)?github\.com\//i, "").replace(/\/+$/, "").replace(/\.git$/i, "");
  return /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/.test(r) && !r.split("/").some((p) => p === "." || p === "..") ? r : null;
}

// ---------- erreurs de l'API ----------
// Codes stables renvoyés par le serveur (« code » ou « code:paramètre »).
function apiError(detail, status) {
  const [code, arg, arg2] = String(detail || "").split(":");
  const k = "err_" + code;
  if (arg && I18N[k + "_n"]) return t(k + "_n", { n: arg, m: arg2 });     // variante qui nomme l'objet (ex. la TV)
  if (I18N[k]) return t(k, { n: arg, m: arg2 });
  if (detail) return String(detail);
  return status >= 500 ? t("err_server") : status === 404 ? t("err_not_found") : status === 422 ? t("err_invalid") : t("err_status", { n: status });
}

// Réglage (clé de l'API) -> libellé du champ affiché, pour qu'une erreur de validation parle comme le formulaire.
const LIBELLE_REGLAGE = { language: "language", renew_before_hours: "renew_before", anisette_url: "anisette_server",
  renew_window_start: "set_lbl_renew_window_start", renew_window_end: "set_lbl_renew_window_end",
  update_check: "update_check" };
const libelleReglage = (k) => (k === "ntfy_url" ? "ntfy" : LIBELLE_REGLAGE[k] ? t(LIBELLE_REGLAGE[k]) : k);

async function api(path, opts = {}) {
  const { json, quiet, ...init } = opts;
  init.headers = { "X-Tvsign": "1", ...(init.headers || {}) };
  if (json !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  }
  const r = await fetch(path, { credentials: "same-origin", ...init });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detail = typeof data.detail === "string" ? data.detail
      : Array.isArray(data.detail) ? data.detail.map((d) => d?.msg || "").filter(Boolean).join(", ")
      : data.detail && typeof data.detail === "object" ? Object.entries(data.detail).map(([k, v]) => `${libelleReglage(k)} : ${apiError(v)}`).join(", ")
      : "";
    const e = new Error(apiError(detail, r.status));
    e.status = r.status; e.code = detail;
    if (r.status === 401 && /^(auth|setup)_required$/.test(detail) && !quiet) demarrer();
    throw e;
  }
  return data;
}

// ---------- temps ----------
const maintenant = () => Date.now() / 1000 + DECALAGE;
const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString(LOCALE(), { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
const fmtHeure = (ts) => new Date(ts * 1000).toLocaleTimeString(LOCALE(), { hour: "2-digit", minute: "2-digit" });
function restant(sec) {           // compte à rebours compact
  if (sec <= 0) return t("dur_m", { m: 0 });
  const j = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (j >= 1) return t("dur_dh", { d: j, h });
  if (h >= 1) return t("dur_hm", { h, m: String(m).padStart(2, "0") });
  return t("dur_m", { m: Math.max(1, m) });
}
function relatif(ts, now) {
  if (!ts) return t("never");
  const d = ts - now, a = Math.abs(d);
  if (a < 60) return d < 0 ? t("just_now") : t("in_a_moment");
  if (a < 3600) { const m = Math.round(a / 60); return t(d < 0 ? "ago_min" : "in_min", { n: m }); }
  if (a < 12 * 3600) { const h = Math.round(a / 3600); return t(d < 0 ? "ago_h" : "in_h", { n: h }); }
  return t("on_date", { date: fmtDate(ts) });
}

// ---------- erreurs en clair ----------
// Chaque cause connue (messages du serveur et de plumesign) : un titre lisible et ce que l'utilisateur peut faire.
const CAUSES = [
  // Ces deux messages listent des numéros de série et des noms d'équipe : à tester avant les motifs génériques.
  [/CERT_LIMIT/, "c_certlimit"],
  [/NO_TEAM|MULTIPLE_TEAMS|TEAM_NOT_FOUND/, "c_team"],
  [/adresse introuvable/i, "c_host_unresolved"],
  [/oubli de l'appairage/i, "c_forget_failed"],
  [/recherche des Apple TV/i, "c_discover_failed"],
  [/arrêté par le système|killed by the system/i, "c_oom"],
  [/ntfy|notification .*échouée|notify_failed/i, "c_ntfy"],
  [/maximum number of (installed )?(apps|app ?ids?)|too many (installed )?(apps|app ?ids?)|app ?id limit|limit of \d+ (apps|app ?ids)|limite du compte gratuit|reached the maximum number/i, "c_limit"],
  [/limite de requêtes GitHub/i, "c_gh_rate"],
  [/jeton GitHub refusé/i, "c_gh_token"],
  [/dépôt GitHub introuvable/i, "c_gh_missing"],
  [/demande tvOS/i, "c_tvos"],
  [/aucune IPA trouvée/, "c_no_release"],
  [/8220|no devices from which/i, "c_8220"],
  [/ancienne version de tvsign/i, "c_legacy"],
  [/appairage perdu|PAIRING_LOST|pairing record|not paired|No stored pairing/i, "c_lost"],
  [/aucune Apple TV/i, "c_no_tv"],
  [/aucun compte Apple/i, "c_no_account"],
  [/compte .*déconnecté|reconnecte-le/i, "c_disconnected"],
  [/compte .*(introuvable|retiré)|account .*not found/i, "c_account_missing"],
  // refus d'Apple et problèmes d'anisette précis : avant les règles générales (anisette, session)
  [/-80045|cannot be completed at this time/i, "c_apple_blocked"],
  [/(GrandSlam|GSA|developer services) (answered|returned) HTTP 429|Apple answered HTTP 429 to the anisette|Apple limite le nombre de requêtes|Too Many Requests/i, "c_rate_limited"],
  [/(GrandSlam|GSA|developer services) (answered|returned) HTTP 50[234]|Apple answered HTTP 50[234] to the anisette|Apple répond HTTP 50[234]|50[234] Service Temporarily Unavailable/i, "c_gsa_503"],
  [/SMS 2FA refused|Access to this Apple Developer Service was denied|refuse les codes par SMS/i, "c_sms_denied"],
  [/-22411\b/, "c_never_signed_in"],
  [/only accepts Windows anisette|ne fournit pas de données Windows/i, "c_anisette_unsupported"],
  [/-22421\b|-22409\b|-80009\b|MID is invalid|-45061|not provisioned/i, "c_anisette_refused"],
  [/anisette/i, "c_anisette"],
  [/reconnexion|-22406|HTTP Some\(401\)|unauthori[sz]ed|session .*(expir|invalid)|not logged|log ?in required/i, "c_session"],
  [/plumesign occupé/i, "c_busy"],
  [/ouverte sur l'Apple TV|app ouverte sur la TV|installd ne progresse plus/i, "c_app_open"],
  [/n'est plus installée|n'apparaît pas dans les apps/i, "c_not_installed"],
  [/plumesign trop ancien/i, "c_plumesign_old"],
  [/réseau ou Apple injoignable/i, "c_apple_net"],
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
// Lignes de journal de plumesign (« [2026-10-15T19:40:36Z WARN  omnisette::… ] … »), qui racontent un repli ou un
// nouvel essai : seule l'erreur finale dit pourquoi la connexion a échoué.
const LIGNE_JOURNAL = /^\[\d{4}-\d\d-\d\dT[\d:.]+Z\s+(INFO|WARN|DEBUG|TRACE)\b/;
const erreursSeules = (log) => String(log || "").split("\n").filter((l) => !LIGNE_JOURNAL.test(l)).join("\n").trim();
function nettoyer(msg) {
  return String(msg || "")
    .replace(/^[^:]{1,60} : échec, /, "")
    .replace(/\s*\|\s*Caused by:[\s\S]*$/, "")
    .replace(/\[URL: [^\]]*\]/g, "")
    .replace(/https?:\/\/\S+/g, "")
    .replace(/^(\w[\w ]{0,20} : )?(Error: )?(Core error: )?/, "$1")
    .replace(/\s+/g, " ").trim();
}
// Message d'une erreur d'API : traduit si le serveur a envoyé un code connu, sinon texte brut passé par enClair
// (messages français des sources : « aucune IPA trouvée… », « adresse locale refusée… »).
function messageErreur(e) {
  const code = String(e.code || "").split(":")[0];
  if (I18N["err_" + code]) return e.message;
  const c = enClair(e.code || e.message);
  return c.connu ? `${c.titre}.${c.conseil ? " " + c.conseil : ""}` : e.message;
}
function enClair(msg) {
  const brut = String(msg || "");
  for (const [re, k] of CAUSES) if (re.test(brut)) return { titre: t(k + "_t"), conseil: t(k + "_a"), cle: k, brut, connu: true };
  const court = nettoyer(brut);
  return { titre: court.length > 110 ? court.slice(0, 107) + "…" : court || t("unknown_error"), conseil: "", cle: null, brut, connu: false };
}

let TOAST = null;
function flash(ton, titre, texte = "") {
  if (VUE !== "main") {         // hors de l'écran principal : bulle temporaire en bas de page
    const el = $("#toast");
    el.textContent = `${titre}${texte ? " —" + texte : ""}`;
    el.className = "toast toast--" + ton;
    el.hidden = false;
    clearTimeout(TOAST);
    TOAST = setTimeout(() => (el.hidden = true), 5000);
    return;
  }
  FLASH = { ton, titre, texte, jusqua: Date.now() + 7000 };
  renderAlertes();
  setTimeout(renderAlertes, 7100);
}

// ---------- rendu sans perte de focus ----------
const CLES = ["data-renew", "data-toggle", "data-edit", "data-del-app", "data-refresh-dev", "data-del-dev", "data-check",
  "data-select", "data-relogin", "data-logout", "data-team", "data-open", "data-key", "data-update", "data-rollback", "data-dismiss-update", "data-replace"];
function cleDe(el) {
  if (!el?.getAttribute) return null;
  for (const a of CLES) if (el.hasAttribute(a)) return [a, el.getAttribute(a)];
  return null;
}
const parCle = (racine, [a, v]) => racine.querySelector(`[${a}="${CSS.escape(v)}"]`);

// Remplace le contenu seulement s'il a changé, en gardant focus et <details> ouverts.
function setHTML(el, html) {
  if (el._html === html) return;
  const actif = el.contains(document.activeElement) ? cleDe(document.activeElement.closest("summary")?.parentElement || document.activeElement) : null;
  const ouverts = $$("details[open][data-key]", el).map((d) => d.dataset.key);
  el._html = html;
  el.innerHTML = html;
  for (const k of ouverts) { const d = parCle(el, ["data-key", k]); if (d) d.open = true; }
  applyBusy(el);
  if (actif) { const n = parCle(el, actif); (n?.tagName === "DETAILS" ? $("summary", n) : n)?.focus({ preventScroll: true }); }
}
function applyBusy(racine = document) {
  for (const [k, label] of BUSY) {
    const i = k.indexOf("=");
    const b = parCle(racine, [k.slice(0, i), k.slice(i + 1)]);
    if (b) { b.disabled = true; b.textContent = label; b.setAttribute("aria-busy", "true"); }
  }
}
async function busy(b, attr, label, fn) {
  const k = `${attr}=${b.getAttribute(attr)}`;
  if (BUSY.has(k)) return;              // double clic ignoré
  const texte = b.textContent;
  BUSY.set(k, label);
  b.disabled = true; b.textContent = label; b.setAttribute("aria-busy", "true");
  try { return await fn(); } finally {
    BUSY.delete(k);
    // setHTML ne reconstruit pas une section dont le HTML n'a pas changé : on rétablit le bouton
    const sec = b.closest("#apps,#devices,#accounts,#alertes,#systeme,#wiz-corps");
    if (sec) sec._html = null;
    if (b.isConnected) { b.disabled = false; b.textContent = texte; b.removeAttribute("aria-busy"); }
  }
}

// ---------- état d'une app ----------
// [ton, libellé] ; ton : ok | warn | err | cours | neutre
function etatApp(a, now, s) {
  const exp = a.expires_at, fenetre = s.renew_before_hours * 3600;
  const expiree = exp ? exp <= now : false;
  if (a.running) {
    const ph = a.phase || {};
    if (ph.step === "waiting_app") return ["warn", t("st_waiting_app")];
    if (ph.step === "queued") return ["cours", t("st_queued")];
    if (ph.step === "installing" && ph.percent != null) return ["cours", t("st_installing_pct", { n: ph.percent })];
    if (["checking", "signing", "uploading", "installing"].includes(ph.step)) return ["cours", t("st_" + ph.step)];
    return ["cours", t("st_renewing")];
  }
  if (!a.enabled) return ["neutre", t("st_paused")];
  if (!a.device_id) return ["err", t("st_no_tv")];
  if (exp && expiree) return ["err", t("st_expired")];
  if (a.status === "error" && a.needs_action) return ["err", t("st_action")];
  if (a.status === "error") return ["err", exp && exp - now < 24 * 3600 ? t("st_failed_soon") : t("st_failed")];
  if (!exp) return ["warn", t("st_to_install")];
  if (exp - now < fenetre) {
    // le moment est passé mais la plage horaire choisie n'est pas ouverte : rien n'est en retard
    if (a.window_wait && s.renew_window) return ["neutre", t("st_window", { h: s.renew_window[0] })];
    return ["warn", t("st_to_renew")];
  }
  return ["ok", t("st_valid")];
}
const COULEUR = { ok: "green", warn: "orange", err: "red", cours: "blue", neutre: "gray" };

const redactUrl = (u) => { try { const x = new URL(u); return x.origin + x.pathname + (x.search ? "?…" : ""); } catch { return String(u).split("?")[0]; } };
const tvOS = (a) => (STATE?.devices || []).find((d) => d.id === a.device_id)?.os_version || "?";

function sourceTexte(a) {
  if (a.source_type === "upload") return t("src_uploaded");
  if (a.source_type === "github") {
    const canal = a.channel === "beta" ? ` · ${t("channel_beta_short")}` : "";
    const epingle = a.pinned_tag ? ` · ${esc(t("pinned_badge"))}` : "";
    return `${esc(a.source)}${a.source_tag ? ` · <span class="mono">${esc(a.source_tag)}</span>` : ""}${canal}${epingle}`;
  }
  return t("src_direct_link");
}

// Mises à jour : échec d'une mise à jour facultative (l'app installée reste valable), version disponible mais
// non installée, notes de version, lien dont le serveur ne dit pas s'il change.
function noteMaj(a, now) {
  let html = "";
  if (a.update_error) {
    const e = enClair(a.update_error);
    const retry = a.next_attempt_at && a.next_attempt_at > now ? t("retry_at", { when: relatif(a.next_attempt_at, now) }) : "";
    html += `<div class="probleme probleme--warn" role="group" aria-label="${esc(t("update_failed_t"))}">
      <span class="probleme__ic">${ICONS.alerte}</span>
      <div class="probleme__texte">
        <p class="probleme__titre">${esc(t("update_failed_t"))}</p>
        <p>${esc(t("update_failed", { why: e.titre + (e.conseil ? ". " + e.conseil : "") }))}</p>
        ${retry ? `<p class="probleme__retry">${esc(retry)}</p>` : ""}
      </div></div>`;
  }
  if (a.update_tag) {
    const m = /^tvos:(.+)$/.exec(a.update_block || "");
    const quoi = a.source_type === "url" ? t("update_new_file") : a.update_tag;
    const pourquoi = m ? t("update_tvos", { min: m[1], have: tvOS(a) }) : a.update_block === "rollback" ? t("update_rolled_back") : t("update_manual");
    html += `<div class="probleme probleme--info" role="group" aria-label="${esc(t("update_available_t", { tag: quoi }))}">
      <span class="probleme__ic">${ICONS.app}</span>
      <div class="probleme__texte">
        <p class="probleme__titre">${esc(t("update_available_t", { tag: quoi }))}</p>
        <p>${esc(pourquoi)}</p>
        ${a.update_notes ? `<details class="details" data-key="notes-${a.id}"><summary>${t("release_notes")}</summary><pre class="log">${esc(a.update_notes)}</pre></details>` : ""}
      </div>
      ${m ? "" : `<button class="btn btn--petit" data-update="${a.id}" ${a.running ? "disabled" : ""}>${t("install_now")}</button>`}
    </div>`;
  } else if (a.notes) {
    html += `<details class="details" data-key="notes-${a.id}"><summary>${esc(t("release_notes_v", { v: a.version || a.source_tag || "" }))}</summary><pre class="log">${esc(a.notes)}</pre></details>`;
  }
  if (a.untracked_link) {
    html += `<p class="row__sub">${esc(t(a.auto_update === 0 ? "untracked_link_manual" : "untracked_link"))} <button class="lien lien--inline" data-update="${a.id}" ${a.running ? "disabled" : ""}>${t("check_now")}</button></p>`;
  }
  return html;
}

function jauge(a, now, s, ton) {
  const exp = a.expires_at;
  if (!exp) return "";
  const debut = a.signed_at && a.signed_at < exp ? a.signed_at : exp - 7 * 86400;
  const total = Math.max(1, exp - debut);
  const pct = Math.min(100, Math.max(0, ((exp - now) / total) * 100));
  const renouv = a.renew_at ?? exp - s.renew_before_hours * 3600;
  const posRenouv = Math.min(100, Math.max(0, ((renouv - debut) / total) * 100));
  const reste = exp - now;
  const label = reste > 0 ? t("gauge_valid", { left: restant(reste), date: fmtDate(exp) }) : t("gauge_expired", { date: fmtDate(exp) });
  return `<div class="validite">
      <p class="validite__reste">${reste > 0
        ? `<span class="validite__chiffre">${restant(reste)}</span><span class="validite__unite">${t("before_expiry")}</span>`
        : `<span class="validite__chiffre validite__chiffre--err">${t("st_expired")}</span><span class="validite__unite">${t("no_longer_launches")}</span>`}</p>
      <div class="jauge jauge--${ton}" role="img" aria-label="${esc(label)}">
        <span class="jauge__plein" style="width:${pct.toFixed(1)}%"></span>
        ${reste > 0 ? `<span class="jauge__repere" style="left:${(100 - posRenouv).toFixed(1)}%" title="${esc(t("auto_renewal"))}"></span>` : ""}
      </div>
      <p class="validite__legende"><span>${esc(t("expires", { date: fmtDate(exp) }))}</span>${a.enabled && reste > 0 && a.status !== "error" && !a.running ? `<span>${esc(t("auto_renewal_when", { when: renouv <= now ? t("asap") : relatif(renouv, now) }))}</span>` : ""}</p>
    </div>`;
}

// App ouverte sur l'Apple TV : installation en attente (installd tient la mise à jour jusqu'à sa fermeture).
function noteApp(a, now) {
  const tv = a.device_name || "Apple TV";
  let titre, texte;
  if (a.running && a.phase?.step === "waiting_app") {
    titre = t("wait_app_t", { name: a.name });
    texte = t("wait_app", { tv, when: relatif(a.phase.deadline, now) });
  } else return "";
  return `<div class="probleme probleme--info" role="status">
      <span class="probleme__ic">${ICONS.tv}</span>
      <div class="probleme__texte"><p class="probleme__titre">${esc(titre)}</p><p>${esc(texte)}</p></div>
    </div>`;
}

function problemeApp(a, now) {
  if (!a.last_error && a.device_id) return "";
  const e = enClair(a.last_error || "aucune Apple TV associée");
  const retry = !a.enabled ? t("retry_paused")
    : a.running ? ""
    : a.needs_action ? (a.next_attempt_at && a.next_attempt_at > now ? t("retry_blocked", { when: relatif(a.next_attempt_at, now) }) : "")
    : a.window_wait && a.window_at ? t("retry_window", { when: relatif(a.window_at, now) })
    : a.next_attempt_at && a.next_attempt_at > now
      ? ((a.fail_count || 0) > 1 ? t("retry_at_n", { when: relatif(a.next_attempt_at, now), n: a.fail_count }) : t("retry_at", { when: relatif(a.next_attempt_at, now) }))
    : a.device_id ? t("retry_next_pass") : "";
  const bouton = ["c_session", "c_account_missing", "c_disconnected"].includes(e.cle) && a.effective_account
    ? `<button class="btn btn--petit" data-relogin="${esc(a.effective_account)}">${t("reconnect")}</button>`
    : e.cle === "c_no_account" ? `<button class="btn btn--petit" data-open="dlg-login" data-key="login-p-${a.id}">${t("connect")}</button>`
    : ["c_lost", "c_legacy"].includes(e.cle) ? `<button class="btn btn--petit" data-open="dlg-pair">${t("pair")}</button>`
    : e.cle === "c_certlimit" ? `<a class="btn btn--petit" href="#/settings">${t("settings")}</a>`
    : e.cle === "c_ipa_missing" && a.source_type === "upload" ? `<button class="btn btn--petit" data-replace="${a.id}">${t("replace_ipa")}</button>`
    : ["c_no_tv", "c_source", "c_bundle", "c_digest", "c_gh_missing", "c_no_release"].includes(e.cle) ? `<button class="btn btn--petit" data-edit="${a.id}" data-key="edit-p-${a.id}">${t("settings")}</button>` : "";
  const details = e.brut && (e.brut.length > 110 || e.connu)
    ? `<details class="details" data-key="err-${a.id}"><summary>${t("tech_details")}</summary><pre class="log">${esc(nettoyer(e.brut) || e.brut)}</pre></details>` : "";
  return `<div class="probleme" role="group" aria-label="${esc(t("last_problem"))}">
      <span class="probleme__ic">${ICONS.alerte}</span>
      <div class="probleme__texte">
        <p class="probleme__titre">${esc(e.titre)}</p>
        ${e.conseil ? `<p>${esc(e.conseil)}</p>` : ""}
        ${retry ? `<p class="probleme__retry">${esc(retry)}</p>` : ""}
        ${details}
      </div>
      ${bouton}
    </div>`;
}

// La version téléchargée peut précéder celle qui tourne sur la TV (installation différée ou ratée) :
// « 1.0 installée · 1.1 en attente » tant que les deux diffèrent.
function versionApp(a) {
  const dispo = a.version, installee = a.installed_version;
  if (installee && dispo && installee !== dispo) {
    return `<small title="${esc(t("version_pending_t"))}">${esc(t("version_pending", { from: installee, to: dispo }))}</small>`;
  }
  const v = installee || dispo;
  return v ? `<small>${esc(v)}</small>` : "";
}

function carteApp(a, now, s) {
  const [ton, mot] = etatApp(a, now, s);
  const compte = a.effective_account || a.account_email;
  const essai = a.last_attempt_at ? `${esc(relatif(a.last_attempt_at, now))}${a.last_trigger ? ` (${esc(trigger(a.last_trigger))})` : ""}` : null;
  return `<article class="app app--${ton}" aria-labelledby="app-${a.id}-t">
    <div class="app__tete">
      <span class="row__icon row__icon--${COULEUR[ton]}">${ICONS.app}</span>
      <div class="app__id">
        <h3 class="app__nom" id="app-${a.id}-t">${esc(a.name)}${versionApp(a)}</h3>
        <p class="row__sub">${sourceTexte(a)}</p>
      </div>
      <span class="etat etat--${ton}">${esc(mot)}</span>
    </div>
    ${jauge(a, now, s, ton)}
    ${noteApp(a, now)}
    ${problemeApp(a, now)}
    ${noteMaj(a, now)}
    <dl class="meta">
      <div><dt>Apple TV</dt><dd>${a.device_id ? esc(a.device_name || "Apple TV") : `<span class="texte-err">${t("none_f")}</span>`}</dd></div>
      <div><dt>${t("account")}</dt><dd>${compte ? esc(compte) : t("default_account")}</dd></div>
      ${essai ? `<div><dt>${t("last_attempt")}</dt><dd>${essai}</dd></div>` : a.signed_at ? `<div><dt>${t("signed")}</dt><dd>${esc(relatif(a.signed_at, now))}</dd></div>` : ""}
      ${a.verified_at ? `<div><dt>${t("verified_on_tv")}</dt><dd>${esc(relatif(a.verified_at, now))}</dd></div>` : ""}
      ${a.bad_tag && !a.update_tag ? `<div><dt>${t("skipped_version")}</dt><dd class="mono">${esc(a.source_type === "url" ? redactUrl(a.bad_tag) : a.bad_tag)}</dd></div>` : ""}
    </dl>
    <div class="app__actions">
      <button class="row__action row__action--go" data-renew="${a.id}" ${a.running ? "disabled" : ""}>${a.running ? t("in_progress") : t("renew")}</button>
      <button class="row__action" data-toggle="${a.id}" data-enabled="${a.enabled}">${a.enabled ? t("pause") : t("resume")}</button>
      ${a.can_rollback ? `<button class="row__action" data-rollback="${a.id}" data-nom="${esc(a.name)}" ${a.running ? "disabled" : ""}>${t("previous_version")}</button>` : ""}
      ${a.source_type === "upload" ? `<button class="row__action" data-replace="${a.id}" ${a.running ? "disabled" : ""}>${t("replace_ipa")}</button>` : ""}
      <button class="row__action" data-edit="${a.id}">${t("settings")}</button>
      <button class="row__action row__action--danger" data-del-app="${a.id}" data-nom="${esc(a.name)}">${t("delete")}</button>
    </div>
  </article>`;
}
// déclencheurs enregistrés par le serveur (en français) -> libellé traduit
const trigger = (x) => ({ auto: t("tr_auto"), manuel: t("tr_manual"), ajout: t("tr_added"), "nouvelle version": t("tr_new_version"),
  "nouvelle source": t("tr_new_source"), "nouvelle destination": t("tr_new_dest"), "nouvelle IPA": t("tr_new_ipa"),
  "mise à jour": t("tr_update"), "retour arrière": t("tr_rollback"),
  reconnexion: t("tr_reconnect"), appairage: t("tr_paired"),
  "nouvel appairage": t("tr_repaired"), "app absente": t("tr_missing") }[x] || x);

// ---------- synthèse globale ----------
const boucleArretee = (h, now) => !!h && (h.loop_stalled || (!!h.last_loop_at && now - h.last_loop_at > 3 * (h.check_interval || 600) + 120));
function synthese(s, now) {
  const h = s.health || null;
  const actives = s.apps.filter((a) => a.enabled);
  const deco = s.accounts.filter((a) => a.ok === 0);
  const expirees = actives.filter((a) => a.expires_at && a.expires_at <= now);
  const echecs = actives.filter((a) => a.status === "error" || !a.device_id);
  const lim = 2 * ((h && h.check_interval) || 600) + 300;   // une passe ne fait que lancer les renouvellements (ils tournent à part)
  const enRetard = actives.filter((a) => !a.running && a.status !== "error" && a.expires_at && a.expires_at > now
    && (a.renew_at ?? a.expires_at - s.renew_before_hours * 3600) < now - lim);
  const arret = boucleArretee(h, now);
  const cours = s.apps.filter((a) => a.running);
  const attente = cours.filter((a) => a.phase?.step === "waiting_app");

  if (ECHECS >= 2) return ["off", t("sum_unreachable_t"), t("sum_unreachable")];
  if (arret) return ["off", t("sum_stopped_t"), t("sum_stopped", { since: relatif(h.last_loop_at, now) })];
  if (deco.length) return ["off", t("sum_disconnected_t"), t("sum_disconnected")];
  if (expirees.length) return ["off", tn("sum_expired_t", expirees.length), t("sum_expired")];
  if (echecs.length) return ["warn", tn("sum_failed_t", echecs.length), t("sum_failed")];
  if (enRetard.length) return ["warn", t("sum_late_t"), t("sum_late")];
  if (attente.length) return ["warn", t("st_waiting_app"), t("wait_app_t", { name: attente.map((a) => a.name).join(", ") })];
  if (cours.length) return ["cours", t("st_renewing"), t("sum_running", { names: cours.map((a) => a.name).join(", ") })];
  if (!s.apps.length) return ["neutre", t("sum_empty_t"), t("sum_empty")];
  const prochaine = actives.filter((a) => a.expires_at).sort((x, y) => x.expires_at - y.expires_at)[0];
  // aucune app active avec un profil valide (toutes en pause, ou pas encore installées) : pas de « à jour » vert
  if (!prochaine) return ["neutre", t("sum_idle_t"), t("sum_idle")];
  const quand = Math.max(now, prochaine.renew_at ?? prochaine.expires_at - s.renew_before_hours * 3600);
  return ["ok", t("sum_ok_t"), t("sum_next", { when: quand <= now + 60 ? t("now") : relatif(quand, now) })];
}

function renderEntete(now) {
  const point = $("#etat-global");
  if (!STATE) {
    if (ECHECS >= 1) { point.className = "point point--off"; point.setAttribute("aria-label", t("sum_unreachable_t")); $("#resume").textContent = t("sum_unreachable_t") + "."; }
    return;
  }
  const [ton, titre, texte] = synthese(STATE, now);
  point.className = "point point--" + ton;
  point.setAttribute("aria-label", titre);
  setHTML($("#resume"), `<strong>${esc(titre)}.</strong> ${esc(texte)}`);
  if (VUE === "main") document.title = ton === "ok" || ton === "neutre" ? "tvsign" : `${ton === "cours" ? t("title_renewing") : t("title_check")} · tvsign`;
}

function renderAlertes() {
  const s = STATE, html = [];
  if (FLASH && FLASH.jusqua > Date.now()) {
    const cls = FLASH.ton === "ok" ? "banner--ok" : FLASH.ton === "info" ? "banner--info" : "";
    html.push(`<div class="banner ${cls}"><p><strong>${esc(FLASH.titre)}</strong>${esc(FLASH.texte)}</p></div>`);
  }
  if (ECHECS >= 2 && DEPUIS) {
    html.push(`<div class="banner"><p><strong>${esc(t("unreachable_since", { time: fmtHeure(DEPUIS / 1000) }))}</strong>${esc(t("unreachable_since_txt"))}</p></div>`);
  }
  if (s) {
    if (s.auth_disabled) {
      html.push(`<div class="banner banner--info"><p><strong>${t("auth_off_t")}</strong>${t("auth_off")}</p></div>`);
    }
    for (const n of (s.notices || []).filter((x) => x.id === "revocation")) {
      html.push(`<div class="banner"><p><strong>${esc(t("revoked_t", { email: n.email }))}</strong>${esc(t("revoked", { app: n.app }))}</p>
        <button class="btn" type="button" data-dismiss-notice="revocation">${t("dismiss")}</button></div>`);
    }
    for (const a of s.accounts.filter((x) => x.ok === 0)) {
      html.push(`<div class="banner"><p><strong>${esc(t("acc_disconnected_t", { email: a.email }))}</strong>${t("acc_disconnected")}</p>
        <button class="btn btn--primary" data-relogin="${esc(a.email)}">${t("reconnect")}</button></div>`);
    }
    if (!s.accounts.length) {
      html.push(`<div class="banner banner--info"><p><strong>${t("no_account_t")}</strong>${t("no_account")}</p>
        <button class="btn btn--primary" data-open="dlg-login">${t("connect")}</button></div>`);
    }
    if (s.accounts.length && !s.devices.length) {
      html.push(`<div class="banner banner--info"><p><strong>${t("no_tv_t")}</strong>${t("no_tv")}</p>
        <button class="btn btn--primary" data-open="dlg-pair">${t("pair")}</button></div>`);
    }
    const legacy = s.devices.filter((x) => x.legacy_pairing);
    if (legacy.length) {
      html.push(`<div class="banner banner--info"><p><strong>${esc(tn("legacy_t", legacy.length, { names: legacy.map((d) => d.name).join(", ") }))}</strong>${t("legacy")}</p>
        <button class="btn btn--primary" data-open="dlg-pair">${t("pair")}</button></div>`);
    }
    if (s.update) {
      const u = s.update;
      html.push(`<div class="banner banner--info"><p><strong>${esc(t("self_update_t", { v: u.version }))}</strong>${esc(u.edition === "windows" ? t("self_update_windows") : t("self_update_docker", { major: String(u.version).split(".")[0] }))}</p>
        ${u.url ? `<a class="btn" href="${esc(u.url)}" target="_blank" rel="noopener noreferrer">${t("release_page")}</a>` : ""}
        <button class="btn" data-dismiss-update="1">${t("dismiss")}</button></div>`);
    }
    if (s.health?.release_check_error) {
      const e = enClair(s.health.release_check_error);
      html.push(`<div class="banner banner--info"><p><strong>${esc(t("release_check_t"))}</strong>${esc(e.titre)}${e.conseil ? ". " + esc(e.conseil) : ""}</p></div>`);
    }
    for (const d of s.devices.filter((x) => x.pairing_lost && !x.legacy_pairing)) {
      html.push(`<div class="banner"><p><strong>${esc(t("lost_t", { name: d.name }))}</strong>${t("lost")}</p>
        <button class="btn btn--primary" data-open="dlg-pair">${t("pair")}</button></div>`);
    }
  }
  setHTML($("#alertes"), html.join(""));
}

function renderApps(s, now) {
  const h = s.health;
  const retry = h?.retry_minutes ?? 45;
  setHTML($("#apps"), s.apps.length
    ? `<div class="apps">${s.apps.map((a) => carteApp(a, now, s)).join("")}</div>
       <p class="group__footer">${esc(t("apps_footer", { h: Math.round(s.renew_before_hours), m: retry }))}</p>`
    : `<div class="vide"><p>${t("apps_empty")}</p><button class="btn btn--primary" data-open="dlg-app">${t("add_app")}</button></div>`);
}

function etatTV(d) {
  const errRecente = d.last_error && (!d.last_seen || (d.last_error_at || 0) > d.last_seen);
  if (d.legacy_pairing) return ["warn", t("tv_repair_update"), errRecente];
  if (d.pairing_lost) return ["err", t("tv_lost"), errRecente];
  if (errRecente) return ["warn", t("tv_unreachable"), errRecente];
  return d.last_seen ? ["ok", t("tv_reachable"), errRecente] : ["neutre", t("tv_never"), errRecente];
}

function renderDevices(s, now) {
  setHTML($("#devices"), s.devices.length ? `<div class="list">${s.devices.map((d) => {
    const [ton, mot, errRecente] = etatTV(d);
    const apps = s.apps.filter((a) => a.device_id === d.id).map((a) => a.name);
    return `<div class="row">
      <span class="row__icon row__icon--${COULEUR[ton]}">${ICONS.tv}</span>
      <div class="row__label">
        <p class="row__title">${esc(d.name)}</p>
        <p class="row__sub"><span class="etat etat--${ton}">${mot}</span> · ${esc(d.model || t("unknown_model"))} · tvOS ${esc(d.os_version || "?")}${d.last_ip ? ` · ${esc(d.last_ip)}` : ""}${d.host_hint ? ` · ${esc(t("manual_addr", { host: d.host_hint }))}` : ""}${d.busy ? ` · ${esc(t("tv_busy"))}` : ""}</p>
        <p class="row__sub">${esc(t("last_contact", { when: relatif(d.last_seen, now) }))}${apps.length ? ` · ${esc(apps.join(", "))}` : ""}</p>
        ${d.legacy_pairing ? `<p class="row__sub">${t("legacy_row")}</p>`
          : errRecente ? `<p class="row__sub texte-err">${esc(enClair(d.last_error).titre)} ${d.last_error_at ? `(${esc(relatif(d.last_error_at, now))})` : ""}</p>` : ""}
      </div>
      <div class="row__buttons">
        ${d.pairing_lost || d.legacy_pairing ? `<button class="row__action row__action--go" data-open="dlg-pair">${t("repair")}</button>` : ""}
        ${d.legacy_pairing ? "" : `<button class="row__action" data-refresh-dev="${d.id}">${t("test_connection")}</button>`}
        ${d.legacy_pairing ? "" : `<button class="row__action" data-addr-dev="${d.id}">${t("address")}</button>`}
        <button class="row__action row__action--danger" data-del-dev="${d.id}" data-nom="${esc(d.name)}">${t("forget")}</button>
      </div>
    </div>`;
  }).join("")}</div>`
    : `<div class="vide"><p>${t("tv_empty")}</p><button class="btn" data-open="dlg-pair">${t("pair_title")}</button></div>`);
}

function renderAccounts(s, now) {
  const heures = s.health?.account_check_hours;
  setHTML($("#accounts"), s.accounts.length ? `<div class="list">${s.accounts.map((a) => {
    const etat = a.ok === 1 ? `<span class="etat etat--ok">${t("acc_valid")}</span>`
      : a.ok === 0 ? `<span class="etat etat--err">${t("acc_off")}</span>`
      : a.checked_at ? `<span class="etat etat--neutre">${t("acc_unknown")}</span>` : `<span class="etat etat--neutre">${t("acc_unchecked")}</span>`;
    const note = a.ok == null && a.checked_at && a.message ? `<p class="row__sub">${esc(t("acc_retry_later", { why: enClair(a.message).titre }))}</p>`
      : a.ok === 0 ? `<p class="row__sub texte-err">${esc(t("acc_reauth_reason"))}</p>` : "";
    const places = (a.slots || []).map((x) => `<p class="row__sub ${x.apps > x.max ? "texte-err" : ""}">${esc(t("slots_line", { apps: x.apps, max: x.max, tv: x.device_name || "Apple TV" }))}${x.apps >= x.max ? " · " + esc(t("slots_note")) : ""}</p>`).join("");
    return `<div class="row">
      <span class="row__icon row__icon--${a.ok === 0 ? "red" : a.ok === 1 ? "blue" : "gray"}">${ICONS.user}</span>
      <div class="row__label">
        <p class="row__title">${esc(a.email)}${a.selected ? `<small>${t("default_account")}</small>` : ""}</p>
        <p class="row__sub">${etat}${a.team_id ? ` · ${t("team")} <span class="mono">${esc(a.team_id)}</span>` : ""}${a.cert_expires ? ` · ${esc(t("cert_until", { date: a.cert_expires }))}` : ""} · ${esc(t("checked_when", { when: relatif(a.checked_at, now) }))}</p>
        ${note}${places}
      </div>
      <div class="row__buttons">
        <button class="row__action" data-check="${esc(a.email)}">${t("check")}</button>
        <button class="row__action" data-team="${esc(a.email)}">${t("team_button")}</button>
        ${a.selected ? "" : `<button class="row__action" data-select="${esc(a.email)}">${t("use_default")}</button>`}
        <button class="row__action ${a.ok === 0 ? "row__action--go" : ""}" data-relogin="${esc(a.email)}">${t("reconnect")}</button>
        <button class="row__action row__action--danger" data-logout="${esc(a.email)}">${t("remove")}</button>
      </div>
    </div>`;
  }).join("")}</div>
    <p class="group__footer">${esc(heures ? t("acc_footer_h", { h: Math.round(heures) }) : t("acc_footer"))}</p>`
    : `<div class="vide"><p>${t("acc_empty")}</p><button class="btn" data-open="dlg-login">${t("add_account")}</button></div>`);
}

function renderSysteme(s, now) {
  const h = s.health;
  $("#systeme-bloc").hidden = !h;
  if (!h) return;
  const arret = boucleArretee(h, now);
  const boucle = arret ? ["err", t("loop_stopped")] : h.loop_error ? ["warn", t("loop_error")] : ["ok", t("loop_active")];
  const notif = !h.notifications ? ["warn", t("notif_none")] : h.notify_error ? ["warn", t("notif_failing")] : ["ok", t("notif_on")];
  const prochaine = arret ? "" : h.next_check_at > now ? t("next_check", { when: relatif(h.next_check_at, now) }) : h.last_loop_at ? t("check_running") : "";
  setHTML($("#systeme"), `<div class="list">
    <div class="row">
      <span class="row__icon row__icon--${COULEUR[boucle[0]]}">${ICONS.boucle}</span>
      <div class="row__label">
        <p class="row__title">${t("auto_renewal")}</p>
        <p class="row__sub"><span class="etat etat--${boucle[0]}">${boucle[1]}</span> · ${esc(t("checked_when", { when: relatif(h.last_loop_at, now) }))}${esc(prochaine)}${h.restarts ? ` · ${esc(t("loop_restarted", { n: h.restarts }))}` : ""}</p>
        ${h.loop_error ? `<p class="row__sub texte-err">${esc(enClair(h.loop_error).titre)}</p>` : ""}
        ${arret ? `<p class="row__sub">${t("restart_hint")}</p>` : ""}
      </div>
    </div>
    <a class="row row--lien" href="#/settings">
      <span class="row__icon row__icon--${COULEUR[notif[0]]}">${ICONS.cloche}</span>
      <div class="row__label">
        <p class="row__title">${t("notifications")}</p>
        <p class="row__sub"><span class="etat etat--${notif[0]}">${notif[1]}</span>${h.notifications ? "" : " · " + esc(t("notif_none_txt"))}</p>
        ${h.notify_error ? `<p class="row__sub texte-err">${esc(nettoyer(h.notify_error))}</p>` : ""}
      </div>
      <span class="chevron" aria-hidden="true">›</span>
    </a>
  </div>`);
}

let OLDER_EVENTS = [];      // événements plus anciens chargés par « Afficher plus » (extras.js)
let OLDER_MORE = null;      // null : inconnu (on suppose qu'il y en a si le serveur en a renvoyé 80)
function renderEvents(s) {
  const filtre = store.get("tvsign.journal") || "tout";
  const radio = $(`input[name="journal-filtre"][value="${filtre}"]`);
  if (radio && !radio.checked) radio.checked = true;
  const tous = s.events.concat(OLDER_EVENTS.filter((o) => o.id < (s.events[s.events.length - 1]?.id ?? Infinity)));
  const evs = tous.filter((e) => filtre === "tout" || e.level !== "info");
  if (!evs.length) return setHTML($("#events"), `<div class="vide"><p>${filtre === "tout" ? t("log_empty") : t("log_no_problem")}</p></div>`);
  const jour = (ts) => {
    const d = new Date(ts * 1000), auj = new Date(), hier = new Date(Date.now() - 86400000);
    if (d.toDateString() === auj.toDateString()) return t("today");
    if (d.toDateString() === hier.toDateString()) return t("yesterday");
    return d.toLocaleDateString(LOCALE(), { weekday: "long", day: "numeric", month: "long" });
  };
  let html = "", courant = null;
  for (const e of evs) {
    const j = jour(e.ts);
    if (j !== courant) { if (courant) html += "</ol>"; html += `<h3 class="journal__jour">${esc(j)}</h3><ol class="list journal">`; courant = j; }
    const pb = e.level !== "info";
    const lisible = pb ? enClair(e.message) : null;
    const prefixe = (e.message.match(/^([^:]{1,40}) : /) || [])[1];
    const titre = pb && lisible.connu ? `${prefixe ? prefixe + " : " : ""}${lisible.titre}` : nettoyer(e.message).slice(0, 180);
    const long = pb && (lisible.connu || e.message.length > 180);
    html += `<li class="row journal__ligne journal__ligne--${esc(e.level)}">
      <time datetime="${new Date(e.ts * 1000).toISOString()}">${fmtHeure(e.ts)}</time>
      <span class="sr">${e.level === "error" ? t("error") : e.level === "warn" ? t("warning") : ""}</span>
      <div class="journal__texte"><p>${esc(titre)}</p>
        ${e.repeat > 1 ? `<p class="journal__rep">${esc(t("journal_repeat", { n: e.repeat, when: fmtHeure(e.first_ts || e.ts) }))}</p>` : ""}
        ${long ? `<details class="details" data-key="ev-${e.id}"><summary>${t("details")}</summary><pre class="log">${esc(e.message)}</pre></details>` : ""}</div>
    </li>`;
  }
  const plus = OLDER_MORE === null ? s.events.length >= 80 : OLDER_MORE;
  setHTML($("#events"), html + "</ol>" + (plus ? `<div class="actions-form"><button class="btn" type="button" data-more-events>${t("log_more")}</button></div>` : ""));
}

function render() {
  const now = maintenant();
  const essayer = (f, ...args) => { try { f(...args); } catch (e) { console.error(f.name, e); } };
  if (VUE === "onboarding") return essayer(renderWizard);
  if (VUE !== "main") return;
  essayer(renderEntete, now);
  essayer(renderAlertes);
  if (!STATE) return;
  const s = STATE;
  essayer(renderApps, s, now);
  essayer(renderDevices, s, now);
  essayer(renderAccounts, s, now);
  essayer(renderSysteme, s, now);
  essayer(renderEvents, s);
}

// ---------- rafraîchissement : jamais deux requêtes à la fois ----------
let SEQ = 0, EN_COURS = null, MINUTERIE = null;
async function refresh() {
  if (VUE === "auth") return;
  const n = ++SEQ;
  EN_COURS?.abort();
  const ctl = (EN_COURS = new AbortController());
  const tm = setTimeout(() => ctl.abort(), 10000);
  try {
    const s = await api("/api/state", { signal: ctl.signal });
    if (n !== SEQ) return;
    if (typeof s.now === "number") DECALAGE = s.now - Date.now() / 1000;
    STATE = s; ECHECS = 0; DEPUIS = null;
  } catch (e) {
    if (n !== SEQ || e.status === 401) return;
    if (++ECHECS === 1) DEPUIS = Date.now();
  } finally {
    clearTimeout(tm);
    if (n === SEQ) render();
  }
}
function planifier(ms = 5000) {
  clearTimeout(MINUTERIE);
  if (document.hidden || VUE === "auth") return;   // en arrière-plan : pas de polling
  MINUTERIE = setTimeout(async () => { try { if (VUE === "main" || VUE === "onboarding") await refresh(); } finally { planifier(); } }, ms);
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { refresh().finally(() => planifier()); } else clearTimeout(MINUTERIE);
});

// ---------- actions ----------
document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("button");
  if (!b || b.closest("dialog") || !b.closest("#vue-main, #vue-onboarding")) return;
  const d = b.dataset;
  if (d.open) return openDialog(d.open);
  if (d.relogin) {
    openDialog("dlg-login");
    $("#form-login").email.value = d.relogin;
    $("#form-login").password.focus();
    return;
  }
  if (d.edit) return openEdit(+d.edit);
  if (d.addrDev) return openAddr(+d.addrDev);
  if (d.team) return openTeam(d.team);
  if (d.replace) {
    // le sélecteur de fichier est un <input> permanent (hors des sections redessinées) : un rendu de
    // la carte pendant le choix du fichier ne perd donc pas l'envoi
    REMPLACE_ID = +d.replace;
    $("#replace-input").click();
    return;
  }
  if (!(d.renew || d.toggle || d.delApp || d.refreshDev || d.delDev || d.check || d.select || d.logout
      || d.update || d.rollback || d.dismissUpdate)) return;
  try {
    if (d.dismissUpdate) {
      await api("/api/update/dismiss", { method: "POST" });
    }
    if (d.update) {
      await busy(b, "data-update", t("starting"), async () => {
        const r = await api(`/api/apps/${d.update}/update`, { method: "POST" });
        if (r.started === false) flash("info", t("already_running_t"), " " + t("already_running"));
        else flash("info", t("update_started_t"), " " + t("update_started"));
        await refresh();
      });
    }
    if (d.rollback && confirm(t("confirm_rollback", { name: d.nom }))) {
      await api(`/api/apps/${d.rollback}/rollback`, { method: "POST" });
      flash("info", t("rollback_started_t"), " " + t("renew_started"));
    }
    if (d.renew) {
      await busy(b, "data-renew", t("starting"), async () => {
        const r = await api(`/api/apps/${d.renew}/renew`, { method: "POST" });
        if (r.started === false) flash("info", t("already_running_t"), " " + t("already_running"));
        else flash("info", t("renew_started_t"), " " + t("renew_started"));
        await refresh();
      });
    }
    if (d.toggle) {
      const reprise = d.enabled !== "1";
      await api(`/api/apps/${d.toggle}`, { method: "PATCH", json: { enabled: reprise } });
      flash("info", reprise ? t("resumed_t") : t("paused_t"), reprise ? "" : " " + t("paused"));
    }
    if (d.delApp && confirm(t("confirm_del_app", { name: d.nom }))) {
      await api(`/api/apps/${d.delApp}`, { method: "DELETE" });
    }
    if (d.refreshDev) {
      await busy(b, "data-refresh-dev", t("connecting"), async () => {
        await api(`/api/devices/${d.refreshDev}/refresh`, { method: "POST" });
        flash("ok", t("tv_ok_t"), " " + t("tv_ok"));
      });
    }
    if (d.delDev) {
      const deps = (STATE?.apps || []).filter((a) => String(a.device_id) === d.delDev).map((a) => a.name);
      const msg = t("confirm_del_tv", { name: d.nom }) + (deps.length ? "\n\n" + t("confirm_del_tv_apps", { apps: deps.join(", ") }) : "");
      if (confirm(msg)) {
        try { await api(`/api/devices/${d.delDev}`, { method: "DELETE" }); }
        catch (e) {
          // l'appairage n'a pas pu être effacé (plumesign absent ou bloqué…) : on propose de retirer la TV quand même
          if (e.status !== 502 || !confirm(t("confirm_force_del_tv", { why: enClair(e.message).titre }))) throw e;
          await api(`/api/devices/${d.delDev}?force=1`, { method: "DELETE" });
        }
      }
    }
    if (d.check) {
      await busy(b, "data-check", t("checking"), async () => {
        const r = await api(`/api/accounts/${encodeURIComponent(d.check)}/check`, { method: "POST" });
        if (r.ok === true) flash("ok", t("acc_ok_t"));
        else if (r.ok === false) flash("err", t("c_session_t"), " " + t("c_session_a"));
        else flash("info", t("acc_unknown_t"), " " + t("acc_unknown_txt"));
      });
    }
    if (d.select) await api(`/api/accounts/${encodeURIComponent(d.select)}/select`, { method: "POST" });
    if (d.logout && confirm(t("confirm_logout", { email: d.logout }))) {
      await api(`/api/accounts/${encodeURIComponent(d.logout)}`, { method: "DELETE" });
    }
  } catch (e) {
    const c = enClair(e.message);
    flash("err", t("action_failed"), ` ${c.titre}${c.conseil ? ". " + c.conseil : ""}`);
  }
  refresh();
});

// Remplacer l'IPA d'une app importée : depuis sa carte, la carte d'erreur « IPA absente » ou ses réglages.
async function remplacerIPA(id, input) {
  const fichier = input.files[0];
  if (!fichier) return false;
  const fd = new FormData();
  fd.append("file", fichier);
  flash("info", t("replacing"));
  try {
    await api(`/api/apps/${id}/ipa`, { method: "POST", body: fd });
    flash("ok", t("ipa_replaced_t"), " " + t("ipa_replaced"));
    return true;
  } catch (e) {
    flash("err", t("action_failed"), " " + e.message);
    return false;
  } finally {
    input.value = "";
    refresh();
  }
}
let REMPLACE_ID = null;
$("#replace-input").addEventListener("change", (e) => {
  const input = e.target, id = REMPLACE_ID;
  REMPLACE_ID = null;
  if (id != null) remplacerIPA(id, input); else input.value = "";
});

$("#journal-bloc").addEventListener("change", (e) => {
  if (e.target.name === "journal-filtre") { store.set("tvsign.journal", e.target.value); if (STATE) renderEvents(STATE); }
});
$("#journal-bloc").addEventListener("toggle", (e) => store.set("tvsign.journal.ouvert", e.target.open ? "1" : "0"));
if (store.get("tvsign.journal.ouvert") === "1") $("#journal-bloc").open = true;

function openDialog(id) {
  if (!STATE && id !== "dlg-login") return flash("err", t("sum_unreachable_t"), " " + t("retry_moment"));
  if (id === "dlg-app") fillAppForm();
  if (id === "dlg-pair") { $("#pair-manuel").open = false; $("#pair-host").value = ""; scan(); }   // une adresse saisie avant ne doit pas masquer la recherche automatique
  if (id === "dlg-login") {
    $("#login-session").innerHTML = ""; $("#login-error").textContent = "";
    $("#login-submit").disabled = false;
    // l'avertissement sur les certificats se confirme une fois, au premier compte
    $("#login-ack-row").hidden = !!(STATE?.accounts || []).length;
    $("#form-login").ack.checked = false;
  }
  document.getElementById(id).showModal();
}

// ---------- équipe de développeur d'un compte ----------
let TEAM_EMAIL = null;
async function openTeam(email) {
  TEAM_EMAIL = email;
  const box = $("#team-list"), err = $("#team-error"), ok = $("#team-submit");
  err.textContent = "";
  ok.disabled = true;
  box.innerHTML = `<p class="statut"><span class="etat etat--cours">${t("team_loading")}</span></p>`;
  $("#dlg-team").showModal();
  try {
    const r = await api(`/api/accounts/${encodeURIComponent(email)}/teams`);
    if (TEAM_EMAIL !== email) return;
    if (!r.teams.length) { box.innerHTML = `<div class="vide"><p>${t("team_none")}</p></div>`; return; }
    // PLUMESIGN_TEAM_ID (environnement du conteneur) l'emporte sur le choix mémorisé : on le dit, on ne laisse pas choisir.
    const verrou = !!r.env_team;
    box.innerHTML = (verrou ? `<p class="group__footer">${esc(t("team_env_locked", { name: "PLUMESIGN_TEAM_ID" }))}</p>` : "") +
      `<div class="list">${r.teams.map((x) => `
      <label class="row row--choix">
        <input type="radio" name="team" value="${esc(x.team_id)}" ${x.selected ? "checked" : ""} ${verrou ? "disabled" : ""}>
        <span class="row__icon row__icon--blue">${ICONS.user}</span>
        <span class="row__label"><span class="row__title">${esc(x.name)}</span>
          <span class="row__sub mono">${esc(x.team_id)}${x.type ? " · " + esc(x.type) : ""}</span></span>
      </label>`).join("")}</div>`;
    ok.disabled = verrou;
  } catch (e) {
    box.innerHTML = "";
    const c = enClair(e.message);
    err.textContent = c.connu ? `${c.titre}. ${c.conseil}` : c.titre;
  }
}
$("#form-team").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value !== "ok") return;
  ev.preventDefault();
  const choix = $('input[name="team"]:checked', ev.target), err = $("#team-error"), btn = $("#team-submit");
  if (!choix || !TEAM_EMAIL) return;
  btn.disabled = true;
  try {
    await api(`/api/accounts/${encodeURIComponent(TEAM_EMAIL)}/team`, { json: { team_id: choix.value } });
    ev.target.closest("dialog").close();
    flash("ok", t("team_saved_t"), " " + t("team_saved"));
  } catch (e) {
    const c = enClair(e.message);
    err.textContent = c.connu ? `${c.titre}. ${c.conseil}` : c.titre;
  } finally {
    btn.disabled = false;
    refresh();
  }
});

// ---------- ajout d'app ----------
let PRESETS = null;
async function presets() {
  if (!PRESETS) { try { PRESETS = await api("/api/presets"); } catch { PRESETS = []; } }
  return PRESETS;
}
const presetDesc = (p) => (p.description && (p.description[LANG] || p.description.en)) || p.repo;
function presetCartes(list, nom) {
  return list.length ? `<div class="list">${list.map((p, i) => `
    <label class="row row--choix">
      <input type="radio" name="${nom}" value="${esc(p.id)}" ${i === 0 ? "checked" : ""}>
      <span class="row__icon row__icon--blue">${ICONS.app}</span>
      <span class="row__label"><span class="row__title">${esc(p.name)}</span><span class="row__sub">${esc(presetDesc(p))}</span>
        <span class="row__sub mono">${esc(p.repo)}</span></span>
    </label>`).join("")}</div>` : `<div class="vide"><p>${t("no_presets")}</p></div>`;
}
const optionsTV = (choix) => STATE.devices.map((d) => `<option value="${d.id}" ${d.id === choix ? "selected" : ""}>${esc(d.name)}</option>`).join("");
async function fillAppForm() {
  const f = $("#form-app");
  f.device_id.innerHTML = optionsTV(STATE.devices[0]?.id) || `<option value="">${t("no_tv_paired")}</option>`;
  f.device_id.disabled = !STATE.devices.length;
  f.account_email.innerHTML = `<option value="">${t("default_account")}</option>` + STATE.accounts.map((a) => `<option>${esc(a.email)}</option>`).join("");
  $("#app-error").textContent = STATE.devices.length ? "" : t("pair_first");
  $("#app-submit").disabled = !STATE.devices.length;
  syncTabs();
  const list = await presets();
  $("#app-presets").innerHTML = presetCartes(list, "preset");
  canalDuPreset("preset", "channel");
  if (!list.length && f.source_type.value === "preset") { f.source_type.value = "github"; syncTabs(); }
}
// canal proposé par défaut : celui du préréglage choisi (ex. bêtas tant qu'il n'y a pas de version stable)
function canalDuPreset(nomPreset, nomCanal) {
  const choix = $(`input[name="${nomPreset}"]:checked`);
  const p = (PRESETS || []).find((x) => x.id === choix?.value);
  const r = p && $(`input[name="${nomCanal}"][value="${p.default_channel === "beta" ? "beta" : "stable"}"]`);
  if (r) r.checked = true;
}
document.addEventListener("change", (e) => {
  if (e.target.name === "preset") canalDuPreset("preset", "channel");
  if (e.target.name === "wiz-preset") canalDuPreset("wiz-preset", "wiz-canal");
});
function syncTabs() {
  const type = $("#form-app").source_type.value;
  $$("#form-app [data-for]").forEach((el) => (el.hidden = !el.dataset.for.split(" ").includes(type)));
}
$("#form-app").addEventListener("change", (e) => { if (e.target.name === "source_type") syncTabs(); });
$("#form-app").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value !== "ok") return;
  ev.preventDefault();
  const f = ev.target, type = f.source_type.value, fd = new FormData(), err = $("#app-error");
  fd.append("source_type", type);
  if (type === "preset") {
    const p = f.querySelector('input[name="preset"]:checked');
    if (!p) return (err.textContent = t("choose_preset"));
    fd.append("preset", p.value); fd.append("channel", f.channel.value);
  }
  if (type === "github") {
    const depot = depotGithub(f.source.value);
    if (!depot) return (err.textContent = t("repo_format"));
    fd.append("source", depot); fd.append("source_filter", f.source_filter.value.trim());
    fd.append("channel", f.channel.value);
  }
  if (type === "url") {
    if (!/^https:\/\//.test(f.source_url.value.trim())) return (err.textContent = t("https_only"));
    fd.append("source", f.source_url.value.trim());
  }
  if (type === "upload") {
    if (!f.file.files[0]) return (err.textContent = t("choose_ipa"));
    fd.append("file", f.file.files[0]);
  }
  if (!f.device_id.value) return (err.textContent = t("choose_tv"));
  fd.append("device_id", f.device_id.value);
  if (f.account_email.value) fd.append("account_email", f.account_email.value);
  const btn = $("#app-submit");
  btn.disabled = true; btn.textContent = type === "upload" ? t("uploading") : t("downloading");
  err.textContent = "";
  try {
    const app = await api("/api/apps", { method: "POST", body: fd });
    f.closest("dialog").close();
    f.reset();
    flash("ok", t("app_added_t", { name: app.name }), " " + t("app_added") + ((app.warnings || []).includes("slots_full") ? t("warn_slots_full", { max: STATE?.max_active_apps || 3 }) : ""));
  } catch (e) {
    err.textContent = messageErreur(e);
  } finally {
    btn.disabled = false; btn.textContent = t("add");
    refresh();
  }
});

// ---------- réglages d'une app ----------
let EDIT = null;
function openEdit(id) {
  const a = STATE?.apps.find((x) => x.id === id);
  if (!a) return;
  EDIT = a;
  const f = $("#form-edit");
  $("#t-edit").textContent = a.name;
  f.device_id.innerHTML = (a.device_id ? "" : `<option value="" selected>${t("choose")}</option>`) + optionsTV(a.device_id);
  const compte = a.account_email || a.effective_account || "";
  const comptes = STATE.accounts.map((c) => c.email);
  f.account_email.innerHTML = (comptes.includes(compte) || !compte ? "" : `<option value="${esc(compte)}" selected>${esc(compte)} (${t("removed")})</option>`)
    + comptes.map((e) => `<option ${e === compte ? "selected" : ""}>${esc(e)}</option>`).join("");
  f.source.value = a.source_type === "github" ? a.source || "" : "";
  f.source_filter.value = a.source_filter || "";
  f.channel.value = a.channel === "beta" ? "beta" : "stable";
  f.source_url.value = a.source_type === "url" ? a.source || "" : "";
  f.auto_update.value = a.auto_update === 0 ? "0" : "1";
  f.pinned_tag.value = a.pinned_tag || "";
  $$("#form-edit [data-for]").forEach((el) => (el.hidden = !el.dataset.for.split(" ").includes(a.source_type)));
  $("#edit-error").textContent = "";
  // la même app sur une autre TV : les TV qui ne l'ont pas encore (même compte)
  const compteApp = a.account_email || a.effective_account || "";
  const candidates = STATE.devices.filter((d) => !d.legacy_pairing && d.id !== a.device_id
    && !STATE.apps.some((x) => x.id !== a.id && a.bundle_id && x.bundle_id === a.bundle_id
      && (x.account_email || x.effective_account || "") === compteApp && x.device_id === d.id));
  $("#edit-copy-bloc").hidden = !candidates.length || !a.device_id;
  $("#edit-copy-tv").innerHTML = candidates.map((d) => `<option value="${d.id}">${esc(d.name)}</option>`).join("");
  $("#dlg-edit").showModal();
}
$("#edit-copy").onclick = async () => {
  const a = EDIT, sel = $("#edit-copy-tv"), err = $("#edit-error"), btn = $("#edit-copy");
  if (!a || !sel.value) return;
  btn.disabled = true; err.textContent = "";
  try {
    const tv = sel.selectedOptions[0].text;
    await api(`/api/apps/${a.id}/copy`, { json: { device_id: +sel.value } });
    $("#dlg-edit").close();
    flash("ok", t("copied_tv_t"), " " + t("copied_tv", { tv }));
  } catch (e) {
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
    refresh();
  }
};

// ---------- adresse d'une Apple TV (multicast bloqué, adresse changée) ----------
let ADDR = null;
function openAddr(id) {
  const d = STATE?.devices.find((x) => x.id === id);
  if (!d) return;
  ADDR = d;
  const f = $("#form-addr");
  $("#t-addr").textContent = t("tv_address_for", { name: d.name });
  f.host.value = d.host_hint || "";
  f.host.placeholder = d.last_ip || "192.168.1.20";
  $("#addr-error").textContent = ""; $("#addr-ok").innerHTML = "";
  $("#dlg-addr").showModal();
}
$("#form-addr").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value !== "ok") return;
  ev.preventDefault();
  const f = ev.target, err = $("#addr-error"), ok = $("#addr-ok"), btn = $("#addr-submit");
  btn.disabled = true; err.textContent = "";
  ok.innerHTML = `<span class="etat etat--cours">${t("connecting")}</span>`;
  try {
    const r = await api(`/api/devices/${ADDR.id}/relocate`, { json: { host: f.host.value.trim() } });
    ok.innerHTML = "";
    f.closest("dialog").close();
    flash("ok", t("tv_ok_t"), " " + t("tv_found_at", { ip: r.ip || "" }));
  } catch (e) {
    ok.innerHTML = "";
    // l'adresse est enregistrée même si la TV ne répond pas (en veille) : on le dit
    err.textContent = e.status === 502 ? t("addr_saved_unreachable", { why: enClair(e.message).titre })
      : e.status === 409 ? t("addr_saved_busy") : e.message;
  } finally {
    btn.disabled = false;
    refresh();
  }
});
$("#form-edit").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value !== "ok") return;
  ev.preventDefault();
  const f = ev.target, a = EDIT, body = {}, err = $("#edit-error");
  if (f.device_id.value && +f.device_id.value !== a.device_id) body.device_id = +f.device_id.value;
  const compte = a.account_email || a.effective_account || "";
  if (f.account_email.value && f.account_email.value !== compte) body.account_email = f.account_email.value;
  if (a.source_type === "github") {
    const src = depotGithub(f.source.value);
    if (!src) return (err.textContent = t("repo_format"));
    if (src !== a.source) body.source = src;
    if (f.source_filter.value.trim() !== (a.source_filter || "")) body.source_filter = f.source_filter.value.trim();
    if (f.channel.value !== (a.channel === "beta" ? "beta" : "stable")) body.channel = f.channel.value;
  }
  if (a.source_type === "url") {
    const src = f.source_url.value.trim();
    if (!/^https:\/\//.test(src)) return (err.textContent = t("https_only"));
    if (src !== a.source) body.source = src;
  }
  if (a.source_type !== "upload" && (f.auto_update.value === "1") !== (a.auto_update !== 0)) body.auto_update = f.auto_update.value === "1";
  if (a.source_type === "github" && f.pinned_tag.value.trim() !== (a.pinned_tag || "")) body.pinned_tag = f.pinned_tag.value.trim();
  if (!Object.keys(body).length) return f.closest("dialog").close();
  if (body.account_email && !confirm(t("confirm_account_change", { name: a.name, email: body.account_email }))) return;
  const btn = $("#edit-submit");
  btn.disabled = true;
  try {
    await api(`/api/apps/${a.id}`, { method: "PATCH", json: body });
    f.closest("dialog").close();
    const relance = "device_id" in body || "account_email" in body;
    let lance = null;
    if (relance && a.enabled) lance = await api(`/api/apps/${a.id}/renew`, { method: "POST" });   // nouvelle destination : installer tout de suite
    flash("ok", t("saved_t"), !lance ? "" : lance.started === false ? " " + t("dest_after") : " " + t("dest_now"));
  } catch (e) {
    err.textContent = messageErreur(e);
  } finally {
    btn.disabled = false;
    refresh();
  }
});

$("#pin-current").addEventListener("click", () => { if (EDIT?.source_tag) $("#form-edit").pinned_tag.value = EDIT.source_tag; });
$("#edit-replace").addEventListener("change", async (e) => {
  if (EDIT && await remplacerIPA(EDIT.id, e.target)) $("#dlg-edit").close();
});

// ---------- sessions interactives (PIN de la TV, code Apple) ----------
const SUIVIES = {};   // boîte -> session suivie (pour l'annuler à la fermeture)
function annulerSession(cle) {
  const s = SUIVIES[cle];
  if (s && !s.fini) { s.fini = true; api(`/api/sessions/${s.id}/cancel`, { method: "POST" }).catch(() => {}); }
  delete SUIVIES[cle];
}
// Invite brute de plumesign (« [… INFO plumesign::…] Enter 2FA code: ») ou jeton interne (PIN_REQUIRED).
function invite(sess) {
  if (sess.kind === "pair") return "";
  const p = String(sess.prompt || "").replace(/^\[[^\]]*\]\s*/, "").trim();
  if (/SMS refused/i.test(p)) return t("prompt_device_sms_denied");
  if (/via SMS/i.test(p)) return t("prompt_sms");
  if (/type 'sms'/i.test(p)) return t("prompt_device_or_sms");
  if (/2FA|code/i.test(p)) return t("prompt_device");
  return p;
}
function watchSession(sess, box, { cle, label, doneText, numerique, onEnd }) {
  annulerSession(cle);
  const suivi = (SUIVIES[cle] = { id: sess.id, fini: false });
  let saisie = false;      // champ affiché et pas encore validé
  let envois = 0;          // nombre de codes envoyés
  let gen = 0;             // ignore les réponses lancées avant le dernier envoi
  let rates = 0;
  const statut = (html) => (box.innerHTML = `<p class="statut">${html}</p>`);
  const fin = (ok) => { suivi.fini = true; if (SUIVIES[cle] === suivi) delete SUIVIES[cle]; onEnd?.(ok); refresh(); };
  const afficherSaisie = () => {
    saisie = true;
    box.innerHTML = `<div class="code"><p class="group__header">${esc(label)}</p>
      ${invite(sess) ? `<p class="code__invite">${esc(invite(sess))}</p>` : ""}
      ${envois ? `<p class="code__invite">${t("new_entry")}</p>` : ""}
      <div class="list"><input ${numerique ? 'inputmode="numeric" pattern="[0-9]*" maxlength="8"' : 'inputmode="text" maxlength="12"'} autocomplete="one-time-code" aria-label="${esc(label)}" spellcheck="false"><button type="button" class="btn btn--primary" data-valider>${t("validate")}</button></div>
      ${/type 'sms'/i.test(sess.prompt || "") ? `<p class="code__invite"><button type="button" class="lien" data-sms>${t("send_sms")}</button></p>` : ""}</div>`;
    const input = $("input", box);
    const go = async (valeur) => {
      if (!saisie) return;
      const v = (valeur ?? input.value).trim();
      if (!v) return input.focus();
      saisie = false; envois++; gen++;
      statut(`<span class="etat etat--cours">${t("verifying")}</span>`);
      try { await api(`/api/sessions/${sess.id}/answer`, { json: { value: v } }); }
      catch (e) { if (e.status !== 409) box.insertAdjacentHTML("beforeend", `<p class="erreur">${esc(e.message)}</p>`); }
      gen++;   // l'état « running » est posé par le serveur avant la réponse : tout « waiting » ultérieur est une nouvelle demande
    };
    $("[data-valider]", box).onclick = () => go();
    const sms = $("[data-sms]", box);
    if (sms) sms.onclick = () => go("sms");
    input.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } };
    input.focus();
  };
  const tick = async () => {
    if (suivi.fini) return;
    const g = gen;
    let s;
    try { s = await api(`/api/sessions/${sess.id}`); rates = 0; }
    catch (e) {
      if (suivi.fini) return;
      if (e.status === 404) { box.innerHTML = `<p class="erreur">${t("session_expired")}</p>`; return fin(false); }
      if (++rates >= 3 && !saisie) statut(`<span class="etat etat--warn">${t("server_retry")}</span>`);
      return setTimeout(tick, 3000);
    }
    if (suivi.fini) return;
    if (g !== gen) return setTimeout(tick, 500);   // réponse périmée (envoi en cours)
    sess = s;
    if (sess.state === "waiting") { if (!saisie) afficherSaisie(); return setTimeout(tick, 1500); }
    if (sess.state === "done") { statut(`<span class="etat etat--ok">${esc(doneText)}</span>`); return fin(true); }
    if (sess.state === "failed") {
      const log = (sess.log || []).join("\n");
      const e = enClair(erreursSeules(log) || log);
      box.innerHTML = `<p class="statut"><span class="etat etat--err">${t("failed")}</span></p>
        ${e.connu ? `<p class="code__invite"><strong>${esc(e.titre)}.</strong> ${esc(e.conseil)}</p>` : ""}
        <pre class="log">${esc(log)}</pre>`;
      return fin(false);
    }
    saisie = false;
    statut(`<span class="etat etat--cours">${sess.state === "finishing" ? t("finishing") : envois ? t("verifying") : t("in_progress")}</span>`);
    setTimeout(tick, 1000);
  };
  tick();
}
// fermer la feuille (Annuler, Échap) abandonne la session côté serveur : plumesign rend la main
$("#dlg-login").addEventListener("close", () => { annulerSession("login"); $("#login-submit").disabled = false; });
$("#dlg-pair").addEventListener("close", () => annulerSession("pair"));

// ---------- appairage ----------
async function scan(host) {
  const list = $("#pair-list");
  annulerSession("pair");
  $("#pair-consigne").innerHTML = t("pair_steps");
  $("#pair-session").innerHTML = "";
  $("#pair-rescan").disabled = $("#pair-host-go").disabled = true;
  list.innerHTML = `<p class="statut"><span class="etat etat--cours">${t("searching")}</span></p>`;
  try {
    const r = await api("/api/devices/discover", { method: "POST", json: host ? { host } : {} });
    const vues = r.paired.filter((d) => d.device_id).map((d) => (STATE?.devices || []).find((x) => x.id === d.device_id)?.name || d.name);
    list.innerHTML = (r.pairable.length ? `<div class="list">${r.pairable.map((d, i) => `
      <div class="row">
        <span class="row__icon row__icon--gray">${ICONS.tv}</span>
        <div class="row__label"><p class="row__title">${esc(d.name)}</p><p class="row__sub">${esc(d.ip)}</p></div>
        <div class="row__buttons"><button type="button" class="row__action row__action--go" data-pair-idx="${i}">${t("pair")}</button></div>
      </div>`).join("")}</div>`
      : `<div class="vide"><p>${t(host ? "pair_none_host" : "pair_none")}</p><p>${t(host ? "pair_none_host_hint" : "pair_none_hint")}</p>${STATE?.windows && !host ? `<p>${t("pair_none_hint_win")}</p>` : ""}</div>`)
      + (vues.length ? `<p class="group__footer">${esc(t("pair_already_visible", { names: [...new Set(vues)].join(", ") }))}</p>` : "");
    list.onclick = (e) => {
      const b = e.target.closest("[data-pair-idx]");
      if (b) startPair(r.pairable[b.dataset.pairIdx]);
    };
    if (!r.pairable.length) $("#pair-manuel").open = true;     // rien trouvé : l'adresse à la main est la suite
  } catch (e) {
    list.innerHTML = `<p class="erreur">${esc(enClair(e.message).titre)}</p>`;
    $("#pair-manuel").open = true;
  } finally {
    $("#pair-rescan").disabled = $("#pair-host-go").disabled = false;
  }
}
async function startPair(d) {
  $("#pair-list").innerHTML = `<p class="statut"><span class="etat etat--cours">${esc(t("pair_connecting", { name: d.name }))}</span></p>`;
  try {
    const sess = await api("/api/devices/pair", { json: { identifier: d.identifier, ip: d.ip, port: d.port, name: d.name } });
    watchSession(sess, $("#pair-session"), { cle: "pair", label: t("pin_label"), doneText: t("paired_ok", { name: d.name }), numerique: true });
  } catch (e) {
    $("#pair-session").innerHTML = `<p class="erreur">${esc(e.message)}</p>`;
  }
}
// « Chercher » n'envoie l'adresse que si la section est ouverte et le champ rempli
$("#pair-rescan").onclick = () => scan($("#pair-manuel").open ? $("#pair-host").value.trim() || undefined : undefined);
$("#pair-host-go").onclick = () => scan($("#pair-host").value.trim() || undefined);
$("#pair-host").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#pair-host-go").click(); } });
// Annuler / Fermer : type=button, pour qu'Entrée dans un champ déclenche l'action principale
$$("dialog [data-close]").forEach((b) => (b.onclick = () => b.closest("dialog").close("cancel")));

// ---------- connexion Apple ----------
$("#form-login").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value !== "ok") return;
  ev.preventDefault();
  const f = ev.target, btn = $("#login-submit");
  if (btn.disabled) return;
  $("#login-error").textContent = "";
  if (!f.ack.checked && !(STATE?.accounts || []).length) return ($("#login-error").textContent = t("login_ack_needed"));
  btn.disabled = true;
  try {
    const sess = await api("/api/accounts/login", { json: { email: f.email.value.trim(), password: f.password.value } });
    f.password.value = "";
    watchSession(sess, $("#login-session"), {
      cle: "login", label: t("apple_code"), doneText: t("account_connected"), numerique: false,
      onEnd: () => { btn.disabled = false; },
    });
  } catch (e) {
    $("#login-error").textContent = e.message;
    btn.disabled = false;
  }
});

// ═══════════════ Connexion à l'interface / premier lancement ═══════════════
function afficher(vue) {
  VUE = vue;
  for (const v of ["auth", "onboarding", "main", "settings", "diagnostics"]) $(`#vue-${v}`).hidden = v !== vue;
  document.body.dataset.vue = vue;
  if (vue !== "main") document.title = { auth: "tvsign", onboarding: t("welcome_title") + " · tvsign", settings: t("settings") + " · tvsign", diagnostics: t("diagnostics") + " · tvsign" }[vue];
}

function renderAuth() {
  const setup = AUTH.setup_required;
  const f = $("#form-auth");
  $$('input[name="langue-porte"]').forEach((r) => (r.checked = r.value === LANG));
  $("#auth-titre").textContent = setup ? t("setup_title") : t("login_title");
  $("#auth-texte").textContent = setup ? (AUTH.setup_code_required ? t("setup_text") : t("setup_text_local")) : t("login_text");
  $("#auth-code-champ").hidden = !(setup && AUTH.setup_code_required);
  $("#auth-confirm-champ").hidden = !setup;
  $("#auth-mdp-label").textContent = setup ? (AUTH.trusted ? t("password_optional") : t("new_password")) : t("password");
  f.password.autocomplete = setup ? "new-password" : "current-password";
  $("#auth-note").textContent = setup ? (AUTH.setup_code_required ? t("setup_code_where") : t("setup_local_note")) : "";
  $("#auth-submit").textContent = setup ? t("secure_continue") : t("sign_in");
  $("#auth-error").textContent = "";
}
$("#vue-auth").addEventListener("change", (e) => {
  if (e.target.name === "langue-porte") { changerLangue(e.target.value); renderAuth(); }
});
$("#form-auth").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target, err = $("#auth-error"), btn = $("#auth-submit");
  err.textContent = "";
  try {
    btn.disabled = true;
    if (AUTH.setup_required) {
      const pw = f.password.value;
      if (pw || !AUTH.trusted) {
        if (pw.length < 8) return (err.textContent = t("err_password_too_short", { n: 8 }));
        if (pw !== f.confirm.value) return (err.textContent = t("password_mismatch"));
      }
      await api("/api/setup", { json: { code: f.code.value, password: pw || null, language: store.get("tvsign.lang") || null }, quiet: true });
    } else {
      await api("/api/auth/login", { json: { password: f.password.value }, quiet: true });
    }
    f.reset();
    await demarrer();
  } catch (e) {
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});

// ═══════════════ Assistant de premier lancement ═══════════════
const ETAPES = ["welcome", "check", "account", "tv", "app", "done"];
let WIZ = { i: 0, diag: null, diagEnCours: false, appAjoutee: false, restaurer: false };

function renderWizard() {
  const etape = ETAPES[WIZ.i];
  $("#wiz-etapes").innerHTML = ETAPES.map((e, i) => `<li class="${i < WIZ.i ? "fait" : i === WIZ.i ? "actuelle" : ""}" ${i === WIZ.i ? 'aria-current="step"' : ""}><span class="sr">${esc(t("step_n", { n: i + 1, total: ETAPES.length }))} ${esc(t("wiz_" + e))}</span></li>`).join("");
  const corps = $("#wiz-corps");
  const html = WIZ_RENDU[etape]();
  setHTML(corps, html);
  $("#wiz-retour").hidden = WIZ.i === 0 || etape === "done";
  $("#wiz-passer").hidden = !["account", "tv", "app"].includes(etape) || etapeFaite(etape);
  const suivant = $("#wiz-suivant");
  suivant.hidden = etape === "welcome" && WIZ.restaurer;
  suivant.textContent = etape === "done" ? t("open_tvsign") : etape === "welcome" ? t("get_started") : t("continue");
  suivant.disabled = ["account", "tv", "app"].includes(etape) && !etapeFaite(etape);
}
function etapeFaite(e) {
  if (!STATE) return false;
  if (e === "account") return STATE.accounts.length > 0;
  if (e === "tv") return STATE.devices.some((d) => !d.legacy_pairing && !d.pairing_lost);
  if (e === "app") return STATE.apps.length > 0 || WIZ.appAjoutee;
  return true;
}
const titreEtape = (k, sous) => `<p class="assistant__eyebrow">${esc(t("step_n", { n: WIZ.i + 1, total: ETAPES.length }))}</p><h1 class="assistant__titre" id="wiz-titre">${t(k)}</h1>${sous ? `<p class="assistant__texte">${sous}</p>` : ""}`;
const fait = (txt) => `<p class="statut"><span class="etat etat--ok">${esc(txt)}</span></p>`;

const WIZ_RENDU = {
  welcome: () => `${titreEtape("welcome_title", t("welcome_text"))}
    <p class="group__header espace">${t("language")}</p>
    <div class="segmented" role="radiogroup" aria-label="${esc(t("language"))}">
      <label><input type="radio" name="wiz-langue" value="fr" ${LANG === "fr" ? "checked" : ""}><span>Français</span></label>
      <label><input type="radio" name="wiz-langue" value="en" ${LANG === "en" ? "checked" : ""}><span>English</span></label>
    </div>
    <ul class="points">
      <li>${ICONS.user}<span>${t("welcome_p1")}</span></li>
      <li>${ICONS.tv}<span>${t("welcome_p2")}</span></li>
      <li>${ICONS.boucle}<span>${t("welcome_p3")}</span></li>
    </ul>
    <div class="list">
      <div class="row">
        <div class="row__label"><p class="row__title">${t("restore_instead")}</p><p class="row__sub">${t("restore_instead_sub")}</p></div>
        <div class="row__buttons"><label class="row__action fichier"><input type="file" id="wiz-backup" accept=".tvsign-backup,.zip"><span>${t("choose_file")}</span></label></div>
      </div>
    </div>
    <p class="erreur" id="wiz-erreur" role="alert"></p>`,
  check: () => {
    const d = WIZ.diag;
    const bloquant = d && d.checks.some((c) => c.status === "error" && ["plumesign", "event_loop"].includes(c.id));
    // Windows : la recherche des Apple TV fait apparaître l'alerte du pare-feu ; on l'annonce, puis on lance
    const attente = !!STATE?.windows && !d && !WIZ.diagEnCours;
    return `${titreEtape("wiz_check_title", t("wiz_check_text"))}
      ${STATE?.windows ? `<div class="banner banner--info"><p><strong>${t("fw_title")}</strong>${t("fw_text")}</p></div>` : ""}
      ${attente ? `<button class="btn btn--primary" type="button" id="wiz-diag">${t("run_check")}</button>` : ""}
      ${WIZ.diagEnCours && !d ? `<p class="statut"><span class="etat etat--cours">${t("checking")}</span></p>` : d ? listeDiag(d) : ""}
      ${bloquant ? `<p class="erreur">${t("wiz_check_blocking")}</p>` : ""}
      ${attente ? "" : `<p class="group__footer"><button class="lien lien--inline" type="button" id="wiz-diag" ${WIZ.diagEnCours ? "disabled" : ""}>${t("run_again")}</button></p>`}`;
  },
  account: () => {
    const accs = STATE?.accounts || [];
    return `${titreEtape("wiz_account_title", t("wiz_account_text"))}
      <ul class="points">
        <li>${ICONS.user}<span>${t("wiz_account_p1")}</span></li>
        <li>${ICONS.cle}<span>${t("wiz_account_p2")}</span></li>
        <li>${ICONS.alerte}<span>${t("wiz_account_p3")}</span></li>
      </ul>
      ${accs.length ? accs.map((a) => fait(t("wiz_account_done", { email: a.email }))).join("") : ""}
      <button class="btn ${accs.length ? "" : "btn--primary"}" data-open="dlg-login">${accs.length ? t("add_account") : t("sign_in_apple")}</button>`;
  },
  tv: () => {
    const devs = STATE?.devices || [];
    const ok = devs.filter((d) => !d.legacy_pairing && !d.pairing_lost);
    const legacy = devs.filter((d) => d.legacy_pairing);
    return `${titreEtape("wiz_tv_title", t("wiz_tv_text"))}
      <ol class="consigne">${t("pair_steps")}</ol>
      ${legacy.length ? `<div class="banner banner--info"><p><strong>${esc(tn("legacy_t", legacy.length, { names: legacy.map((d) => d.name).join(", ") }))}</strong>${t("legacy")}</p></div>` : ""}
      ${ok.map((d) => fait(t("wiz_tv_done", { name: d.name }))).join("")}
      <button class="btn ${ok.length ? "" : "btn--primary"}" data-open="dlg-pair">${ok.length ? t("pair_another") : t("pair_title")}</button>`;
  },
  app: () => {
    const apps = STATE?.apps || [];
    const tvs = (STATE?.devices || []).filter((d) => !d.legacy_pairing);
    if (apps.length) return `${titreEtape("wiz_app_title", "")}${apps.map((a) => fait(t("wiz_app_done", { name: a.name }))).join("")}
      <p class="assistant__texte">${t("wiz_app_more")}</p>`;
    return `${titreEtape("wiz_app_title", t("wiz_app_text"))}
      ${PRESETS ? presetCartes(PRESETS, "wiz-preset") : `<p class="statut"><span class="etat etat--cours">${t("loading")}</span></p>`}
      <p class="group__header espace">${t("channel")}</p>
      <div class="segmented" role="radiogroup" aria-label="${esc(t("channel"))}">
        <label><input type="radio" name="wiz-canal" value="stable" ${PRESETS?.[0]?.default_channel === "beta" ? "" : "checked"}><span>${t("channel_stable")}</span></label>
        <label><input type="radio" name="wiz-canal" value="beta" ${PRESETS?.[0]?.default_channel === "beta" ? "checked" : ""}><span>${t("channel_beta")}</span></label>
      </div>
      <p class="group__footer">${t("channel_note")}</p>
      ${tvs.length ? "" : `<p class="erreur">${t("pair_first")}</p>`}
      <div class="actions-form">
        <button class="btn btn--primary" id="wiz-installer" ${tvs.length && PRESETS?.length ? "" : "disabled"}>${t("install")}</button>
        <button class="btn" data-open="dlg-app">${t("other_app")}</button>
      </div>
      <p class="erreur" id="wiz-erreur" role="alert"></p>`;
  },
  done: () => {
    const apps = STATE?.apps || [];
    return `${titreEtape("wiz_done_title", t("wiz_done_text"))}
      <ul class="points">
        <li>${ICONS.cloche}<span>${t("wiz_done_p1")}</span></li>
        <li>${ICONS.boucle}<span>${apps.length ? t("wiz_done_p2", { h: Math.round(STATE?.renew_before_hours || 60) }) : t("wiz_done_p2_empty")}</span></li>
      </ul>`;
  },
};

// Détails calculés par le serveur en anglais (dossier de support) : l'interface les écrit dans sa langue.
function detailDiag(c, d) {
  if (c.id === "disk" && c.value != null) return t("diag_disk_d", { gb: c.value, dir: d.data_dir || "" });
  if (c.id === "clock" && typeof c.value === "number") return t("diag_clock_d", { s: (c.value > 0 ? "+" : "") + c.value });
  if (c.id === "clock" && c.value == null && c.status === "warn") return t("diag_clock_none");
  if (c.id === "mdns" && c.value && typeof c.value === "object") return t("diag_mdns_d", c.value);
  return c.detail;
}

function listeDiag(d) {
  return `<div class="list">${d.checks.map((c) => {
    const ton = c.status === "ok" ? "ok" : c.status === "warn" ? "warn" : "err";
    // le conseil réseau dépend du cas : TV joignable en direct, conteneur Docker en pont, pare-feu Windows
    const cleConseil = c.id === "mdns"
      ? (c.value?.direct ? "diag_mdns_direct" : c.value?.bridge ? "diag_mdns_bridge" : d.platform === "win32" ? "diag_mdns_fix_win" : "diag_mdns_fix")
      : `diag_${c.id}_fix`;
    const conseil = c.status !== "ok" ? t(cleConseil) : "";
    return `<div class="row">
      <span class="row__icon row__icon--${COULEUR[ton]}">${c.status === "ok" ? ICONS.ok : ICONS.alerte}</span>
      <div class="row__label">
        <p class="row__title">${t("diag_" + c.id)}</p>
        <p class="row__sub"><span class="etat etat--${ton}">${t("diag_" + c.status)}</span>${c.id === "plumesign" && c.value ? ` · <span class="mono">${esc(c.value)}</span>` : ""}</p>
        <p class="row__sub">${esc(detailDiag(c, d))}</p>
        ${conseil && conseil !== cleConseil ? `<p class="row__sub">${esc(conseil)}</p>` : ""}
      </div>
    </div>`;
  }).join("")}</div>`;
}

async function lancerDiag(apres) {
  if (WIZ.diagEnCours) return;
  WIZ.diagEnCours = true; WIZ.diag = null; apres?.();
  try { WIZ.diag = await api("/api/diagnostics"); }
  catch (e) { WIZ.diag = { checks: [{ id: "plumesign", status: "error", detail: e.message }] }; }
  finally { WIZ.diagEnCours = false; apres?.(); }
}

$("#vue-onboarding").addEventListener("change", async (e) => {
  if (e.target.name === "wiz-langue") { await changerLangue(e.target.value, true); renderWizard(); }
  if (e.target.id === "wiz-backup" && e.target.files[0]) {
    WIZ.restaurer = true;
    const ok = await restaurer(e.target.files[0], $("#wiz-erreur"));
    WIZ.restaurer = false;
    if (ok) { await api("/api/onboarding/complete", { method: "POST" }); return demarrer(); }
    renderWizard();
  }
});
$("#vue-onboarding").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.id === "wiz-diag") lancerDiag(() => { $("#wiz-corps")._html = null; renderWizard(); });
  if (b.id === "wiz-installer") {
    const p = $('input[name="wiz-preset"]:checked'), canal = $('input[name="wiz-canal"]:checked');
    const tv = (STATE?.devices || []).find((d) => !d.legacy_pairing);
    if (!p || !tv) return;
    const fd = new FormData();
    fd.append("source_type", "preset"); fd.append("preset", p.value); fd.append("channel", canal?.value || "stable");
    fd.append("device_id", tv.id);
    await busy(b, "id", t("downloading"), async () => {
      try { await api("/api/apps", { method: "POST", body: fd }); WIZ.appAjoutee = true; await refresh(); }
      catch (err) { const el = $("#wiz-erreur"); if (el) el.textContent = messageErreur(err); }
    });
  }
});
$("#wiz-retour").onclick = () => { if (WIZ.i > 0) { WIZ.i--; renderWizard(); } };
$("#wiz-passer").onclick = () => avancer();
$("#wiz-suivant").onclick = async () => {
  if (ETAPES[WIZ.i] === "done") {
    try { await api("/api/onboarding/complete", { method: "POST" }); } catch { /* repris au prochain lancement */ }
    AUTH.onboarded = true;
    location.hash = "#/";
    return route();
  }
  avancer();
};
function avancer() {
  WIZ.i = Math.min(ETAPES.length - 1, WIZ.i + 1);
  const e = ETAPES[WIZ.i];
  if (e === "check" && !WIZ.diag && !STATE?.windows) lancerDiag(() => { $("#wiz-corps")._html = null; renderWizard(); });
  if (e === "app" && !PRESETS) presets().then(() => { $("#wiz-corps")._html = null; renderWizard(); });
  renderWizard();
  $("#wiz-titre")?.focus?.();
}

// ═══════════════ Réglages ═══════════════
let REGLAGES = null;
async function chargerReglages() {
  try { REGLAGES = await api("/api/settings"); } catch (e) { $("#reglages-error").textContent = e.message; return; }
  const f = $("#form-reglages");
  for (const [k, s] of Object.entries(REGLAGES.settings)) {
    const champ = f.elements[k];
    if (!champ) continue;
    champ.value = s.value ?? "";
    champ.disabled = s.locked;
    const label = champ.closest(".field");
    label.querySelector(".verrou")?.remove();
    if (s.locked) label.insertAdjacentHTML("beforeend", `<span class="verrou" title="${esc(t("set_by_env", { env: s.env }))}">${esc(s.env)}</span>`);
  }
  $("#version").textContent = REGLAGES.version;
  afficherMaj(REGLAGES.update);
  // des apps importées : sans leur .ipa dans la sauvegarde, elles sont à réimporter après une restauration
  const cb = $("#backup-uploads");
  cb.checked = (STATE?.apps || []).some((a) => a.source_type === "upload");
  $("#backup-export").href = "/api/backup/export" + (cb.checked ? "?uploads=true" : "");
  renderSecurite();
  if (typeof chargerNotifs === "function") chargerNotifs();
}
function afficherMaj(u) {
  const el = $("#update-status");
  if (!u) return;
  el.textContent = !u.enabled ? t("update_status_off")
    : u.error ? t("update_status_error", { why: enClair(u.error).titre })
    : u.available ? t("update_status_new", { v: u.latest })
    : u.checked_at ? t("update_status_current", { when: relatif(u.checked_at, maintenant()) })
    : t("update_status_never");
}
$("#update-check").onclick = async (ev) => {
  const b = ev.currentTarget, err = $("#reglages-error");
  err.textContent = "";
  await busy(b, "id", t("checking"), async () => {
    try { afficherMaj(await api("/api/update/check", { method: "POST" })); await refresh(); }
    catch (e) { err.textContent = e.message; }
  });
};
function renderSecurite() {
  const a = REGLAGES.auth;
  const box = $("#securite");
  if (!a.enabled) {
    box.innerHTML = `<div class="list"><div class="row"><div class="row__label"><p class="row__title">${t("auth_off_t")}</p><p class="row__sub">${t("auth_off")}</p></div></div></div>`;
    return;
  }
  const changer = a.password_from_env ? `<p class="group__footer">${esc(t("set_by_env", { env: "TVSIGN_PASSWORD" }))}</p>` : `
    <form id="form-mdp" class="list" novalidate>
      ${a.has_password && !a.trusted ? `<label class="field"><span>${t("current_password")}</span><input name="current" type="password" autocomplete="current-password"></label>` : ""}
      <label class="field"><span>${a.has_password ? t("new_password") : t("password")}</span><input name="new" type="password" autocomplete="new-password"></label>
      <label class="field"><span>${t("password_confirm")}</span><input name="confirm" type="password" autocomplete="new-password"></label>
    </form>
    <p class="erreur" id="mdp-error" role="alert"></p>
    <div class="actions-form">
      <button class="btn" id="mdp-ok" type="button">${a.has_password ? t("change_password") : t("set_password")}</button>
      ${a.has_password && a.trusted ? `<button class="btn" id="mdp-retirer" type="button">${t("remove_password")}</button>` : ""}
    </div>`;
  box.innerHTML = `<p class="group__footer premier">${a.has_password ? t("password_on") : t("password_off_local")}</p>
    ${changer}
    <div class="list espace-haut">
      <div class="row">
        <div class="row__label"><p class="row__title">${t("sessions")}</p><p class="row__sub">${t("sessions_sub")}</p></div>
        <div class="row__buttons">
          ${a.has_password ? `<button class="row__action" id="deconnexion" type="button">${t("sign_out")}</button>` : ""}
          <button class="row__action row__action--danger" id="deconnexion-partout" type="button">${t("sign_out_all")}</button>
        </div>
      </div>
    </div>`;
}
$("#form-reglages").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target, err = $("#reglages-error"), body = {};
  err.textContent = "";
  for (const [k, s] of Object.entries(REGLAGES?.settings || {})) {
    const champ = f.elements[k];
    if (!champ || s.locked) continue;
    const v = champ.value.trim();
    if (v !== String(s.value ?? "")) body[k] = v === "" ? null : v;
  }
  if (!Object.keys(body).length) return flash("ok", t("saved_t"));
  try {
    await api("/api/settings", { method: "PATCH", json: body });
    if ("language" in body) await changerLangue(body.language || "auto", false);
    await chargerReglages();
    flash("ok", t("saved_t"));
    afficherFlashReglages();
  } catch (e) { err.textContent = e.message; }
});
function afficherFlashReglages() {
  const el = $("#reglages-error");
  el.textContent = "";
  const b = $("#reglages-submit"), txt = b.textContent;
  b.textContent = t("saved_t"); setTimeout(() => (b.textContent = txt), 1800);
}
$("#vue-settings").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const err = $("#mdp-error");
  try {
    if (b.id === "mdp-ok") {
      const f = $("#form-mdp");
      if (f.new.value.length < 8) return (err.textContent = t("err_password_too_short", { n: 8 }));
      if (f.new.value !== f.confirm.value) return (err.textContent = t("password_mismatch"));
      await api("/api/auth/password", { json: { current: f.current?.value || null, new: f.new.value } });
      f.reset(); err.textContent = "";
      flash("ok", t("password_saved"));
      await chargerReglages();
      b.blur();
    }
    if (b.id === "mdp-retirer" && confirm(t("confirm_remove_password"))) {
      await api("/api/auth/password", { json: { new: null } });
      await chargerReglages();
    }
    if (b.id === "deconnexion") { await api("/api/auth/logout", { method: "POST" }); return demarrer(); }
    if (b.id === "deconnexion-partout" && confirm(t("confirm_sign_out_all"))) {
      await api("/api/auth/logout-all", { method: "POST" }); return demarrer();
    }
  } catch (ex) { if (err) err.textContent = ex.message; else flash("err", t("action_failed"), " " + ex.message); }
});
$("#backup-uploads").onchange = (e) => ($("#backup-export").href = "/api/backup/export" + (e.target.checked ? "?uploads=true" : ""));
// L'export passe par fetch : un « occupé » (connexion Apple ou appairage en cours) s'affiche au lieu d'un
// téléchargement raté ou d'un fichier contenant {"detail":"busy"}.
$("#backup-export").addEventListener("click", async (e) => {
  e.preventDefault();
  const lien = e.currentTarget, err = $("#reglages-error");
  if (lien.getAttribute("aria-busy")) return;
  lien.setAttribute("aria-busy", "true");
  err.textContent = "";
  try {
    const r = await fetch(lien.getAttribute("href"), { credentials: "same-origin", headers: { "X-Tvsign": "1" } });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(apiError(typeof d.detail === "string" ? d.detail : "", r.status));
    }
    const nom = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(r.headers.get("content-disposition") || "")?.[1];
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url; a.download = nom ? decodeURIComponent(nom) : "tvsign.tvsign-backup";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (ex) { err.textContent = ex.message; }
  finally { lien.removeAttribute("aria-busy"); }
});
$("#backup-file").onchange = async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f || !confirm(t("confirm_restore"))) return;
  if (await restaurer(f, $("#reglages-error"))) { location.hash = "#/"; route(); }
};
async function restaurer(file, errEl) {
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await api("/api/backup/import", { method: "POST", body: fd });
    const manquantes = r.missing_uploads || [];
    if (manquantes.length) flash("warn", t("restored_t"), " " + t("restored_missing", { apps: manquantes.join(", ") }));
    else flash("ok", t("restored_t"), " " + t("restored", { apps: r.apps, tvs: r.devices }));
    return true;
  } catch (e) {
    if (errEl) errEl.textContent = e.message;
    return false;
  }
}

// ═══════════════ Diagnostic ═══════════════
async function renderDiagPage() {
  const box = $("#diag"), b = $("#diag-relancer");
  b.disabled = true;
  box.innerHTML = `<p class="statut"><span class="etat etat--cours">${t("checking")}</span></p>`;
  try {
    const d = await api("/api/diagnostics");
    box.innerHTML = listeDiag(d) + `<p class="group__footer">${esc(t("diag_footer", { v: d.version, py: d.python, dir: d.data_dir, s: d.duration }))}</p>`;
  } catch (e) {
    box.innerHTML = `<p class="erreur">${esc(e.message)}</p>`;
  } finally { b.disabled = false; }
}
$("#diag-relancer").onclick = renderDiagPage;

// ═══════════════ Langue ═══════════════
async function changerLangue(lang, serveur = false) {
  const effective = lang === "auto" ? langueNavigateur() : lang;
  store.set("tvsign.lang", lang === "auto" ? "" : lang);
  setLang(effective);
  appliquerI18n();
  if (serveur) { try { await api("/api/settings", { method: "PATCH", json: { language: lang }, quiet: true }); } catch { /* verrouillé par l'env */ } }
  for (const el of ["#apps", "#devices", "#accounts", "#alertes", "#systeme", "#events", "#resume", "#wiz-corps"]) { const n = $(el); if (n) n._html = null; }
  render();
}
function appliquerI18n() {
  document.documentElement.lang = LANG;
  if (typeof annoncerLangue === "function") annoncerLangue();
  $$("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  $$("[data-i18n-placeholder]").forEach((el) => (el.placeholder = t(el.dataset.i18nPlaceholder)));
  $$("[data-i18n-aria]").forEach((el) => el.setAttribute("aria-label", t(el.dataset.i18nAria)));
  $$("[data-i18n-title]").forEach((el) => (el.title = t(el.dataset.i18nTitle)));
  $("#wiz-etapes").setAttribute("aria-label", t("progress"));
}

// ═══════════════ Démarrage et navigation ═══════════════
async function demarrer() {
  try { AUTH = await api("/api/auth/status", { quiet: true }); }
  catch { AUTH = null; }
  const local = store.get("tvsign.lang");
  setLang(local || (AUTH && AUTH.language !== "auto" ? AUTH.language : langueNavigateur()));
  appliquerI18n();
  if (!AUTH) { afficher("main"); ECHECS = 2; DEPUIS = Date.now(); render(); return planifier(); }
  if (!AUTH.authenticated || AUTH.setup_required) { afficher("auth"); renderAuth(); $$("#form-auth label:not([hidden]) input")[0]?.focus(); return; }
  VUE = null;
  await refresh();
  route();
  planifier();
}
function route() {
  if (!AUTH || !AUTH.authenticated) return;
  if (!AUTH.onboarded) { afficher("onboarding"); presets(); renderWizard(); return; }
  const h = location.hash;
  if (h === "#/settings") { afficher("settings"); chargerReglages(); }
  else if (h === "#/diagnostics") { afficher("diagnostics"); renderDiagPage(); }
  else { afficher("main"); render(); }
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

// les comptes à rebours avancent même sans nouvelle réponse du serveur
setInterval(() => { if (!document.hidden && VUE === "main") render(); }, 30000);
demarrer();
