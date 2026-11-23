# pragma: no cover
"""Windows process probes — stand-ins for the Unix-only ``resource`` module.

``ProcessSampler`` reads CPU time from ``resource.getrusage`` and RSS from
``/proc`` on Linux. Neither exists on Windows, where a hard ``import
resource`` used to make the whole benchmark package unimportable. These
ctypes calls provide the same metrics: ``GetProcessTimes`` for the
user/kernel CPU split, ``GetProcessMemoryInfo`` for the working set.

Loaded lazily ONLY on Windows, so nothing here executes on the Linux gate
(file-level coverage pragma). ``CDLL`` loads kernel32/psapi fine on x64 —
the WinDLL/CDLL distinction is a 32-bit calling-convention matter — and
keeps the module type-checkable without platform-stub guards.
"""

from __future__ import annotations

import ctypes
import time

__all__ = ["process_times", "rss_kb"]

_TICKS_PER_SECOND = 10_000_000.0  # FILETIME: 100 ns intervals


class _FileTime(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _seconds(ft: _FileTime) -> float:
    return ((ft.hi << 32) | ft.lo) / _TICKS_PER_SECOND


def _kernel32() -> ctypes.CDLL:
    kernel32 = ctypes.CDLL("kernel32", use_last_error=True)
    # HANDLE is pointer-sized on x64: without the declared restype the
    # pseudo-handle (-1) truncates to 32 bits and every call fails
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p, *([ctypes.POINTER(_FileTime)] * 4)]
    return kernel32


def process_times() -> tuple[float, float]:
    """(user, kernel) CPU seconds for this process.

    Falls back to ``(process_time(), 0.0)`` — the combined total with no
    split — if the Win32 call is unavailable, so the sampler's arithmetic
    never breaks on an exotic host.
    """
    creation, exited, kernel, user = (
        _FileTime(),
        _FileTime(),
        _FileTime(),
        _FileTime(),
    )
    kernel32 = _kernel32()
    ok = kernel32.GetProcessTimes(
        kernel32.GetCurrentProcess(),
        ctypes.byref(creation),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    )
    if not ok:
        return (time.process_time(), 0.0)
    return (_seconds(user), _seconds(kernel))


def rss_kb() -> int | None:
    """Resident working set in KB, or None when the call is unavailable."""
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    kernel32 = _kernel32()
    psapi = ctypes.CDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    ok = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    )
    if not ok:
        return None
    return counters.WorkingSetSize // 1024
