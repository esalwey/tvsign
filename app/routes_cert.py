"""Routes des certificats importés (.p12 + profil de provisionnement) : liste, import ou remplacement, suppression,
contrôle de révocation. Montées par main.py (même authentification, même CSRF). Voir imported_cert.py."""
import asyncio
import time
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from . import db, devices, imported_cert, notifier, renew

router = APIRouter()


def _cert_or_404(ident_id: int) -> dict:
    ident = imported_cert.row(ident_id)
    if not ident:
        raise HTTPException(404, "unknown_cert")
    return ident


@router.get("/api/certs")
def certs_list():
    now = time.time()
    return {"available": imported_cert.AVAILABLE, "certs": [imported_cert.view(c, now) for c in imported_cert.rows()]}


@router.post("/api/certs")
async def cert_import(
    device_id: int = Form(...),
    p12: UploadFile = File(...),
    profile: UploadFile = File(...),
    password: str = Form(""),
    app_id: Optional[int] = Form(None),        # l'app pour laquelle on importe : son identifiant doit être couvert par le profil
    replace_id: Optional[int] = Form(None),    # remplace les fichiers de ce certificat (ses apps continuent de l'utiliser)
):
    """Vérifie le .p12 (mot de passe compris) et le profil AVANT d'enregistrer quoi que ce soit : certificat et profil
    non expirés, profil fait pour ce genre d'appareil et pour cet appareil, certificat qui figure dans le profil,
    identifiant d'app couvert, certificat non révoqué (contrôle OCSP, s'il répond). Rien n'est gardé si l'un échoue."""
    if not imported_cert.AVAILABLE:
        raise HTTPException(501, "cert_unavailable")
    device = db.row("SELECT * FROM devices WHERE id = ?", device_id)
    if not device:
        raise HTTPException(400, "unknown_device")
    app = db.row("SELECT * FROM apps WHERE id = ?", app_id) if app_id is not None else None
    if app_id is not None and not app:
        raise HTTPException(400, "unknown_app")
    if not device["udid"] or not device["os_version"]:
        # the profile lists hardware UDIDs: make sure the one held for this device is its own (as a renewal does)
        try:
            await devices.refresh_info(device_id, wait=False)
            device = db.row("SELECT * FROM devices WHERE id = ?", device_id) or device
        except Exception:  # noqa: BLE001 - device asleep or busy: the identifiers already known are compared
            pass
    existing = None
    if replace_id is not None:
        existing = _cert_or_404(replace_id)
        if existing["device_id"] != device_id:
            raise HTTPException(400, "cert_wrong_device")
    p12_bytes = await p12.read(imported_cert.MAX_P12 + 1)
    profile_bytes = await profile.read(imported_cert.MAX_PROFILE + 1)
    using = [a for a in db.rows("SELECT * FROM apps WHERE cert_id = ? AND sign_mode = ?", replace_id,
                                imported_cert.CERTIFICATE)] if existing else []
    try:
        prep = await asyncio.to_thread(imported_cert.prepare, device, p12_bytes, password, profile_bytes,
                                       None if existing else app)
        for a in using:         # un certificat remplacé doit encore couvrir toutes les apps qui l'utilisent
            why = imported_cert.app_problem(prep.profile.pattern, a)
            if why:
                raise imported_cert.CertError(why)
    except imported_cert.CertError as e:
        raise HTTPException(400, e.code) from None
    status = await asyncio.to_thread(imported_cert.check_revocation, prep.info.der, prep.chain)
    if status.state == "revoked":
        raise HTTPException(400, "cert_revoked")
    try:
        ident = await asyncio.to_thread(imported_cert.commit, prep, device_id, replace_id)
    except OSError:
        raise HTTPException(500, "cert_store_failed") from None
    if status.state == "good":
        imported_cert.record_check(ident["id"], status)
    db.log(f"Certificat importé « {ident['name']} » {'remplacé' if existing else 'ajouté'} pour « {device['name']} », "
           f"valable jusqu'au {notifier.fmt_time(ident['expires_at'])}")
    for a in using:
        # nouveau certificat : la signature faite avec l'ancien ne vaut plus, l'app est signée et installée à nouveau
        (renew.app_dir(a["id"]) / "signed.ipa").unlink(missing_ok=True)
        db.update("apps", a["id"], notified_level=None, **renew.CLEAR_BLOCK)
        if a["enabled"]:
            renew.rerun(a["id"])
            renew.spawn(a["id"], "nouveau certificat")
    return {"cert": imported_cert.view(imported_cert.row(ident["id"])), "warnings": prep.warnings,
            "revocation": status.state}


@router.delete("/api/certs/{ident_id}")
def cert_delete(ident_id: int):
    ident = _cert_or_404(ident_id)
    try:
        imported_cert.remove(ident_id)
    except imported_cert.CertError as e:
        raise HTTPException(409, e.code) from None
    db.log(f"Certificat importé « {ident['name']} » supprimé")
    return {"ok": True}


@router.post("/api/certs/{ident_id}/check")
async def cert_check(ident_id: int):
    """Contrôle de révocation tout de suite (OCSP). « unknown » : aucune conclusion (pas d'adresse, serveur muet)."""
    _cert_or_404(ident_id)
    if not imported_cert.AVAILABLE:
        raise HTTPException(501, "cert_unavailable")
    result = await renew.check_identity(ident_id)
    if result is None:
        raise HTTPException(409, "cert_missing")
    return {"state": result.state, "detail": result.detail, "cert": imported_cert.view(imported_cert.row(ident_id))}
