// Ajouts 1.1 : destinations de notification, aide au support, journal paginé.
// Chargé après app.js : s'appuie sur ses fonctions ($, api, t, esc, flash, busy, STATE, LANG…).

// ═══════════════ Langue vue par l'interface ═══════════════
// Réglage « auto » : le serveur ne connaît pas la langue du navigateur, il envoie donc les notifications
// dans la dernière langue affichée.
let LANGUE_ANNONCEE = null;
function annoncerLangue() {
  if (!AUTH?.authenticated || AUTH.language !== "auto" || LANGUE_ANNONCEE === LANG) return;
  LANGUE_ANNONCEE = LANG;
  api("/api/ui-language", { json: { lang: LANG }, quiet: true }).catch(() => { LANGUE_ANNONCEE = null; });
}

// ═══════════════ Notifications ═══════════════
// type -> champs [nom, type d'entrée, secret, exemple]
const NOTIF_CHAMPS = {
  ntfy: [["url", "url", false, "https://ntfy.sh/…"], ["token", "password", true, ""]],
  webhook: [["url", "url", true, "https://…"]],
  discord: [["url", "url", true, "https://discord.com/api/webhooks/…"]],
  telegram: [["token", "password", true, "123456789:ABC…"], ["chat_id", "text", false, "123456789"]],
  gotify: [["url", "url", false, "https://gotify.example.com"], ["token", "password", true, ""]],
};
const NOTIF_EVENTS = ["fail", "expiry", "recovered", "update", "account"];
let NOTIF = null;           // réponse de /api/notifications
let NOTIF_EDIT = null;      // destination ouverte dans la feuille (null : nouvelle)

const nomNotif = (x) => x.name || t("nt_" + x.type);
function resumeEvents(ev) {
  if (!ev.length) return t("notif_none_events");
  if (ev.length === NOTIF_EVENTS.length) return t("notif_all_events");
  return ev.map((e) => t("ev_" + e)).join(", ");
}

async function chargerNotifs() {
  const box = $("#notif-liste");
  try { NOTIF = await api("/api/notifications"); }
  catch (e) { box.innerHTML = `<p class="erreur">${esc(e.message)}</p>`; return; }
  box._html = null;
  setHTML(box, NOTIF.targets.length ? `<div class="list">${NOTIF.targets.map((x) => `
    <div class="row">
      <span class="row__icon row__icon--${x.last_error ? "orange" : "blue"}">${ICONS.cloche}</span>
      <div class="row__label">
        <p class="row__title">${esc(nomNotif(x))}${x.name ? `<small>${esc(t("nt_" + x.type))}</small>` : ""}</p>
        <p class="row__sub">${x.summary ? `<span class="mono">${esc(x.summary)}</span> · ` : ""}${esc(resumeEvents(x.events))}</p>
        ${x.last_error ? `<p class="row__sub texte-err">${esc(x.last_error)}</p>` : ""}
      </div>
      <div class="row__buttons">
        <button class="row__action" type="button" data-notif-test="${esc(x.id)}">${t("send_test")}</button>
        <button class="row__action" type="button" data-notif-edit="${esc(x.id)}">${t("edit")}</button>
        ${x.locked ? "" : `<button class="row__action row__action--danger" type="button" data-notif-del="${esc(x.id)}">${t("delete")}</button>`}
      </div>
    </div>`).join("")}</div>`
    : `<div class="vide"><p>${t("notif_empty")}</p><button class="btn" type="button" data-notif-new>${t("notif_add")}</button></div>`);
}

// Destination ntfy venue de l'adresse des réglages : ni nom ni jeton (le serveur les refuserait), l'accès
// protégé passe par https://utilisateur:mot-de-passe@hôte/sujet.
const integree = (cible) => !!(cible && cible.builtin);
function champsNotif(type, cible) {
  const champs = NOTIF_CHAMPS[type].filter(([nom]) => !(integree(cible) && nom === "token"));
  $("#notif-champs").innerHTML = champs.map(([nom, kind, secret, exemple]) => `
    <label class="field"><span>${t("nf_" + type + "_" + nom)}</span>
      <input name="${nom}" type="${kind}" placeholder="${esc(exemple)}" autocomplete="off" spellcheck="false"
        ${cible && cible[nom] != null ? `value="${esc(cible[nom])}"` : ""}
        ${cible && cible.locked && nom === "url" ? "readonly" : ""}></label>`).join("");
  $("#notif-aide").textContent = t("nh_" + type) + " " + t("nh_privacy")
    + (integree(cible) ? " " + t("nh_ntfy_builtin") : "");
}
function evenementsNotif(actifs) {
  $("#notif-events").innerHTML = NOTIF_EVENTS.map((e) => `
    <label class="row row--choix">
      <input type="checkbox" name="ev" value="${e}" ${actifs.includes(e) ? "checked" : ""}>
      <span class="row__label"><span class="row__title">${t("ev_" + e)}</span><span class="row__sub">${t("ev_" + e + "_sub")}</span></span>
    </label>`).join("");
}
function ouvrirNotif(cible) {
  NOTIF_EDIT = cible || null;
  const f = $("#form-notif");
  f.type.innerHTML = Object.keys(NOTIF_CHAMPS).map((k) => `<option value="${k}">${t("nt_" + k)}</option>`).join("");
  f.type.value = cible ? cible.type : "ntfy";
  f.type.disabled = !!cible;
  f.name.value = cible ? cible.name : "";
  f.name.closest("label").hidden = integree(cible);
  champsNotif(f.type.value, cible);
  evenementsNotif(cible ? cible.events : NOTIF?.events || NOTIF_EVENTS);
  $("#notif-error").textContent = "";
  $("#notif-error").className = "erreur";
  $("#dlg-notif").showModal();
}
function brouillonNotif() {
  const f = $("#form-notif");
  const body = { type: f.type.value, name: integree(NOTIF_EDIT) ? "" : f.name.value.trim(), events: $$('input[name="ev"]:checked', f).map((c) => c.value) };
  for (const [nom] of NOTIF_CHAMPS[body.type]) if (f.elements[nom]) body[nom] = f.elements[nom].value.trim();
  if (NOTIF_EDIT) body.id = NOTIF_EDIT.id;
  return body;
}
function messageNotif(texte, bon) {
  const el = $("#notif-error");
  el.textContent = texte;
  el.className = bon ? "statut" : "erreur";
}
$("#notif-add").onclick = () => ouvrirNotif(null);
$("#form-notif").elements.type.addEventListener("change", (e) => champsNotif(e.target.value, null));
$("#notif-test").onclick = async (ev) => {
  const b = ev.currentTarget;
  messageNotif("", false);
  await busy(b, "id", t("sending"), async () => {
    try { await api("/api/notifications/test", { json: brouillonNotif() }); messageNotif(t("sent"), true); }
    catch (e) { messageNotif(e.message, false); }
  });
};
$("#form-notif").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value !== "ok") return;
  ev.preventDefault();
  const body = brouillonNotif(), btn = $("#notif-submit");
  btn.disabled = true;
  try {
    if (NOTIF_EDIT) await api(`/api/notifications/${encodeURIComponent(NOTIF_EDIT.id)}`, { method: "PUT", json: body });
    else await api("/api/notifications", { json: body });
    $("#dlg-notif").close();
    flash("ok", t("saved_t"));
    await chargerNotifs();
    refresh();
  } catch (e) { messageNotif(e.message, false); }
  finally { btn.disabled = false; }
});
$("#vue-settings").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const d = b.dataset;
  try {
    if ("notifNew" in d) return ouvrirNotif(null);
    if (d.notifEdit) return ouvrirNotif(NOTIF.targets.find((x) => x.id === d.notifEdit));
    if (d.notifDel) {
      const x = NOTIF.targets.find((y) => y.id === d.notifDel);
      if (!confirm(t("notif_confirm_del", { name: nomNotif(x) }))) return;
      await api(`/api/notifications/${encodeURIComponent(d.notifDel)}`, { method: "DELETE" });
      await chargerNotifs();
      refresh();
    }
    if (d.notifTest) {
      const x = NOTIF.targets.find((y) => y.id === d.notifTest);
      await busy(b, "data-notif-test", t("sending"), async () => {
        try { await api("/api/notifications/test", { json: x }); flash("ok", t("sent")); }
        catch (ex) { flash("err", t("notif_failed_t"), " " + ex.message); }
        await chargerNotifs();
      });
    }
  } catch (ex) { flash("err", t("action_failed"), " " + ex.message); }
});

// ═══════════════ Support ═══════════════
$("#support-copy").onclick = async (ev) => {
  const b = ev.currentTarget, box = $("#support-text");
  box.innerHTML = "";
  await busy(b, "id", t("checking"), async () => {
    let texte;
    try { texte = (await api("/api/support-summary")).text; }
    catch (e) { box.innerHTML = `<p class="erreur">${esc(e.message)}</p>`; return; }
    try { await navigator.clipboard.writeText(texte); flash("ok", t("copied")); return; }
    catch { /* page en http sur le réseau local : pas d'API presse-papiers */ }
    box.innerHTML = `<p class="group__footer premier">${esc(t("copy_manually"))}</p><textarea class="log" readonly rows="10" style="width:100%"></textarea>`;
    const ta = $("textarea", box);
    ta.value = texte; ta.focus(); ta.select();
  });
};

// ═══════════════ Journal ═══════════════
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-more-events]");
  if (b) {
    const tous = STATE ? STATE.events.concat(OLDER_EVENTS) : OLDER_EVENTS;
    const dernier = tous.length ? Math.min(...tous.map((x) => x.id)) : null;
    try {
      const r = await api(`/api/events?limit=100${dernier ? "&before=" + dernier : ""}`);
      OLDER_EVENTS = OLDER_EVENTS.concat(r.events.filter((x) => !OLDER_EVENTS.some((o) => o.id === x.id)));
      OLDER_MORE = r.more;
      $("#events")._html = null;
      if (STATE) renderEvents(STATE);
    } catch (ex) { flash("err", t("action_failed"), " " + ex.message); }
  }
  const n = e.target.closest("[data-dismiss-notice]");
  if (n) {
    try { await api(`/api/notices/${n.dataset.dismissNotice}/dismiss`, { method: "POST" }); } catch { /* sera réaffiché */ }
    refresh();
  }
});
