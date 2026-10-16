"""Entry point: `python -m app` (Docker, Windows desktop launcher, manual runs).

Runs uvicorn in-process with a single worker and no reloader. On Windows this keeps the default
Proactor event loop, which is required for asyncio subprocesses (plumesign); uvicorn switches to
the Selector loop when it has to spawn its own worker processes.
Listens on TVSIGN_HOST:TVSIGN_PORT (127.0.0.1:8765 on Windows, 0.0.0.0:8765 elsewhere).

`python -m app reset-password` sets a new admin password (forgotten password), see README.
"""
import asyncio
import os
import sys

import uvicorn

from .config import HOST, PORT


def reset_password() -> int:
    """Nouveau mot de passe administrateur, sans l'ancien : à lancer sur la machine qui héberge tvsign
    (docker exec -it -u <PUID>:<PGID> tvsign python -m app reset-password). Coupe aussi toutes les sessions."""
    import getpass

    from . import auth, db

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("Lance cette commande avec l'utilisateur de tvsign (PUID:PGID), pas en root : "
              "docker exec -it -u 1000:1000 tvsign python -m app reset-password", file=sys.stderr)
        return 2
    if auth.PASSWORD_ENV:
        print("Le mot de passe est imposé par TVSIGN_PASSWORD / TVSIGN_PASSWORD_FILE : modifie cette valeur "
              "puis redémarre tvsign (rien à réinitialiser ici).", file=sys.stderr)
        return 2
    if not auth.enabled():
        print("TVSIGN_AUTH=none : l'interface n'a pas de mot de passe.", file=sys.stderr)
        return 2
    password = os.environ.get("TVSIGN_NEW_PASSWORD") or getpass.getpass("Nouveau mot de passe : ")
    if not os.environ.get("TVSIGN_NEW_PASSWORD") and getpass.getpass("Confirmation : ") != password:
        print("Les deux saisies diffèrent.", file=sys.stderr)
        return 1
    db.init()
    try:
        auth.set_password(password)
    except ValueError:
        print(f"Au moins {auth.MIN_PASSWORD} caractères.", file=sys.stderr)
        return 1
    auth.finish_setup()
    print("Mot de passe modifié ; les sessions ouvertes sont déconnectées.")
    return 0


def main() -> None:
    proxies = os.environ.get("TVSIGN_TRUSTED_PROXIES", "127.0.0.1")
    config = uvicorn.Config(
        "app.main:app", host=HOST, port=PORT, reload=False, workers=1, loop="asyncio",
        proxy_headers=True, forwarded_allow_ips=proxies, server_header=False,
        log_level=os.environ.get("TVSIGN_LOG_LEVEL", "info"),
    )
    server = uvicorn.Server(config)
    # asyncio.run keeps the platform default loop (Proactor on Windows)
    asyncio.run(server.serve())


if __name__ == "__main__":
    if sys.argv[1:2] == ["reset-password"]:
        sys.exit(reset_password())
    main()
