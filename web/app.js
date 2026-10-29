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
let VERSION_CHARGEE = null; // version de tvsign au chargement de la page (le JS en mémoire est de cette version)
// http:// sur le réseau (pas en local) : mots de passe et codes circulent en clair
const NON_CHIFFREE = () => location.protocol === "http:" && !["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
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
  phone: '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><path d="M11 18.5h2"/></svg>',
};
const OS_NAME = { tvos: "tvOS", ios: "iOS", ipados: "iPadOS", visionos: "visionOS" };
const estMobile = (famille) => famille === "ios" || famille === "ipados";   // iPhone / iPad
const platformOf = (famille) => (estMobile(famille) ? "iphoneos" : "appletvos");
// Signature par un certificat importé (réglages de l'app › Avancé) : l'app n'est pas renouvelée tous les 7 jours
const estCert = (a) => a.sign_mode === "certificate";
const certDe = (a) => (STATE?.certs || []).find((c) => c.id === a.cert_id) || null;
const certsDe = (deviceId) => (STATE?.certs || []).filter((c) => c.device_id === deviceId);

async function api(path, opts = {}) {
  const { json, quiet, ...init } = opts;
  init.headers = { "X-Tvsign": "1", ...(init.headers || {}) };
  if (json !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  }
  let r;
  try { r = await fetch(path, { credentials: "same-origin", ...init }); }
  catch (x) {
    if (x?.name === "AbortError") throw x;
    const e = new Error(t("err_network"));      // serveur arrêté, réseau coupé, proxy qui ferme la connexion
    e.network = true;
    throw e;
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detail = data.detail ?? "";
    const code = typeof detail === "string" ? detail : "";
    const e = new Error(apiError(detail, r.status));      // déjà traduit : code stable ou cause connue
    e.status = r.status; e.code = code;
    if (r.status === 401 && /^(auth|setup)_required$/.test(code) && !quiet) demarrer();
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
  "data-select", "data-relogin", "data-logout", "data-devmode", "data-team", "data-open", "data-key", "data-update", "data-rollback", "data-dismiss-update", "data-replace",
  "data-reinstall", "data-retry-version", "data-accept-version", "data-rename-dev", "data-renew-all"];
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
// Délai de renouvellement de l'app, en heures : celui de son appareil (iPhone / iPad : plus long), sinon le général.
const fenetreRenouv = (a, s) => a.renew_before_hours ?? s.renew_before_hours;
// [ton, libellé] ; ton : ok | warn | err | cours | neutre
function etatApp(a, now, s) {
  const exp = a.expires_at, fenetre = fenetreRenouv(a, s) * 3600;
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
  if (estCert(a)) {       // pas de renouvellement : seule la fin du certificat ou du profil est à prévoir
    if (exp - now < 7 * 86400) return ["warn", t("st_cert_expiring")];
    return a.tv_present === false ? ["warn", t("st_not_on_tv")] : ["ok", t("st_valid")];
  }
  if (exp - now < fenetre) {
    // le moment est passé mais la plage horaire choisie n'est pas ouverte : rien n'est en retard
    if (a.window_wait && s.renew_window) return ["neutre", t("st_window", { h: s.renew_window[0] })];
    return ["warn", t("st_to_renew")];
  }
  if (a.tv_present === false) return ["warn", t("st_not_on_tv")];
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
    const pourquoi = m ? t("update_tvos", { min: m[1], have: tvOS(a), os: OS_NAME[a.device_family || "tvos"] || "tvOS" }) : a.update_block === "rollback" ? t("update_rolled_back") : t("update_manual");
    html += `<div class="probleme probleme--info" role="group" aria-label="${esc(t("update_available_t", { tag: quoi }))}">
      <span class="probleme__ic">${ICONS.app}</span>
      <div class="probleme__texte">
        <p class="probleme__titre">${esc(t("update_available_t", { tag: quoi }))}</p>
        <p>${esc(pourquoi)}</p>
        ${a.update_notes ? `<details class="details" data-key="notes-${a.id}"><summary>${t("release_notes")}</summary><pre class="log">${esc(a.update_notes)}</pre></details>` : ""}
      </div>
      ${m ? "" : `<button class="btn btn--petit" data-update="${a.id}" ${a.running ? "disabled" : ""}>${t("install_now")}</button>`}
    </div>`;
  } else {
    if (a.update_block === "pin_missing") {
      // la version épinglée n'existe pas (ou plus) : l'app garde sa version et ne suit plus rien tant que l'épingle est là
      const titre = t("pin_missing_t", { tag: a.pinned_tag || "" });
      html += `<div class="probleme probleme--info" role="group" aria-label="${esc(titre)}">
      <span class="probleme__ic">${ICONS.alerte}</span>
      <div class="probleme__texte">
        <p class="probleme__titre">${esc(titre)}</p>
        <p>${esc(t("pin_missing"))}</p>
      </div>
      <button class="btn btn--petit" data-edit="${a.id}" data-key="edit-pin-${a.id}" aria-label="${esc(t("settings") + " " + a.name)}">${t("settings")}</button>
    </div>`;
    }
    if (a.notes) {
      html += `<details class="details" data-key="notes-${a.id}"><summary>${esc(t("release_notes_v", { v: a.version || a.source_tag || "" }))}</summary><pre class="log">${esc(a.notes)}</pre></details>`;
    }
  }
  if (a.untracked_link) {
    html += `<p class="row__sub">${esc(t(a.auto_update === 0 ? "untracked_link_manual" : "untracked_link"))} <button class="lien lien--inline" data-update="${a.id}" ${a.running ? "disabled" : ""}>${t("check_now")}</button></p>`;
  }
  return html;
}

function jauge(a, now, s, ton) {
  const exp = a.expires_at;
  if (!exp) return "";
  const debut = a.signed_at && a.signed_at < exp ? a.signed_at : exp - (estCert(a) ? 365 : 7) * 86400;
  const total = Math.max(1, exp - debut);
  const pct = Math.min(100, Math.max(0, ((exp - now) / total) * 100));
  const renouv = a.renew_at ?? exp - fenetreRenouv(a, s) * 3600;
  const posRenouv = Math.min(100, Math.max(0, ((renouv - debut) / total) * 100));
  const reste = exp - now;
  const label = reste > 0 ? t("gauge_valid", { left: restant(reste), date: fmtDate(exp) }) : t("gauge_expired", { date: fmtDate(exp) });
  return `<div class="validite">
      <p class="validite__reste">${reste > 0
        ? `<span class="validite__chiffre">${restant(reste)}</span><span class="validite__unite">${t("before_expiry")}</span>`
        : `<span class="validite__chiffre validite__chiffre--err">${t("st_expired")}</span><span class="validite__unite">${t("no_longer_launches")}</span>`}</p>
      <div class="jauge jauge--${ton}" role="img" aria-label="${esc(label)}">
        <span class="jauge__plein" style="width:${pct.toFixed(1)}%"></span>
        ${reste > 0 && !estCert(a) ? `<span class="jauge__repere" style="left:${(100 - posRenouv).toFixed(1)}%" title="${esc(t("auto_renewal"))}"></span>` : ""}
      </div>
      <p class="validite__legende"><span>${esc(t("expires", { date: fmtDate(exp) }))}</span>${estCert(a) && reste > 0 ? `<span>${esc(t("ic_no_renewal"))}</span>` : a.enabled && reste > 0 && a.status !== "error" && !a.running ? `<span>${esc(t("auto_renewal_when", { when: renouv <= now ? t("asap") : relatif(renouv, now) }))}</span>` : ""}</p>
    </div>`;
}

// App ouverte sur l'Apple TV : installation en attente (installd tient la mise à jour jusqu'à sa fermeture).
function noteApp(a, now) {
  const tv = a.device_name || t("device");
  const mobile = estMobile(a.device_family);
  let titre, texte;
  if (a.running && a.phase?.step === "waiting_app") {
    // iOS remplace une app ouverte : une attente y vient d'ailleurs (appareil verrouillé, occupé, plein)
    titre = t(mobile ? "wait_phone_t" : "wait_app_t", { name: mobile ? tv : a.name });
    texte = t(mobile ? "wait_phone" : "wait_app", { tv, when: relatif(a.phase.deadline, now) });
  } else if (!a.running && a.tv_present === false) {
    return `<div class="probleme probleme--info" role="group" aria-label="${esc(t("not_on_tv_t", { tv }))}">
      <span class="probleme__ic">${mobile ? ICONS.phone : ICONS.tv}</span>
      <div class="probleme__texte"><p class="probleme__titre">${esc(t("not_on_tv_t", { tv }))}</p><p>${esc(t("not_on_tv", { when: relatif(a.tv_checked_at, now) }))}</p></div>
      <button class="btn btn--petit" data-reinstall="${a.id}" aria-label="${esc(t("reinstall") + " " + a.name)}">${t("reinstall")}</button>
    </div>`;
  } else return "";
  return `<div class="probleme probleme--info" role="status">
      <span class="probleme__ic">${mobile ? ICONS.phone : ICONS.tv}</span>
      <div class="probleme__texte"><p class="probleme__titre">${esc(titre)}</p><p>${esc(texte)}</p></div>
    </div>`;
}

// Mise à jour refusée par tvsign (identifiant d'app changé, empreinte fausse, IPA invalide) : la raison et quoi faire.
function versionRefusee(a) {
  if (!a.bad_tag) return "";
  const e = a.bad_reason ? enClair(a.bad_reason) : null;
  const titre = e?.connu ? e.titre : t("skipped_t", { tag: a.bad_tag });
  const conseil = e?.connu ? e.conseil : t("skipped_a");
  const detail = a.bad_reason ? `<details class="details" data-key="bad-${a.id}"><summary>${t("tech_details")}</summary><pre class="log">${esc(nettoyer(a.bad_reason) || a.bad_reason)}</pre></details>` : "";
  const accepter = e?.cle === "c_bundle" ? `<button class="btn btn--petit" data-accept-version="${a.id}" aria-label="${esc(t("accept_change") + " " + a.name)}">${t("accept_change")}</button>` : "";
  return `<div class="probleme probleme--info" role="group" aria-label="${esc(t("skipped_version"))}">
      <span class="probleme__ic">${ICONS.alerte}</span>
      <div class="probleme__texte"><p class="probleme__titre">${esc(titre)}</p><p>${esc(conseil)}</p>${detail}</div>
      <span class="probleme__actions">${accepter}<button class="btn btn--petit" data-retry-version="${a.id}" aria-label="${esc(t("retry_version") + " " + a.name)}">${t("retry_version")}</button></span>
    </div>`;
}

// Certificat importé qui va finir (dans moins de 7 jours) : rien n'est renouvelé tout seul, il faut en importer un nouveau.
function noteCert(a, now) {
  if (!estCert(a) || !a.expires_at || a.expires_at - now >= 7 * 86400 || a.expires_at <= now) return "";
  if (a.last_error && /^c_ic_/.test(enClair(a.last_error).cle || "")) return "";       // la carte d'erreur du certificat le dit déjà
  return `<div class="probleme probleme--info" role="status">
      <span class="probleme__ic">${ICONS.cle}</span>
      <div class="probleme__texte"><p class="probleme__titre">${esc(t("ic_soon_t", { when: relatif(a.expires_at, now) }))}</p><p>${esc(t("ic_soon"))}</p></div>
      <button class="btn btn--petit" data-edit="${a.id}" data-key="edit-c-${a.id}" aria-label="${esc(t("settings") + " " + a.name)}">${t("settings")}</button>
    </div>`;
}

// Limites du compte gratuit repérées dans l'IPA (extensions, push, iCloud, domaines associés).
function avertissementsApp(a) {
  const w = (a.warnings || "").split(",").filter(Boolean);
  if (!w.length) return "";
  const lignes = w.map((x) => (x.startsWith("appex:") ? t("warn_appex", { n: x.slice(6) }) : t("warn_" + x.replace(/-/g, "_"))));
  return `<div class="probleme probleme--info" role="status">
      <span class="probleme__ic">${ICONS.alerte}</span>
      <div class="probleme__texte"><p class="probleme__titre">${esc(t("warn_t"))}</p>${lignes.map((l) => `<p>${esc(l)}</p>`).join("")}</div>
    </div>`;
}

// Première installation sur iPhone / iPad : le certificat du développeur est à approuver une fois par Apple ID.
function noteConfiance(a, now) {
  if (!estMobile(a.device_family) || !a.created_at || now - a.created_at > 3 * 86400) return "";
  return `<div class="probleme probleme--info" role="status">
      <span class="probleme__ic">${ICONS.phone}</span>
      <div class="probleme__texte"><p class="probleme__titre">${esc(t("phone_trust_t"))}</p><p>${esc(t("phone_trust"))}</p></div>
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
    : ["c_no_tv", "c_source", "c_bundle", "c_digest", "c_gh_missing", "c_no_release", "c_ic_none", "c_ic_revoked", "c_ic_expired",
       "c_ic_missing", "c_ic_device", "c_ic_profile"].includes(e.cle) ? `<button class="btn btn--petit" data-edit="${a.id}" data-key="edit-p-${a.id}">${t("settings")}</button>` : "";
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
  const essai = a.last_attempt_at ? `${esc(relatif(a.last_attempt_at, now))}${a.last_trigger ? ` (${esc(declencheur(a.last_trigger))})` : ""}` : null;
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
    ${noteCert(a, now)}
    ${noteConfiance(a, now)}
    ${avertissementsApp(a)}
    ${problemeApp(a, now)}
    ${noteMaj(a, now)}
    ${versionRefusee(a)}
    <dl class="meta">
      <div><dt>${t("device")}</dt><dd>${a.device_id ? esc(a.device_name || t("device")) : `<span class="texte-err">${t("none_m")}</span>`}</dd></div>
      ${estCert(a) ? `<div><dt>${t("ic_card_signing")}</dt><dd>${esc(t("ic_card_cert", { name: certDe(a)?.name || "?" }))}</dd></div>`
        : `<div><dt>${t("account")}</dt><dd>${compte ? esc(compte) : t("default_account")}</dd></div>`}
      ${essai ? `<div><dt>${t("last_attempt")}</dt><dd>${essai}</dd></div>` : a.signed_at ? `<div><dt>${t("signed")}</dt><dd>${esc(relatif(a.signed_at, now))}</dd></div>` : ""}
      ${a.verified_at ? `<div><dt>${t("verified_on_tv")}</dt><dd>${esc(relatif(a.verified_at, now))}</dd></div>` : ""}
      ${a.bad_tag && !a.update_tag ? `<div><dt>${t("skipped_version")}</dt><dd class="mono">${esc(a.source_type === "url" ? redactUrl(a.bad_tag) : a.bad_tag)}</dd></div>` : ""}
    </dl>
    <div class="app__actions">
      <button class="row__action row__action--go" data-renew="${a.id}" aria-label="${esc(t("renew") + " " + a.name)}" ${a.running ? "disabled" : ""}>${a.running ? t("in_progress") : t("renew")}</button>
      <button class="row__action" data-toggle="${a.id}" data-enabled="${a.enabled}" aria-label="${esc((a.enabled ? t("pause") : t("resume")) + " " + a.name)}">${a.enabled ? t("pause") : t("resume")}</button>
      ${a.can_rollback ? `<button class="row__action" data-rollback="${a.id}" data-nom="${esc(a.name)}" aria-label="${esc(t("previous_version") + " " + a.name)}" ${a.running ? "disabled" : ""}>${t("previous_version")}</button>` : ""}
      ${a.source_type === "upload" ? `<button class="row__action" data-replace="${a.id}" aria-label="${esc(t("replace_ipa") + " " + a.name)}" ${a.running ? "disabled" : ""}>${t("replace_ipa")}</button>` : ""}
      <button class="row__action" data-edit="${a.id}" aria-label="${esc(t("settings") + " " + a.name)}">${t("settings")}</button>
      <button class="row__action row__action--danger" data-del-app="${a.id}" data-nom="${esc(a.name)}" aria-label="${esc(t("delete") + " " + a.name)}">${t("delete")}</button>
    </div>
  </article>`;
}
// ---------- synthèse globale ----------
const boucleArretee = (h, now) => !!h && (h.loop_stalled || (!!h.last_loop_at && now - h.last_loop_at > 3 * (h.check_interval || 600) + 120));
function synthese(s, now) {
  const h = s.health || null;
  const actives = s.apps.filter((a) => a.enabled);
  const deco = s.accounts.filter((a) => a.ok === 0);
  const expirees = actives.filter((a) => a.expires_at && a.expires_at <= now);
  const echecs = actives.filter((a) => a.status === "error" || !a.device_id);
  const lim = 2 * ((h && h.check_interval) || 600) + 300;   // une passe ne fait que lancer les renouvellements (ils tournent à part)
  const enRetard = actives.filter((a) => !estCert(a) && !a.running && a.status !== "error" && a.expires_at && a.expires_at > now
    && (a.renew_at ?? a.expires_at - fenetreRenouv(a, s) * 3600) < now - lim);
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
  const prochaine = actives.filter((a) => a.expires_at && !estCert(a)).sort((x, y) => x.expires_at - y.expires_at)[0];
  const certifiee = actives.filter((a) => a.expires_at && estCert(a)).sort((x, y) => x.expires_at - y.expires_at)[0];
  // aucune app active avec un profil valide (toutes en pause, ou pas encore installées) : pas de « à jour » vert
  if (!prochaine && !certifiee) return ["neutre", t("sum_idle_t"), t("sum_idle")];
  if (!prochaine) return ["ok", t("sum_ok_t"), t("ic_sum_cert", { when: fmtDate(certifiee.expires_at) })];
  const quand = Math.max(now, prochaine.renew_at ?? prochaine.expires_at - fenetreRenouv(prochaine, s) * 3600);
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
    if (VERSION_CHARGEE && s.version && s.version !== VERSION_CHARGEE) {
      html.push(`<div class="banner banner--info"><p><strong>${t("updated_t")}</strong>${t("updated")}</p>
        <button class="btn btn--primary" data-reload="1">${t("reload")}</button></div>`);
    }
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
    for (const d of s.devices.filter((x) => estMobile(x.family) && x.dev_mode === 0)) {
      html.push(`<div class="banner"><p><strong>${esc(t("devmode_off_t", { name: d.name }))}</strong>${esc(t("devmode_off"))}</p>
        <button class="btn" data-devmode="${d.id}">${t("devmode_check")}</button></div>`);
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
        <button class="btn btn--primary" data-open="dlg-pair" data-pair-kind="${estMobile(d.family) ? "phone" : "tv"}">${t("pair")}</button></div>`);
    }
  }
  setHTML($("#alertes"), html.join(""));
}

function renderApps(s, now) {
  const h = s.health;
  $("#renew-all").hidden = s.apps.filter((a) => a.enabled && a.device_id).length < 2;     // une seule app : « Renouveler » suffit
  const retry = h?.retry_minutes ?? 45;
  // délai de renouvellement : celui de l'appareil de chaque app (un iPhone ou un iPad : plus tôt qu'une Apple TV)
  const actives = s.apps.filter((a) => a.enabled), suivies = actives.length ? actives : s.apps;
  const delais = suivies.map((a) => Math.round(fenetreRenouv(a, s)));
  const court = delais.length ? Math.min(...delais) : Math.round(s.renew_before_hours), long = delais.length ? Math.max(...delais) : court;
  const pied = court === long ? t("apps_footer", { h: court, m: retry }) : t("apps_footer_range", { h: court, hp: long, m: retry });
  setHTML($("#apps"), s.apps.length
    ? `<div class="apps">${s.apps.map((a) => carteApp(a, now, s)).join("")}</div>
       <p class="group__footer">${esc(pied)}</p>`
    : `<div class="vide"><p>${t("apps_empty")}</p><button class="btn btn--primary" data-open="dlg-app">${t("add_app")}</button></div>`);
}

// tvsign ne contacte la TV qu'aux renouvellements et au test de connexion : « joignable » ne vaut que peu de temps.
const TV_FRAIS = 2 * 3600;
function etatTV(d, now) {
  const errRecente = d.last_error && (!d.last_seen || (d.last_error_at || 0) > d.last_seen);
  if (d.legacy_pairing) return ["warn", t("tv_repair_update"), errRecente];
  if (d.pairing_lost) return ["err", t("tv_lost"), errRecente];
  if (errRecente) return ["warn", t("tv_unreachable"), errRecente];
  if (!d.last_seen) return ["neutre", t("tv_never"), errRecente];
  return now - d.last_seen < TV_FRAIS ? ["ok", t("tv_reachable"), errRecente] : ["neutre", t("tv_not_checked"), errRecente];
}

function renderDevices(s, now) {
  setHTML($("#devices"), s.devices.length ? `<div class="list">${s.devices.map((d) => {
    const [ton, mot, errRecente] = etatTV(d, now);
    const apps = s.apps.filter((a) => a.device_id === d.id).map((a) => a.name);
    const mobile = estMobile(d.family), fam = d.family || "tvos";
    const vieux = mobile && parseInt(d.os_version, 10) < 17;
    const notes = [];
    if (mobile && d.dev_mode === 1) notes.push(["", t("devmode_row_on")]);
    if (mobile && d.dev_mode === 0) notes.push(["texte-err", t("devmode_row_off")]);
    if (mobile && d.app_count > 3) notes.push(["texte-err", t("dev_apps_limit", { n: d.app_count })]);
    else if (mobile && d.app_count === 3) notes.push(["", t("dev_apps_full", { n: d.app_count })]);
    if (vieux) notes.push(["texte-err", t("dev_old_os")]);
    return `<div class="row">
      <span class="row__icon row__icon--${COULEUR[ton]}">${mobile ? ICONS.phone : ICONS.tv}</span>
      <div class="row__label">
        <p class="row__title">${esc(d.name)}<small>${esc(t("family_" + fam))}</small></p>
        <p class="row__sub"><span class="etat etat--${ton}">${mot}</span> · ${esc(d.model || t("unknown_model"))} · ${OS_NAME[fam] || "tvOS"} ${esc(d.os_version || "?")}${d.last_ip ? ` · ${esc(d.last_ip)}` : ""}${d.host_hint ? ` · ${esc(t("manual_addr", { host: d.host_hint }))}` : ""}${d.busy ? ` · ${esc(t("tv_busy"))}` : ""}</p>
        <p class="row__sub">${esc(t("last_contact", { when: relatif(d.last_seen, now) }))}${apps.length ? ` · ${esc(apps.join(", "))}` : ""}</p>
        ${notes.map(([cls, txt]) => `<p class="row__sub ${cls}">${esc(txt)}</p>`).join("")}
        ${d.legacy_pairing ? `<p class="row__sub">${t("legacy_row")}</p>`
          : errRecente ? `<p class="row__sub texte-err">${esc(enClair(d.last_error).titre)} ${d.last_error_at ? `(${esc(relatif(d.last_error_at, now))})` : ""}</p>` : ""}
      </div>
      <div class="row__buttons">
        ${d.pairing_lost || d.legacy_pairing ? `<button class="row__action row__action--go" data-open="dlg-pair" data-pair-kind="${mobile ? "phone" : "tv"}">${t("repair")}</button>` : ""}
        ${d.legacy_pairing ? "" : `<button class="row__action" data-refresh-dev="${d.id}" aria-label="${esc(t("test_connection") + " " + d.name)}">${t("test_connection")}</button>`}
        ${d.legacy_pairing ? "" : `<button class="row__action" data-addr-dev="${d.id}" aria-label="${esc(t("address") + " " + d.name)}">${t("address")}</button>`}
        <button class="row__action" data-rename-dev="${d.id}" data-nom="${esc(d.name)}" aria-label="${esc(t("rename") + " " + d.name)}">${t("rename")}</button>
        <button class="row__action row__action--danger" data-del-dev="${d.id}" data-nom="${esc(d.name)}" aria-label="${esc(t("forget") + " " + d.name)}">${t("forget")}</button>
      </div>
    </div>`;
  }).join("")}</div>`
    : `<div class="vide"><p>${t("tv_empty")}</p><button class="btn" data-open="dlg-pair">${t("pair_title")}</button></div>`);
}

const messageCompte = (m) => { const e = enClair(m); return e.connu ? e.titre : nettoyer(m); };
function renderAccounts(s, now) {
  const heures = s.health?.account_check_hours;
  setHTML($("#accounts"), s.accounts.length ? `<div class="list">${s.accounts.map((a) => {
    const etat = a.ok === 1 ? `<span class="etat etat--ok">${t("acc_valid")}</span>`
      : a.ok === 0 ? `<span class="etat etat--err">${t("acc_off")}</span>`
      : a.checked_at ? `<span class="etat etat--neutre">${t("acc_unknown")}</span>` : `<span class="etat etat--neutre">${t("acc_unchecked")}</span>`;
    const note = a.ok == null && a.checked_at && a.message ? `<p class="row__sub">${esc(t("acc_retry_later", { why: enClair(a.message).titre }))}</p>`
      : a.ok === 0 ? `<p class="row__sub texte-err">${esc(a.message ? messageCompte(a.message) : t("acc_reauth_reason"))}</p>` : "";
    const places = (a.slots || []).map((x) => `<p class="row__sub ${x.apps > x.max ? "texte-err" : ""}">${esc(t("slots_line", { apps: x.apps, max: x.max, tv: x.device_name || "Apple TV" }))}${x.apps >= x.max ? " · " + esc(t("slots_note")) : ""}</p>`).join("");
    return `<div class="row">
      <span class="row__icon row__icon--${a.ok === 0 ? "red" : a.ok === 1 ? "blue" : "gray"}">${ICONS.user}</span>
      <div class="row__label">
        <p class="row__title">${esc(a.email)}${a.selected ? `<small>${t("default_account")}</small>` : ""}</p>
        <p class="row__sub">${etat}${a.team_id ? ` · ${t("team")} <span class="mono">${esc(a.team_id)}</span>` : ""}${a.cert_expires ? ` · ${esc(t("cert_until", { date: a.cert_expires }))}` : ""} · ${esc(t("checked_when", { when: relatif(a.checked_at, now) }))}</p>
        ${a.apps ? `<p class="row__sub ${a.app_ids >= 8 ? "texte-err" : ""}">${esc(t("acc_budget", { apps: a.apps, ids: a.app_ids }))}</p>` : ""}
        ${note}${places}
      </div>
      <div class="row__buttons">
        <button class="row__action" data-check="${esc(a.email)}" aria-label="${esc(t("check") + " " + a.email)}">${t("check")}</button>
        <button class="row__action" data-team="${esc(a.email)}" aria-label="${esc(t("team_button") + " " + a.email)}">${t("team_button")}</button>
        ${a.selected ? "" : `<button class="row__action" data-select="${esc(a.email)}" aria-label="${esc(t("use_default") + " " + a.email)}">${t("use_default")}</button>`}
        <button class="row__action ${a.ok === 0 ? "row__action--go" : ""}" data-relogin="${esc(a.email)}" aria-label="${esc(t("reconnect") + " " + a.email)}">${t("reconnect")}</button>
        <button class="row__action row__action--danger" data-logout="${esc(a.email)}" aria-label="${esc(t("remove") + " " + a.email)}">${t("remove")}</button>
      </div>
    </div>`;
  }).join("")}</div>
    <p class="group__footer">${esc(heures ? t("acc_footer_h", { h: Math.round(heures) }) : t("acc_footer"))}${s.accounts.some((x) => x.apps) ? " " + esc(t("acc_budget_note")) : ""}</p>`
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
        ${arret ? `<p class="row__sub">${esc(conseilEdition("restart_hint", AUTH?.edition))}</p>` : ""}
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
    const traduit = evenementTexte(e.message);     // ligne connue du serveur, traduite en anglais
    const titre = traduit ?? (pb && lisible.connu ? `${prefixe ? prefixe + " : " : ""}${lisible.titre}` : nettoyer(e.message).slice(0, 180));
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
    // /api/auth/status avait échoué (serveur en redémarrage) : on le relit maintenant qu'il répond
    if (!AUTH && !AUTH_RELU) { AUTH_RELU = true; demarrer().finally(() => (AUTH_RELU = false)); }
  } catch (e) {
    if (n !== SEQ || e.status === 401) return;
    if (++ECHECS === 1) DEPUIS = Date.now();
  } finally {
    clearTimeout(tm);
    if (n === SEQ) render();
  }
}
let AUTH_RELU = false;

// « Tester la connexion » vérifie aussi, en lecture seule, que les apps sont sur l'Apple TV ; le contrôle périodique
// (et la réinstallation d'une app disparue) est fait par le serveur (TVSIGN_INSTALL_CHECK_HOURS).
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
  if (d.reload) return location.reload();
  if (d.open) return openDialog(d.open, d.pairKind);
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
  if (!(d.renew || d.toggle || d.delApp || d.refreshDev || d.delDev || d.check || d.select || d.logout || d.devmode
      || d.update || d.rollback || d.dismissUpdate || d.reinstall || d.retryVersion || d.acceptVersion
      || d.renameDev || d.renewAll)) return;
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
    if (d.reinstall || d.retryVersion) {
      const [attr, id, chemin] = d.reinstall ? ["data-reinstall", d.reinstall, "renew"] : ["data-retry-version", d.retryVersion, "retry-version"];
      await busy(b, attr, t("starting"), async () => {
        const r = await api(`/api/apps/${id}/${chemin}`, { method: "POST" });
        if (r.started === false) flash("info", t("already_running_t"), " " + t("already_running"));
        else flash("info", t("renew_started_t"), " " + t("renew_started"));
        await refresh();
      });
    }
    if (d.acceptVersion && confirm(t("confirm_accept_change"))) {
      await busy(b, "data-accept-version", t("starting"), async () => {
        await api(`/api/apps/${d.acceptVersion}/accept-version`, { method: "POST" });
        flash("info", t("renew_started_t"), " " + t("renew_started"));
        await refresh();
      });
    }
    if (d.renewAll) {
      await busy(b, "data-renew-all", t("starting"), async () => {
        const r = await api("/api/apps/renew-all", { method: "POST" });
        flash("info", r.started ? t("renew_all_t", { n: r.started }) : t("already_running_t"), " " + (r.started ? t("renew_all") : t("already_running")));
        await refresh();
      });
    }
    if (d.renameDev) {
      const nom = prompt(t("rename_prompt"), d.nom);
      if (nom !== null && nom.trim() && nom.trim() !== d.nom) await api(`/api/devices/${d.renameDev}`, { method: "PATCH", json: { name: nom.trim() } });
    }
    if (d.refreshDev) {
      await busy(b, "data-refresh-dev", t("connecting"), async () => {
        await api(`/api/devices/${d.refreshDev}/refresh`, { method: "POST" });
        // la TV répond : on regarde aussi si les apps y sont encore (sans que ça puisse masquer la connexion réussie)
        let verif = null;
        try { verif = await api(`/api/devices/${d.refreshDev}/verify`, { method: "POST" }); } catch { /* liste d'apps indisponible */ }
        const absentes = (verif?.apps || []).filter((x) => !x.present).map((x) => STATE?.apps.find((a) => a.id === x.app_id)?.name).filter(Boolean);
        if (absentes.length) flash("err", t("tv_ok_t"), " " + t("tv_missing_apps", { apps: absentes.join(", ") }));
        else flash("ok", t("tv_ok_t"), " " + t(verif ? "tv_ok_verified" : "tv_ok"));
      });
    }
    if (d.devmode) {
      await busy(b, "data-devmode", t("devmode_checking"), async () => {
        const r = await api(`/api/devices/${d.devmode}/devmode`, { method: "POST" });
        const nom = STATE?.devices.find((x) => String(x.id) === d.devmode)?.name || "";
        if (r.dev_mode === true) flash("ok", t("devmode_on_t"));
        else if (r.dev_mode === false) flash("err", t("devmode_off_t", { name: nom }), " " + t("devmode_off"));
        else flash("info", t("devmode_unknown_t"));
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
    flash("err", t("action_failed"), " " + e.message);     // déjà traduit par api()
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

function openDialog(id, kind) {
  if (!STATE && id !== "dlg-login") return flash("err", t("sum_unreachable_t"), " " + t("retry_moment"));
  if (id === "dlg-app") fillAppForm();
  if (id === "dlg-pair") {
    $("#pair-manuel").open = false; $("#pair-host").value = "";    // une adresse saisie avant ne doit pas masquer la recherche automatique
    $(`input[name="pair_kind"][value="${kind === "phone" ? "phone" : "tv"}"]`).checked = true;
    syncPairKind();
  }
  if (id === "dlg-login") {
    $("#login-session").innerHTML = ""; $("#login-error").textContent = "";
    $("#login-submit").disabled = false;
    // l'avertissement sur les certificats se confirme une fois, au premier compte
    $("#login-ack-row").hidden = !!(STATE?.accounts || []).length;
    $("#form-login").ack.checked = false;
    $("#login-cancel").textContent = t("cancel");
    $("#http-avis-login").hidden = !NON_CHIFFREE();
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
let PRESETS_ECHEC = false;     // la dernière lecture a échoué : on réessaie à la prochaine demande
async function presets() {
  if (!PRESETS) {
    try { PRESETS = await api("/api/presets", { quiet: true }); PRESETS_ECHEC = false; }
    catch { PRESETS_ECHEC = true; return []; }
  }
  return PRESETS;
}
const presetDesc = (p) => (p.description && (p.description[LANG] || p.description.en)) || p.repo;
function presetCartes(list, nom, choisi = null) {
  const present = !!choisi && list.some((p) => p.id === choisi);      // le choix de l'utilisateur survit à un nouveau rendu
  return list.length ? `<div class="list">${list.map((p, i) => `
    <label class="row row--choix">
      <input type="radio" name="${nom}" value="${esc(p.id)}" ${present ? (p.id === choisi ? "checked" : "") : i === 0 ? "checked" : ""}>
      <span class="row__icon row__icon--blue">${ICONS.app}</span>
      <span class="row__label"><span class="row__title">${esc(p.name)}</span><span class="row__sub">${esc(presetDesc(p))}</span>
        <span class="row__sub mono">${esc(p.repo)}</span></span>
    </label>`).join("")}</div>` : `<div class="vide"><p>${t("no_presets")}</p></div>`;
}
const optionsTV = (choix) => STATE.devices.map((d) => `<option value="${d.id}" ${d.id === choix ? "selected" : ""}>${esc(d.name)} (${esc(t("family_" + (d.family || "tvos")))})</option>`).join("");
// préréglages qui conviennent à la famille de l'appareil choisi (Apple TV : apps tvOS ; iPhone / iPad : apps iPhone)
const presetsFor = (list, device) => list.filter((p) => (p.platform || "appletvos") === platformOf(device?.family));
const appareilChoisi = () => STATE.devices.find((d) => String(d.id) === $("#form-app").device_id.value);
async function fillAppForm() {
  const f = $("#form-app");
  f.device_id.innerHTML = optionsTV(STATE.devices[0]?.id) || `<option value="">${t("no_tv_paired")}</option>`;
  f.device_id.disabled = !STATE.devices.length;
  f.account_email.innerHTML = `<option value="">${t("default_account")}</option>` + STATE.accounts.map((a) => `<option>${esc(a.email)}</option>`).join("");
  $("#app-error").textContent = STATE.devices.length ? "" : t("pair_first");
  $("#app-submit").disabled = !STATE.devices.length;
  syncTabs();
  await presetsDeAppareil();
}
async function presetsDeAppareil() {
  const f = $("#form-app");
  const tous = await presets();
  if (PRESETS_ECHEC) {
    // liste illisible : on le dit et on propose de réessayer, sans changer d'onglet à la place de l'utilisateur
    $("#app-presets").innerHTML = `<p class="erreur">${t("presets_failed")} <button class="lien lien--inline" type="button" id="app-presets-retry">${t("try_again")}</button></p>`;
    return;
  }
  const list = presetsFor(tous, appareilChoisi());
  $("#app-presets").innerHTML = presetCartes(list, "preset");
  canalDuPreset("preset", "channel");
  if (!list.length && f.source_type.value === "preset") { f.source_type.value = "github"; syncTabs(); }
}
$("#app-presets").addEventListener("click", (e) => { if (e.target.closest("#app-presets-retry")) fillAppForm(); });
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
$("#form-app").addEventListener("change", (e) => {
  if (e.target.name === "source_type") syncTabs();
  if (e.target.name === "device_id") presetsDeAppareil();
});
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
    const depot = normaliserDepot(f.source.value);
    if (!depotValide(depot)) return (err.textContent = t("repo_format"));
    f.source.value = depot;
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
  const libelle = type === "upload" ? t("uploading") : t("downloading"), debut = Date.now();
  const chrono = setInterval(() => { btn.textContent = `${libelle} ${Math.round((Date.now() - debut) / 1000)} s`; }, 1000);
  btn.disabled = true; btn.textContent = libelle;
  err.textContent = "";
  $("#app-note").textContent = t("add_wait_note");     // le téléchargement se fait dans la requête : pas de pourcentage
  try {
    const app = await api("/api/apps", { method: "POST", body: fd });
    f.closest("dialog").close();
    f.reset();
    flash("ok", t("app_added_t", { name: app.name }), " " + t("app_added") + ((app.warnings || []).includes("slots_full") ? t("warn_slots_full", { max: STATE?.max_active_apps || 3 }) : ""));
  } catch (e) {
    // coupure (proxy à délai court) : le serveur continue et l'app apparaîtra si l'IPA est valide
    err.textContent = e.network || [502, 503, 504].includes(e.status) ? t("add_maybe_running") : e.message;
  } finally {
    clearInterval(chrono);
    $("#app-note").textContent = "";
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
  // Avancé › signature : Apple ID (par défaut) ou certificat importé
  const dispo = STATE.capabilities?.imported_cert !== false;
  f.sign_mode.value = estCert(a) ? "certificate" : "apple_id";
  f.querySelector('input[name="sign_mode"][value="certificate"]').disabled = !dispo && !estCert(a);
  $("#edit-cert-unavailable").hidden = dispo;
  $("#edit-advanced").open = estCert(a) || (a.status === "error" && /^c_ic_/.test(enClair(a.last_error).cle || ""));
  for (const id of ["#edit-cert-error", "#edit-cert-ok"]) $(id).textContent = "";
  f.cert_password.value = ""; f.cert_p12.value = ""; f.cert_profile.value = "";
  syncSign(a.cert_id);
  $("#dlg-edit").showModal();
}

// ---------- signature par un certificat importé (réglages d'une app › Avancé) ----------
const jour = (ts) => new Date(ts * 1000).toLocaleDateString(LOCALE(), { day: "numeric", month: "short", year: "numeric" });
function infoCert(c) {
  const etat = c.state === "revoked" ? t("ic_state_revoked") : c.state === "missing" ? t("ic_state_missing")
    : c.state === "expired" ? t("ic_state_expired", { date: jour(c.expires_at) })
    : t(c.state === "expiring" ? "ic_state_expiring" : "ic_state_ok", { date: jour(c.expires_at) });
  const parts = [c.team_name || c.team_id, (c.platforms || []).join(" / "),
    c.profile_app_id === "*" ? t("ic_scope_all") : t("ic_scope_ids", { id: c.profile_app_id }), etat];
  if (c.checked_at) parts.push(t("ic_checked", { when: relatif(c.checked_at, maintenant()) }));
  if ((c.apps || []).length) parts.push(t("ic_used_by", { apps: c.apps.join(", ") }));
  return parts.filter(Boolean).join(" · ");
}
// Liste des certificats de l'appareil choisi dans la feuille, et façons d'importer (nouveau, ou remplacement du choisi).
function syncSign(choisi) {
  const f = $("#form-edit"), a = EDIT;
  const cert = f.sign_mode.value === "certificate";
  $("#edit-cert-bloc").hidden = !cert;
  if (!cert) return;
  const deviceId = +f.device_id.value || a.device_id;
  const liste = deviceId ? certsDe(deviceId) : [];
  const courant = choisi ?? (+f.cert_id.value || null);
  f.cert_id.innerHTML = liste.length
    ? liste.map((c) => `<option value="${c.id}" ${c.id === courant ? "selected" : ""}>${esc(c.name)}</option>`).join("")
    : `<option value="">${esc(t(deviceId ? "ic_none_yet" : "ic_needs_device"))}</option>`;
  if (liste.length && !liste.some((c) => c.id === courant)) f.cert_id.value = String(liste[0].id);
  const c = liste.find((x) => String(x.id) === f.cert_id.value);
  $("#edit-cert-row").hidden = !c;
  $("#edit-cert-info").textContent = c ? infoCert(c) : "";
  f.cert_target.innerHTML = `<option value="new">${esc(t("ic_as_new"))}</option>`
    + (c ? `<option value="${c.id}">${esc(t("ic_as_replace", { name: c.name }))}</option>` : "");
  if (c && ["expired", "revoked", "missing"].includes(c.state)) f.cert_target.value = String(c.id);
  $("#edit-cert-import").disabled = !deviceId;
}
$("#form-edit").addEventListener("change", (e) => {
  if (e.target.name === "sign_mode" || e.target.name === "device_id") syncSign();
  else if (e.target.name === "cert_id") syncSign(+e.target.value || null);
});
$("#form-edit").elements.cert_password.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#edit-cert-import").click(); }     // Entrée importe, elle n'enregistre pas la feuille
});
$("#edit-cert-import").onclick = async (ev) => {
  const b = ev.currentTarget, f = $("#form-edit"), a = EDIT, err = $("#edit-cert-error"), ok = $("#edit-cert-ok");
  err.textContent = ""; ok.textContent = "";
  const p12 = f.cert_p12.files[0], profil = f.cert_profile.files[0];
  if (!p12 || !profil) return (err.textContent = t("ic_import_missing"));
  const fd = new FormData();
  fd.append("device_id", +f.device_id.value || a.device_id);
  fd.append("p12", p12); fd.append("profile", profil); fd.append("password", f.cert_password.value);
  fd.append("app_id", a.id);
  if (f.cert_target.value !== "new") fd.append("replace_id", f.cert_target.value);
  await busy(b, "id", t("ic_importing"), async () => {
    try {
      const r = await api("/api/certs", { method: "POST", body: fd });
      f.cert_password.value = ""; f.cert_p12.value = ""; f.cert_profile.value = "";
      await refresh();
      syncSign(r.cert.id);
      ok.textContent = [t("ic_imported_t") + ".", t(f.cert_target.value === "new" ? "ic_imported" : "ic_replaced"),
        ...(r.warnings || []).map((w) => t("ic_warn_" + w))].join(" ");
    } catch (e) { err.textContent = e.message; }
  });
};
$("#edit-cert-check").onclick = async (ev) => {
  const b = ev.currentTarget, f = $("#form-edit"), id = +f.cert_id.value, err = $("#edit-cert-error"), ok = $("#edit-cert-ok");
  err.textContent = ""; ok.textContent = "";
  if (!id) return;
  await busy(b, "id", t("checking"), async () => {
    try {
      const r = await api(`/api/certs/${id}/check`, { method: "POST" });
      await refresh();
      syncSign(id);
      if (r.state === "revoked") err.textContent = t("ic_check_revoked_t");
      else ok.textContent = r.state === "good" ? t("ic_check_good_t") : t("ic_check_unknown");
    } catch (e) { err.textContent = e.message; }
  });
};
$("#edit-cert-remove").onclick = async () => {
  const f = $("#form-edit"), id = +f.cert_id.value, err = $("#edit-cert-error"), ok = $("#edit-cert-ok");
  const c = (STATE?.certs || []).find((x) => x.id === id);
  err.textContent = ""; ok.textContent = "";
  if (!c || !confirm(t("ic_confirm_remove", { name: c.name }))) return;
  try {
    await api(`/api/certs/${id}`, { method: "DELETE" });
    await refresh();
    syncSign();
    ok.textContent = t("ic_removed_t");
  } catch (e) { err.textContent = e.message; }
};
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
    const src = normaliserDepot(f.source.value);
    if (!depotValide(src)) return (err.textContent = t("repo_format"));
    f.source.value = src;
    if (src !== a.source) body.source = src;
    if (f.source_filter.value.trim() !== (a.source_filter || "")) body.source_filter = f.source_filter.value.trim();
    if (f.channel.value !== (a.channel === "beta" ? "beta" : "stable")) body.channel = f.channel.value;
  }
  if (a.source_type === "url") {
    const src = f.source_url.value.trim();
    if (!/^https:\/\//.test(src)) return (err.textContent = t("https_only"));
    if (src !== a.source) body.source = src;
  }
  const mode = f.sign_mode.value;
  if (mode !== (estCert(a) ? "certificate" : "apple_id")) body.sign_mode = mode;
  if (mode === "certificate") {
    const cid = +f.cert_id.value;
    if (!cid) { $("#edit-advanced").open = true; return (err.textContent = t("ic_pick_one")); }
    if (body.sign_mode || cid !== a.cert_id) { body.sign_mode = "certificate"; body.cert_id = cid; }
  }
  if (a.source_type !== "upload" && (f.auto_update.value === "1") !== (a.auto_update !== 0)) body.auto_update = f.auto_update.value === "1";
  if (a.source_type === "github" && f.pinned_tag.value.trim() !== (a.pinned_tag || "")) body.pinned_tag = f.pinned_tag.value.trim();
  if (!Object.keys(body).length) return f.closest("dialog").close();
  if (body.account_email && !confirm(t("confirm_account_change", { name: a.name, email: body.account_email }))) return;
  if (body.sign_mode && !confirm(t(body.sign_mode === "certificate" ? "ic_confirm_switch" : "ic_confirm_back", { name: a.name }))) return;
  const btn = $("#edit-submit");
  btn.disabled = true;
  try {
    await api(`/api/apps/${a.id}`, { method: "PATCH", json: body });
    f.closest("dialog").close();
    const relance = "device_id" in body || "account_email" in body || "sign_mode" in body || "cert_id" in body;
    let lance = null;
    if (relance && a.enabled) lance = await api(`/api/apps/${a.id}/renew`, { method: "POST" });   // nouvelle destination : installer tout de suite
    const seulementSignature = ("sign_mode" in body || "cert_id" in body) && !("device_id" in body || "account_email" in body);
    flash("ok", t("saved_t"), !lance ? "" : lance.started === false ? " " + t("dest_after") : " " + t(seulementSignature ? "ic_sign_now" : "dest_now"));
  } catch (e) {
    err.textContent = e.message;
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
  let sms = false;         // la dernière saisie était une demande de SMS : la suivante n'est pas une « nouvelle saisie »
  let numeros = [];        // fins des numéros de téléphone de confiance (plusieurs : l'utilisateur choisit)
  const statut = (html) => (box.innerHTML = `<p class="statut">${html}</p>`);
  const fin = (ok) => { suivi.fini = true; if (SUIVIES[cle] === suivi) delete SUIVIES[cle]; onEnd?.(ok); refresh(); };
  const afficherSaisie = () => {
    saisie = true;
    const n = numerosSms(sess.prompt);
    if (n.length) numeros = n;
    const peutSms = /type 'sms'/i.test(sess.prompt || "");
    const renvoi = /via SMS/i.test(sess.prompt || "") && cle === "login";
    const boutons = (peutSms || renvoi) ? (numeros.length > 1
      ? numeros.map((x) => `<button type="button" class="lien" data-sms="${esc(x)}">${esc(t(renvoi ? "resend_sms_to" : "send_sms_to", { n: x }))}</button>`).join(" ")
      : `<button type="button" class="lien" data-sms="">${t(renvoi ? "resend_sms" : "send_sms")}</button>`) : "";
    box.innerHTML = `<div class="code"><p class="group__header">${esc(label)}</p>
      ${invite(sess) ? `<p class="code__invite">${esc(invite(sess))}</p>` : ""}
      ${envois && !sms ? `<p class="code__invite">${t("new_entry")}</p>` : ""}
      <div class="list"><input ${numerique ? 'inputmode="numeric" pattern="[0-9]*" maxlength="8"' : 'inputmode="text" maxlength="12"'} autocomplete="one-time-code" aria-label="${esc(label)}" spellcheck="false"><button type="button" class="btn btn--primary" data-valider>${t("validate")}</button></div>
      ${boutons ? `<p class="code__invite code__sms">${boutons}</p>` : ""}</div>`;
    const input = $("input", box);
    const go = async (valeur) => {
      if (!saisie) return;
      const v = (valeur ?? input.value).trim();
      if (!v) return input.focus();
      saisie = false; envois++; gen++;
      sms = /^sms\b/i.test(v);
      statut(`<span class="etat etat--cours">${t("verifying")}</span>`);
      try { await api(`/api/sessions/${sess.id}/answer`, { json: { value: v } }); }
      catch (e) { if (e.status !== 409) box.insertAdjacentHTML("beforeend", `<p class="erreur">${esc(e.message)}</p>`); }
      gen++;   // l'état « running » est posé par le serveur avant la réponse : tout « waiting » ultérieur est une nouvelle demande
    };
    $("[data-valider]", box).onclick = () => go();
    $$("[data-sms]", box).forEach((b) => (b.onclick = () => go(b.dataset.sms ? `sms ${b.dataset.sms}` : "sms")));
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
      // connexion : l'erreur d'Apple (mot de passe refusé, compte verrouillé, code faux…) ; le journal reste en détail technique
      const brut = erreursSeules(log) || log;
      const e = cle === "login" ? causeConnexion(brut) : enClair(brut);
      box.innerHTML = `<p class="statut"><span class="etat etat--err">${t("failed")}</span></p>
        ${e.connu ? `<p class="code__invite"><strong>${esc(e.titre)}.</strong> ${esc(e.conseil)}</p>` : ""}
        <details class="details"${e.connu ? "" : " open"}><summary>${t("tech_details")}</summary><pre class="log">${esc(journalSession(sess.log))}</pre></details>`;
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
const pairKind = () => $('input[name="pair_kind"]:checked')?.value || "tv";
function syncPairKind() {
  const k = pairKind();
  $$("#dlg-pair [data-pair]").forEach((el) => (el.hidden = el.dataset.pair !== k));
  $("#pair-rescan").hidden = k !== "tv";
  if (k === "tv") return scan();
  annulerSession("pair");
  $("#pair-session").innerHTML = "";
  $("#import-error").textContent = "";
  $("#pair-phone-steps").innerHTML = t("phone_steps");
  const usb = STATE?.capabilities?.usb_pairing !== false;
  $("#pair-usb").disabled = !usb;
  $("#pair-usb-none").hidden = usb;
}
$("#dlg-pair").addEventListener("change", (e) => { if (e.target.name === "pair_kind") syncPairKind(); });
$("#pair-usb").onclick = async () => {
  const box = $("#pair-session");
  $("#import-error").textContent = "";
  box.innerHTML = `<p class="statut"><span class="etat etat--cours">${t("pair_usb_running")}</span></p>`;
  try {
    const sess = await api("/api/devices/pair-usb", { json: {} });
    watchSession(sess, box, { cle: "pair", label: t("pair_usb"), doneText: t("pair_usb_done"), numerique: false });
  } catch (e) {
    box.innerHTML = `<p class="erreur">${esc(e.message)}</p>`;
  }
};
$("#import-go").onclick = async (ev) => {
  const b = ev.currentTarget, dlg = $("#dlg-pair"), err = $("#import-error"), box = $("#pair-session");
  const file = dlg.querySelector('[name="pfile"]').files[0];
  err.textContent = ""; box.innerHTML = "";
  if (!file) return (err.textContent = t("import_choose"));
  const fd = new FormData();
  fd.append("file", file);
  fd.append("name", dlg.querySelector('[name="pname"]').value.trim());
  fd.append("udid", dlg.querySelector('[name="pudid"]').value.trim());
  fd.append("family", dlg.querySelector('[name="pfamily"]').value);
  await busy(b, "id", t("uploading"), async () => {
    try {
      const r = await api("/api/devices/import-pairing", { method: "POST", body: fd });
      const nom = dlg.querySelector('[name="pname"]').value.trim() || t("device");
      box.innerHTML = `<p class="statut"><span class="etat etat--${r.info_error ? "warn" : "ok"}">${esc(t("import_done", { name: nom }))}</span></p>`
        + (r.info_error ? `<p class="code__invite">${esc(t("import_offline"))}</p>` : "");
      dlg.querySelector('[name="pfile"]').value = "";
      refresh();
    } catch (e) { err.textContent = e.message; }
  });
};
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
      : `<div class="vide"><p>${t(host ? "pair_none_host" : "pair_none")}</p><p>${t(host ? "pair_none_host_hint" : "pair_none_hint")}</p>
          ${host ? "" : `<button type="button" class="btn btn--primary" id="pair-again">${t("pair_search_again")}</button>
          <details class="details"><summary>${t("pair_help_t")}</summary><p class="pair-aide">${esc(conseilEdition("pair_help", AUTH?.edition))}</p></details>`}</div>`)
      + (vues.length ? `<p class="group__footer">${esc(t("pair_already_visible", { names: [...new Set(vues)].join(", ") }))}</p>` : "");
    list.onclick = (e) => {
      const b = e.target.closest("[data-pair-idx]");
      if (b) startPair(r.pairable[b.dataset.pairIdx]);
      if (e.target.closest("#pair-again")) scan();
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
  const liste = $("#pair-list");
  liste.innerHTML = `<p class="statut"><span class="etat etat--cours">${esc(t("pair_connecting", { name: d.name }))}</span></p>`;
  $("#pair-session").innerHTML = "";
  // échec : on propose de recommencer au lieu de laisser la ligne « connexion… » ; succès : plus rien à chercher
  const encore = () => {
    liste.innerHTML = `<p class="statut"><button type="button" class="btn" id="pair-retry">${t("try_again")}</button></p>`;
    $("#pair-retry").onclick = scan;
  };
  try {
    const sess = await api("/api/devices/pair", { json: { identifier: d.identifier, ip: d.ip, port: d.port, name: d.name } });
    if (!$("#dlg-pair").open) {          // feuille fermée pendant la requête : plumesign attendrait un code pour rien
      api(`/api/sessions/${sess.id}/cancel`, { method: "POST", quiet: true }).catch(() => {});
      return;
    }
    watchSession(sess, $("#pair-session"), {
      cle: "pair", label: t("pin_label"), doneText: t("paired_ok", { name: d.name }), numerique: true,
      onEnd: (ok) => { if (ok) liste.innerHTML = ""; else encore(); },
    });
  } catch (e) {
    $("#pair-session").innerHTML = `<p class="erreur">${esc(e.message)}</p>`;
    encore();
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
  const email = f.email.value.trim();
  try {
    const sess = await api("/api/accounts/login", { json: { email, password: f.password.value } });
    f.password.value = "";
    watchSession(sess, $("#login-session"), {
      cle: "login", label: t("apple_code"), doneText: t("account_connected"), numerique: false,
      onEnd: (ok) => {
        btn.disabled = false;
        if (ok) { $("#login-cancel").textContent = t("close"); reessayerApres(email); }
        else f.password.focus();         // l'adresse reste remplie : on ne ressaisit que le mot de passe
      },
    });
  } catch (e) {
    $("#login-error").textContent = e.message;
    btn.disabled = false;
  }
});

// Compte reconnecté : les apps en échec à cause de lui sont relancées tout de suite, sans attendre leur prochain essai.
const CAUSES_COMPTE = ["c_session", "c_disconnected", "c_account_missing"];
async function reessayerApres(email) {
  try {
    await refresh();
    const apps = (STATE?.apps || []).filter((a) => a.enabled && a.device_id && a.status === "error" && !a.running
      && a.effective_account === email && CAUSES_COMPTE.includes(enClair(a.last_error).cle));
    for (const a of apps) await api(`/api/apps/${a.id}/renew`, { method: "POST", quiet: true });
    if (apps.length) { flash("info", t("retrying_now_t"), " " + t("retrying_now", { n: apps.length })); await refresh(); }
  } catch { /* le prochain essai automatique reprendra */ }
}

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
  $("#auth-note").textContent = setup ? (AUTH.setup_code_required ? conseilEdition("setup_code_where", AUTH?.edition) : t("setup_local_note")) : "";
  $("#auth-submit").textContent = setup ? t("secure_continue") : t("sign_in");
  $("#auth-error").textContent = "";
  $("#http-avis-auth").hidden = !NON_CHIFFREE();
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
// adding : requête d'ajout en cours (téléchargement) ; erreur : son échec, gardé ici et non dans le DOM
let WIZ = { i: 0, diag: null, diagEnCours: false, adding: false, erreur: "", appId: null, restaurer: false, preset: null, canal: null };

function renderWizard() {
  const etape = ETAPES[WIZ.i];
  $("#wiz-etapes").innerHTML = ETAPES.map((e, i) => `<li class="${i < WIZ.i ? "fait" : i === WIZ.i ? "actuelle" : ""}" ${i === WIZ.i ? 'aria-current="step"' : ""}><span class="sr">${esc(t("step_n", { n: i + 1, total: ETAPES.length }))} ${esc(t("wiz_" + e))}</span></li>`).join("");
  const corps = $("#wiz-corps");
  const html = WIZ_RENDU[etape]();
  setHTML(corps, html);
  $("#wiz-retour").hidden = WIZ.i === 0 || etape === "done";
  $("#wiz-passer").hidden = !["account", "tv", "app"].includes(etape) || etapeFaite(etape) || (etape === "app" && WIZ.adding);
  const suivant = $("#wiz-suivant");
  suivant.hidden = etape === "welcome" && WIZ.restaurer;
  suivant.textContent = etape === "done" ? t("open_tvsign") : etape === "welcome" ? t("get_started") : t("continue");
  suivant.disabled = ["account", "tv", "app"].includes(etape) && !etapeFaite(etape);
}
// L'app suivie par l'assistant : celle qu'il vient d'ajouter, sinon la première.
const appAssistant = () => (STATE?.apps || []).find((a) => a.id === WIZ.appId) || (STATE?.apps || [])[0] || null;
// installed : installée sur la TV (premier renouvellement réussi) ; running ; failed ; waiting : créée, pas encore démarrée
function etatPremiereApp(a) {
  if (a.running) return "running";
  if (a.status === "error") return "failed";
  if (a.expires_at && a.status === "ok") return "installed";
  return "waiting";
}
function etapeFaite(e) {
  if (!STATE) return false;
  if (e === "account") return STATE.accounts.length > 0;
  if (e === "tv") return STATE.devices.some((d) => !d.legacy_pairing && !d.pairing_lost);
  if (e === "app") { const a = appAssistant(); return !!a && etatPremiereApp(a) === "installed"; }
  return true;
}
const titreEtape = (k, sous) => `<p class="assistant__eyebrow">${esc(t("step_n", { n: WIZ.i + 1, total: ETAPES.length }))}</p><h1 class="assistant__titre" id="wiz-titre" tabindex="-1">${t(k)}</h1>${sous ? `<p class="assistant__texte">${sous}</p>` : ""}`;
const fait = (txt) => `<p class="statut"><span class="etat etat--ok">${esc(txt)}</span></p>`;

// Avancement réel de la première installation, d'après l'état de l'app (et non « ajoutée » = « installée »).
function blocPremiereApp(a) {
  const e = etatPremiereApp(a), now = maintenant();
  if (e === "installed") return fait(t("wiz_app_installed", { name: a.name, device: a.device_name || t("device"), date: fmtDate(a.expires_at) })) + `<p class="assistant__texte">${t("wiz_app_more")}</p>`;
  if (e === "failed") {
    const c = enClair(a.last_error);
    return `<div class="probleme" role="alert"><span class="probleme__ic">${ICONS.alerte}</span>
        <div class="probleme__texte"><p class="probleme__titre">${esc(t("wiz_app_failed", { name: a.name }))} ${esc(c.titre)}</p>${c.conseil ? `<p>${esc(c.conseil)}</p>` : ""}
        <p class="probleme__retry">${esc(a.next_attempt_at && a.next_attempt_at > now ? t("retry_at", { when: relatif(a.next_attempt_at, now) }) : t("retry_next_pass"))}</p></div>
        <button class="btn btn--petit" type="button" id="wiz-reessayer">${t("try_again")}</button></div>
      <p class="assistant__texte">${t("wiz_app_failed_hint")}</p>`;
  }
  const libelle = e === "running" ? etatApp(a, now, STATE)[1] : t("wiz_app_queued");
  return `<p class="statut"><span class="etat etat--cours">${esc(t("wiz_app_installing", { name: a.name }))} ${esc(libelle)}</span></p>
    ${noteApp(a, now)}<p class="assistant__texte">${attenteInstallation(a.device_family)}</p>`;
}
// Première installation : un iPhone ou un iPad doit rester déverrouillé, une Apple TV seulement allumée.
const attenteInstallation = (famille) => t(estMobile(famille) ? "wiz_app_wait_phone" : "wiz_app_wait");

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
        <div class="row__buttons"><label class="row__action fichier"><input type="file" id="wiz-backup" accept=".tvsign-backup,.zip" ${WIZ.restaurer ? "disabled" : ""}><span>${t("choose_file")}</span></label></div>
      </div>
    </div>
    ${WIZ.restaurer ? `<p class="statut"><span class="etat etat--cours">${t("restoring")}</span></p>` : ""}
    <p class="erreur" id="wiz-erreur" role="alert">${esc(WIZ.erreur)}</p>`,
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
        <li>${ICONS.alerte}<span><strong>${t("wiz_account_revoke")}</strong></span></li>
        <li>${ICONS.cle}<span>${t("wiz_account_p2")}</span></li>
        <li>${ICONS.cle}<span>${t("wiz_account_p4")}</span></li>
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
      <ol class="consigne">${t("pair_steps_wiz")}</ol>
      ${legacy.length ? `<div class="banner banner--info"><p><strong>${esc(tn("legacy_t", legacy.length, { names: legacy.map((d) => d.name).join(", ") }))}</strong>${t("legacy")}</p></div>` : ""}
      ${ok.map((d) => fait(t("wiz_tv_done", { name: d.name }))).join("")}
      <div class="actions-form">
        <button class="btn ${ok.length ? "" : "btn--primary"}" data-open="dlg-pair" data-pair-kind="tv">${ok.length ? t("pair_another") : t("pair_title")}</button>
        <button class="btn" data-open="dlg-pair" data-pair-kind="phone">${t("wiz_pair_phone")}</button>
      </div>`;
  },
  app: () => {
    const tvs = (STATE?.devices || []).filter((d) => !d.legacy_pairing);
    const a = appAssistant();
    // l'ajout est en cours (téléchargement, validation) : jamais le formulaire ni « ajoutée » avant d'en connaître l'issue
    if (WIZ.adding) return `${titreEtape("wiz_app_title", "")}
      <p class="statut"><span class="etat etat--cours">${t("wiz_app_downloading")}</span></p>
      <p class="assistant__texte">${attenteInstallation(tvs[0]?.family)}</p>`;
    if (a) return `${titreEtape("wiz_app_title", "")}${blocPremiereApp(a)}`;
    const choix = WIZ.preset, canal = WIZ.canal || (PRESETS?.[0]?.default_channel === "beta" ? "beta" : "stable");
    // seuls les préréglages qui conviennent à l'appareil (Apple TV : apps tvOS ; iPhone / iPad : apps iPhone)
    const proposes = PRESETS ? presetsFor(PRESETS, tvs[0]) : null;
    return `${titreEtape("wiz_app_title", t("wiz_app_text"))}
      ${proposes ? presetCartes(proposes, "wiz-preset", choix)
        : PRESETS_ECHEC ? `<p class="erreur">${t("presets_failed")} <button class="lien lien--inline" type="button" id="wiz-presets">${t("try_again")}</button></p>`
        : `<p class="statut"><span class="etat etat--cours">${t("loading")}</span></p>`}
      ${proposes && !proposes.length && estMobile(tvs[0]?.family) ? `<p class="group__footer">${t("wiz_no_phone_preset")}</p>` : ""}
      <p class="group__header espace">${t("channel")}</p>
      <div class="segmented" role="radiogroup" aria-label="${esc(t("channel"))}">
        <label><input type="radio" name="wiz-canal" value="stable" ${canal === "beta" ? "" : "checked"}><span>${t("channel_stable")}</span></label>
        <label><input type="radio" name="wiz-canal" value="beta" ${canal === "beta" ? "checked" : ""}><span>${t("channel_beta")}</span></label>
      </div>
      <p class="group__footer">${t("channel_note")}</p>
      ${tvs.length ? "" : `<p class="erreur">${t("pair_first")}</p>`}
      <div class="actions-form">
        <button class="btn btn--primary" id="wiz-installer" ${tvs.length && proposes?.length ? "" : "disabled"}>${t("install")}</button>
        <button class="btn" data-open="dlg-app">${t("other_app")}</button>
      </div>
      <p class="erreur" id="wiz-erreur" role="alert">${esc(WIZ.erreur)}</p>`;
  },
  done: () => {
    const s = STATE || { accounts: [], devices: [], apps: [] };
    const tvOk = s.devices.some((d) => !d.legacy_pairing && !d.pairing_lost);
    const installees = s.apps.filter((a) => etatPremiereApp(a) === "installed");
    const manque = [!s.accounts.length && t("wiz_missing_account"), !tvOk && t("wiz_missing_tv"),
      !s.apps.length && t("wiz_missing_app"), s.apps.length && !installees.length && t("wiz_missing_install")].filter(Boolean);
    if (manque.length) return `${titreEtape("wiz_done_title_partial", t("wiz_done_text_partial"))}
      <ul class="points">${manque.map((m) => `<li>${ICONS.alerte}<span>${esc(m)}</span></li>`).join("")}</ul>`;
    const h = Math.round(STATE?.renew_before_hours || 60);
    const premiere = installees[0];
    // iPhone / iPad : l'app s'installe mais ne se lance qu'après deux réglages faits sur l'appareil
    const ouverture = estMobile(premiere.device_family)
      ? `<li>${ICONS.phone}<span>${esc(t("wiz_done_open_phone", { name: premiere.name, device: premiere.device_name || t("device") }))}</span></li>
        <li>${ICONS.cle}<span><strong>${esc(t("phone_trust_t"))}.</strong> ${esc(t("phone_trust"))}</span></li>
        <li>${ICONS.alerte}<span><strong>${esc(t("wiz_done_dev_t"))}.</strong> ${esc(t("wiz_done_dev"))}</span></li>`
      : `<li>${ICONS.tv}<span>${esc(t("wiz_done_open", { name: premiere.name }))}</span></li>`;
    return `${titreEtape("wiz_done_title", t("wiz_done_text"))}
      <ul class="points">
        ${ouverture}
        <li>${ICONS.cloche}<span>${t("wiz_done_p1")}</span></li>
        <li>${ICONS.boucle}<span>${esc(conseilEdition("wiz_done_p2", AUTH?.edition).replace("{h}", h))}</span></li>
        ${STATE?.update_check ? `<li>${ICONS.ok}<span>${t("wiz_done_p3")}</span></li>` : ""}
      </ul>`;
  },
};

// Serveurs anisette du diagnostic : chacun avec ce qu'il annonce (client info, user agent) ou la raison pour
// laquelle il ne répond pas. Aucun n'est jugé sur la plateforme qu'il annonce : seul Apple dit si ses données conviennent.
function lignesServeurs(v) {
  const lignes = v.servers.map((s) => {
    const delai = s.reachable && s.ms != null ? ` · ${s.ms} ms` : "";
    const dernier = s.last_accepted ? ` · ${esc(t("diag_server_last"))}` : "";
    const annonce = s.reachable ? [s.client_info, s.user_agent].filter(Boolean).join(" · ") : s.error;
    return `<p class="row__sub"><span class="etat etat--${s.reachable ? "ok" : "err"}">${t(s.reachable ? "diag_server_up" : "diag_server_down")}</span> <span class="mono">${esc(s.url)}</span>${delai}${dernier}<br><span class="mono">${esc(annonce)}</span></p>`;
  });
  if (v.override) {
    const pair = [v.override.client_info, v.override.user_agent].filter(Boolean).join(" · ");
    lignes.push(`<p class="row__sub">${esc(t("diag_override"))} <span class="mono">${esc(pair)}</span></p>`);
  }
  return lignes.join("");
}

function listeDiag(d) {
  return `<div class="list">${d.checks.map((c) => {
    const ton = c.status === "ok" ? "ok" : c.status === "warn" ? "warn" : "err";
    // le conseil réseau dépend du cas : TV joignable en direct, conteneur Docker en pont, pare-feu Windows
    const cleConseil = c.id === "mdns"
      ? (c.value?.direct ? "diag_mdns_direct" : c.value?.bridge ? "diag_mdns_bridge" : "diag_mdns_fix")
      : `diag_${c.id}_fix`;
    const conseil = c.status !== "ok" ? conseilEdition(cleConseil, AUTH?.edition) : "";
    // les serveurs anisette sont listés un par un (ce qu'ils annoncent, ou pourquoi ils ne répondent pas)
    const detail = c.id === "anisette" && Array.isArray(c.value?.servers) ? lignesServeurs(c.value) : `<p class="row__sub">${esc(diagDetail(c, d))}</p>`;
    return `<div class="row">
      <span class="row__icon row__icon--${COULEUR[ton]}">${c.status === "ok" ? ICONS.ok : ICONS.alerte}</span>
      <div class="row__label">
        <p class="row__title">${t("diag_" + c.id)}</p>
        <p class="row__sub"><span class="etat etat--${ton}">${t("diag_" + c.status)}</span>${c.id === "plumesign" && c.value ? ` · <span class="mono">${esc(c.value)}</span>` : ""}</p>
        ${detail}
        ${conseil ? `<p class="row__sub">${esc(conseil)}</p>` : ""}
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

// Restaurer une sauvegarde depuis l'accueil : confirmation si l'installation contient déjà quelque chose
const nonVide = () => !!STATE && (STATE.apps.length > 0 || STATE.devices.length > 0 || STATE.accounts.length > 0);
$("#vue-onboarding").addEventListener("change", async (e) => {
  if (e.target.name === "wiz-langue") { await changerLangue(e.target.value, true); renderWizard(); }
  if (e.target.name === "wiz-preset") WIZ.preset = e.target.value;
  if (e.target.name === "wiz-canal") WIZ.canal = e.target.value;
  if (e.target.id === "wiz-backup") {
    const fichier = e.target.files[0];
    e.target.value = "";          // choisir de nouveau le même fichier après un échec doit déclencher « change »
    if (!fichier || WIZ.restaurer) return;
    if (nonVide() && !confirm(t("confirm_restore"))) return;
    WIZ.restaurer = true; WIZ.erreur = "";
    renderWizard();
    const ok = await restaurer(fichier, (msg) => (WIZ.erreur = msg));
    WIZ.restaurer = false;
    if (ok) { try { await api("/api/onboarding/complete", { method: "POST" }); } catch { /* repris au prochain lancement */ } return demarrer(); }
    renderWizard();
  }
});
$("#vue-onboarding").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.id === "wiz-diag") lancerDiag(() => { $("#wiz-corps")._html = null; renderWizard(); });
  if (b.id === "wiz-presets") presets().then(() => renderWizard());
  if (b.id === "wiz-reessayer") {
    const a = appAssistant();
    if (a) await busy(b, "id", t("starting"), async () => {
      try { await api(`/api/apps/${a.id}/renew`, { method: "POST" }); await refresh(); }
      catch (err) { WIZ.erreur = err.message; renderWizard(); }
    });
  }
  if (b.id === "wiz-installer") {
    const p = $('input[name="wiz-preset"]:checked'), canal = $('input[name="wiz-canal"]:checked');
    const tv = (STATE?.devices || []).find((d) => !d.legacy_pairing);
    if (!p || !tv || WIZ.adding) return;
    const fd = new FormData();
    fd.append("source_type", "preset"); fd.append("preset", p.value); fd.append("channel", canal?.value || "stable");
    fd.append("device_id", tv.id);
    WIZ.adding = true; WIZ.erreur = "";
    renderWizard();
    try {
      const app = await api("/api/apps", { method: "POST", body: fd });
      WIZ.appId = app.id;
    } catch (err) {
      // l'échec reste dans l'état de l'assistant : le formulaire revient avec l'explication
      WIZ.erreur = err.network || [502, 503, 504].includes(err.status) ? t("add_maybe_running") : err.message;
    } finally {
      WIZ.adding = false;
      await refresh();
      renderWizard();
    }
  }
});
$("#wiz-retour").onclick = () => { if (WIZ.i > 0) { WIZ.i--; renderWizard(); $("#wiz-titre")?.focus?.(); } };
$("#wiz-passer").onclick = () => {
  const a = appAssistant();
  if (ETAPES[WIZ.i] === "app" && a && !confirm(t("wiz_skip_app_warning", { name: a.name }))) return;
  avancer();
};
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
  $("#wiz-titre")?.focus?.();       // le bouton Continuer peut être désactivé à l'étape suivante : le focus va au titre
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
  const actual = $("#anisette-actual");
  if (actual) actual.textContent = Array.isArray(REGLAGES.anisette?.servers) ? t("anisette_actual", { list: REGLAGES.anisette.servers.join(" → ") }) : "";
  $("#version").textContent = REGLAGES.version;
  afficherMaj(REGLAGES.update);
  // des apps importées : sans leur .ipa dans la sauvegarde, elles sont à réimporter après une restauration
  const cb = $("#backup-uploads");
  cb.checked = (STATE?.apps || []).some((a) => a.source_type === "upload");
  // les certificats importés sont une voie secondaire : l'option n'apparaît que s'il y en a un à sauvegarder
  const avecCerts = (STATE?.certs || []).length > 0;
  $("#backup-certs-row").hidden = !avecCerts;
  if (!avecCerts) $("#backup-certs").checked = false;
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
$("#ntfy-generate").onclick = () => {
  const champ = $("#form-notif").elements.url;
  if (!champ || champ.readOnly) return;
  const octets = new Uint8Array(10);
  crypto.getRandomValues(octets);
  champ.value = sujetNtfy(octets);
  $("#ntfy-sujet").textContent = champ.value.split("/").pop();
  $("#ntfy-etapes").open = true;
  champ.focus();
};
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
// Export : téléchargé par fetch pour voir une session expirée, une erreur ou l'attente du verrou au lieu d'enregistrer un JSON d'erreur
$("#backup-export").onclick = async (ev) => {
  const b = ev.currentTarget, err = $("#backup-error");
  err.textContent = "";
  await busy(b, "id", t("preparing"), async () => {
    try {
      const options = [$("#backup-uploads").checked && "uploads=true", $("#backup-certs").checked && "certs=true"].filter(Boolean).join("&");
      const r = await fetch("/api/backup/export" + (options ? "?" + options : ""), { credentials: "same-origin", headers: { "X-Tvsign": "1" } });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        if (r.status === 401) demarrer();
        throw new Error(apiError(data.detail ?? "", r.status));
      }
      const blob = await r.blob();
      const nom = (r.headers.get("Content-Disposition") || "").match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i)?.[1] || "tvsign.tvsign-backup";
      const lien = document.createElement("a");
      lien.href = URL.createObjectURL(blob); lien.download = decodeURIComponent(nom);
      document.body.appendChild(lien); lien.click(); lien.remove();
      setTimeout(() => URL.revokeObjectURL(lien.href), 10000);
      flash("ok", t("export_done_t"), " " + t("export_done"));
    } catch (e) { err.textContent = e.network ? t("err_network") : e.message; }
  });
};
$("#backup-file").onchange = async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  $("#backup-error").textContent = "";
  if (!f || !confirm(t("confirm_restore"))) return;
  if (await restaurer(f, (m) => ($("#backup-error").textContent = m))) {
    STATE = null;          // l'ancien état décrit une autre base : ses identifiants ne valent plus rien
    await refresh();
    location.hash = "#/"; route();
  }
};
async function restaurer(file, onError) {
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await api("/api/backup/import", { method: "POST", body: fd });
    const manquantes = r.missing_uploads || [];
    if (manquantes.length) flash("warn", t("restored_t"), " " + t("restored_missing", { apps: manquantes.join(", ") }));
    else flash("ok", t("restored_t"), " " + t("restored", { apps: r.apps, tvs: r.devices }));
    return true;
  } catch (e) {
    onError?.(e.message);
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
  // setHTML saute un rendu identique au précédent : après une reconnexion le DOM doit repartir de zéro
  const precedent = AUTH;
  try { AUTH = await api("/api/auth/status", { quiet: true }); }
  catch { AUTH = precedent; }           // serveur qui redémarre : on garde ce que l'on savait
  if (AUTH?.version) VERSION_CHARGEE ??= AUTH.version;
  const local = store.get("tvsign.lang");
  setLang(local || (AUTH && AUTH.language !== "auto" ? AUTH.language : langueNavigateur()));
  appliquerI18n();
  const resume = $("#resume");
  resume._html = null; resume.textContent = t("loading_state");
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
