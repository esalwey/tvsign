"""Windows : les processus lancés par tvsign (plumesign.exe) meurent avec lui.

Un objet Job « kill on close » est créé au premier appel et garde son handle ouvert pour la vie du
processus. Quand tvsign s'arrête (quitter depuis l'icône, déconnexion de session, plantage, fin de
tâche), Windows ferme ce handle et termine tous les enfants du Job : plus de plumesign orphelin qui
continue d'envoyer une app ou d'écrire accounts.json pendant que le tvsign suivant démarre un 2e essai.
Sans effet (et sans erreur) hors Windows ou si le Job est refusé.
"""
import os

_k32 = None          # kernel32 avec ses prototypes
_job = None          # handle du Job, jamais fermé exprès
_failed = False


def _api():
    global _k32
    if _k32 is not None:
        return _k32
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32 = k
    return k


def _create():
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k = _api()
    handle = k.CreateJobObjectW(None, None)
    if not handle:
        return None
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    # 9 = JobObjectExtendedLimitInformation
    if not k.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        k.CloseHandle(handle)
        return None
    return handle


def adopt(pid: int | None) -> bool:
    """Place le processus `pid` dans le Job. True si c'est fait."""
    global _job, _failed
    if os.name != "nt" or not pid or _failed:
        return False
    try:
        if _job is None:
            _job = _create()
            if _job is None:
                _failed = True
                return False
        k = _api()
        proc = k.OpenProcess(0x0100 | 0x0001, False, pid)          # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not proc:
            return False
        try:
            return bool(k.AssignProcessToJobObject(_job, proc))
        finally:
            k.CloseHandle(proc)
    except Exception:  # noqa: BLE001 - confort : jamais bloquant pour un renouvellement
        return False


def in_job(pid: int) -> bool:
    """Le processus est-il dans notre Job ? (tests)"""
    if os.name != "nt" or _job is None:
        return False
    from ctypes import byref, wintypes
    k = _api()
    h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        res = wintypes.BOOL()
        return bool(k.IsProcessInJob(h, _job, byref(res))) and bool(res.value)
    finally:
        k.CloseHandle(h)
