"""Routes ajoutées en 1.1 : destinations de notification, langue de l'interface, journal paginé et
exportable, dossier de support, bandeaux. Montées par main.py (même authentification, même CSRF)."""
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, Response

from . import db, diag, notifier, settings, support

router = APIRouter()


# ---------- destinations de notification ----------

@router.get("/api/notifications")
def notifications_get():
    return notifier.listing()


def _clean_error(e: ValueError) -> HTTPException:
    return HTTPException(400, str(e))


@router.post("/api/notifications")
def notifications_add(body: dict):
    try:
        return notifier.save_target(body)
    except ValueError as e:
        raise _clean_error(e) from None


@router.put("/api/notifications/{target_id}")
def notifications_update(target_id: str, body: dict):
    try:
        return notifier.save_target(body, target_id)
    except LookupError:
        raise HTTPException(404, "unknown_target") from None
    except ValueError as e:
        raise _clean_error(e) from None


@router.delete("/api/notifications/{target_id}")
def notifications_delete(target_id: str):
    try:
        notifier.delete_target(target_id)
    except LookupError:
        raise HTTPException(404, "unknown_target") from None
    except ValueError as e:
        raise _clean_error(e) from None
    return {"ok": True}


@router.post("/api/notifications/test")
async def notifications_test(body: dict):
    """Envoie un message d'essai à la destination saisie (pas forcément enregistrée). Un champ secret
    renvoyé masqué reprend le secret enregistré de la destination `id`."""
    try:
        await notifier.test(body)
    except ValueError as e:
        raise _clean_error(e) from None
    except notifier.NotifyFailed as e:
        raise HTTPException(502, f"notify_failed:{e}") from None
    return {"ok": True}


@router.post("/api/ui-language")
def ui_language(body: dict):
    """Langue vue par l'interface (réglage « auto ») : les notifications partent dans cette langue."""
    lang = body.get("lang")
    if lang in ("fr", "en") and settings.raw_get("ui_lang") != lang:
        settings.raw_set("ui_lang", lang)
    return {"ok": True}


# ---------- journal ----------

@router.get("/api/events")
def events(app_id: int | None = None, level: str | None = None, before: int | None = None, limit: int = 80):
    """Journal paginé, du plus récent au plus ancien : `before` = identifiant du dernier événement déjà reçu."""
    limit = max(1, min(limit, 200))
    where, args = [], []
    if app_id is not None:
        where.append("app_id = ?")
        args.append(app_id)
    if level == "problems":
        where.append("level != 'info'")
    if before is not None:
        where.append("id < ?")
        args.append(before)
    rows = db.rows("SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
                   + " ORDER BY id DESC LIMIT ?", *args, limit + 1)
    return {"events": rows[:limit], "more": len(rows) > limit}


@router.get("/api/log/export")
def log_export(app_id: int | None = None, level: str | None = None):
    """Journal en texte brut, masqué (e-mails, adresses IP, identifiants) : prêt à coller dans un ticket."""
    return PlainTextResponse(support.log_text(app_id, level), headers={
        "Content-Disposition": f'attachment; filename="tvsign-log-{time.strftime("%Y%m%d-%H%M%S")}.txt"'})


# ---------- support ----------

@router.get("/api/support-bundle")
async def support_bundle():
    data = await support.build_bundle()
    return Response(data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="tvsign-support-{time.strftime("%Y%m%d-%H%M%S")}.zip"'})


@router.get("/api/support-summary")
async def support_summary():
    """Résumé texte (version, vérifications, derniers événements), masqué, pour le bouton « Copier »."""
    return {"text": support.summary_text(await diag.run_all())}


# ---------- bandeaux ----------

@router.post("/api/notices/revocation/dismiss")
def dismiss_revocation():
    settings.raw_set("revocation_notice", None)
    return {"ok": True}
