"""单实例进程锁 —— 使用 Windows 命名互斥体（内核级别，不会 fork 穿透）。"""
from __future__ import annotations

import sys
import atexit
import ctypes
from ctypes import wintypes
from config.logger import get_logger

logger = get_logger("single_instance")

# --- Windows 内核对象 API ---
_HANDLE = wintypes.HANDLE
_DWORD = wintypes.DWORD
_LPCSTR = ctypes.c_char_p
_LPCWSTR = ctypes.c_wchar_p
_BOOL = wintypes.BOOL

kernel32 = ctypes.windll.kernel32

CreateMutexW = kernel32.CreateMutexW
CreateMutexW.restype = _HANDLE
CreateMutexW.argtypes = [ctypes.c_void_p, _BOOL, _LPCWSTR]

CloseHandle = kernel32.CloseHandle
CloseHandle.restype = _BOOL
CloseHandle.argtypes = [_HANDLE]

WaitForSingleObject = kernel32.WaitForSingleObject
WaitForSingleObject.restype = _DWORD
WaitForSingleObject.argtypes = [_HANDLE, _DWORD]

ERROR_ALREADY_EXISTS = 183
WAIT_TIMEOUT = 258
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 128

_mutexes: dict[str, _HANDLE] = {}


def acquire(name: str) -> bool:
    """
    使用 Windows 内核级别命名 Mutex 防止同名进程多重启动。
    返回 True 表示获得锁（可以继续执行）；
    返回 False 表示已有另一个同名进程在运行。
    即使进程被 kill 掉，内核会自动释放 Mutex，不会死锁。
    """
    mutex_name = f"Global\\ProbMonitor_{name}"
    handle = CreateMutexW(None, False, mutex_name)
    if handle is None or handle == 0:
        logger.error("Failed to create mutex '%s'", mutex_name)
        return True  # 出错时保守放行
    err = ctypes.get_last_error()
    if err == ERROR_ALREADY_EXISTS:
        logger.warning("Lock already held for '%s' — another instance is running, exiting.", name)
        CloseHandle(handle)
        return False
    _mutexes[name] = handle
    atexit.register(_cleanup, name)
    logger.info("Lock acquired for '%s' (mutex)", name)
    return True


def _cleanup(name: str) -> None:
    handle = _mutexes.pop(name, None)
    if handle:
        try:
            CloseHandle(handle)
        except Exception:
            pass
