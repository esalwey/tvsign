"""Sous-processus : exécution simple et sessions interactives (code 2FA, PIN d'appairage)."""
import asyncio
import os
import re
import subprocess
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

# Windows : pas de fenêtre console qui clignote à chaque appel de plumesign (app lancée depuis le tray).
_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


async def _exec(*args, **kw) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(*map(str, args), **kw, **_FLAGS)
    except NotImplementedError:
        # SelectorEventLoop sous Windows (uvicorn --reload/--workers) : pas de sous-processus
        raise RuntimeError("boucle asyncio sans sous-processus : lancer tvsign avec « python -m app »") from None


@dataclass
class Result:
    code: int
    output: str
    err: str = ""          # stderr, quand il est capturé à part (split=True)

    @property
    def ok(self) -> bool:
        return self.code == 0

    def tail(self, n: int = 3) -> str:
        lines = [l for l in (self.output + "\n" + self.err).splitlines() if l.strip()]
        return " | ".join(lines[-n:])

    def lines(self) -> list[str]:
        return [l.strip() for l in self.output.splitlines() if l.strip()]


# Dernières sorties des commandes plumesign (dossier de support) : (heure, commande, code, lignes).
# Jamais d'arguments dans l'étiquette (chemins, identifiants d'appareil), et masquées à l'export.
RING: deque = deque(maxlen=60)
_LABEL_WORD = re.compile(r"[a-z][a-z-]*")


def _remember(args, result: "Result") -> None:
    try:
        words = [a for a in map(str, args[1:4]) if _LABEL_WORD.fullmatch(a)]
        lines = [l[:500] for l in (result.err or result.output).splitlines() if l.strip()][-200:]
        RING.append((time.time(), " ".join(["plumesign", *words]), result.code, lines))
    except Exception:  # noqa: BLE001 - jamais gênant pour la commande elle-même
        pass


async def run(*args, env: dict | None = None, timeout: float = 600, split: bool = False,
              on_line=None) -> Result:
    """Exécute une commande. split=True garde stderr à part (plumesign : journal sur stderr,
    résultats sur stdout). on_line(ligne) est appelé pour chaque ligne de stdout dès qu'elle
    arrive ; une exception qu'il lève arrête le processus et remonte à l'appelant."""
    proc = await _exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE if split else asyncio.subprocess.STDOUT,
        env=env,
    )
    out, err = bytearray(), bytearray()

    async def _pump(stream, buf, lines: bool = False) -> None:
        # lecture par blocs : les barres de progression (\r sans \n) dépassent la limite de readline
        pending = b""
        while chunk := await stream.read(65536):
            buf.extend(chunk)
            if lines:
                *done, pending = re.split(rb"[\r\n]", pending + chunk)
                for raw in done:
                    if raw.strip():
                        on_line(raw.decode("utf-8", "replace").strip())
        if lines and pending.strip():
            on_line(pending.decode("utf-8", "replace").strip())

    async def _read() -> None:
        await asyncio.gather(_pump(proc.stdout, out, on_line is not None),
                             *([_pump(proc.stderr, err)] if split else []))
        await proc.wait()

    def text(buf) -> str:
        return buf.decode("utf-8", "replace").replace("\r", "\n")

    try:
        await asyncio.wait_for(_read(), timeout)
    except TimeoutError:
        # on garde la sortie partielle pour le diagnostic
        result = Result(-1, f"{text(out)}\ndélai dépassé ({timeout:.0f} s)", text(err))
        _remember(args, result)
        return result
    finally:
        # tue aussi l'enfant si on est annulé (arrêt, délai externe)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    result = Result(proc.returncode, text(out), text(err))
    _remember(args, result)
    return result


@dataclass
class Session:
    """Processus qui peut s'arrêter pour demander une saisie (code, PIN)."""

    kind: str
    prompt_re: re.Pattern
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # running | waiting | finishing (plumesign a fini, on termine : modèle, version…) | done | failed
    state: str = "running"
    cancelled: bool = False              # abandon voulu par l'utilisateur (pas une panne)
    exit_ok: bool = False                # plumesign est sorti avec le code 0
    prompt: str = ""
    lines: list[str] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    _proc: asyncio.subprocess.Process | None = None
    _task: asyncio.Task | None = None

    async def start(self, *args, env: dict | None = None, on_exit=None) -> None:
        try:
            self._proc = await _exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except Exception as e:  # noqa: BLE001 - binaire absent, bloqué par l'antivirus, non exécutable…
            # sans ça, l'exception se perdait dans une tâche de fond : la session restait « en cours »
            # (spinner sans fin, import de sauvegarde refusé) jusqu'au balayage de 15 min
            self.lines.append(str(e))
            self.state = "failed"
            if on_exit:
                try:
                    await on_exit(self)
                except Exception as e2:  # noqa: BLE001
                    self.lines.append(f"erreur après la fin : {e2}")
            return
        self._task = asyncio.create_task(self._pump(on_exit))

    async def _pump(self, on_exit) -> None:
        assert self._proc and self._proc.stdout
        while line := await self._proc.stdout.readline():
            text = line.decode("utf-8", "replace").rstrip()
            self.lines.append(text)
            if self.prompt_re.search(text):
                self.state, self.prompt = "waiting", text
        code = await self._proc.wait()
        self.exit_ok = code == 0 and not self.cancelled
        # « done » seulement une fois le travail de fin terminé : l'interface n'annonce pas « appairée »
        # alors que le modèle et la version de la TV sont encore en cours de lecture
        self.state = "finishing" if self.exit_ok else "failed"
        if on_exit:
            try:
                await on_exit(self)
            except Exception as e:  # noqa: BLE001 - on veut garder la session lisible
                self.state = "failed"
                self.lines.append(f"erreur après la fin : {e}")
        if self.state == "finishing":
            self.state = "done"

    async def send(self, text: str) -> None:
        assert self._proc and self._proc.stdin
        self.state = "running"
        self._proc.stdin.write((text.strip() + "\n").encode())
        await self._proc.stdin.drain()

    @property
    def user_cancelled(self) -> bool:
        """Fermée par l'utilisateur, pas abandonnée au bout du délai (qui, lui, est une panne)."""
        return self.cancelled and not any(l.startswith("délai dépassé") for l in self.lines[-3:])

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def cancel(self) -> None:
        if self.state in ("running", "waiting"):
            self.cancelled = True
        if self.alive:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.kill()          # _pump passe alors la session en « failed »
        elif self.state in ("running", "waiting"):
            self.state = "failed"      # jamais démarrée (en attente du verrou)

    def public(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "state": self.state, "prompt": self.prompt,
            "log": self.lines[-15:], "result": self.result,
        }


SESSIONS: dict[str, Session] = {}
# Une saisie (code 2FA, PIN) jamais faite ne doit pas bloquer indéfiniment.
WAITING_MAX = 15 * 60


def sweep() -> None:
    """Tue les sessions abandonnées et oublie les anciennes."""
    now = time.time()
    for sid, s in list(SESSIONS.items()):
        age = now - s.created
        if s.state in ("running", "waiting") and age > WAITING_MAX:
            s.lines.append("délai dépassé, session abandonnée")
            s.cancel()
            s.state = "failed"
        if age > 3600:
            SESSIONS.pop(sid).cancel()


def register(session: Session) -> Session:
    sweep()
    SESSIONS[session.id] = session
    return session


def busy() -> bool:
    """Une session interactive (connexion Apple, appairage) est en cours."""
    return any(s.state in ("running", "waiting", "finishing") for s in SESSIONS.values())


# Tâches de fond : on garde une référence (sinon le ramasse-miettes peut les interrompre).
_TASKS: set[asyncio.Task] = set()


def _finished(task: asyncio.Task) -> None:
    _TASKS.discard(task)
    # une exception jamais récupérée disparaissait en silence : on la met au moins au journal
    if not task.cancelled() and (exc := task.exception()) is not None:
        print(f"[error] tâche de fond interrompue : {exc!r}", flush=True)


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_finished)
    return task
