"""Capture Apple host identity without Instruments or launching a helper process."""

import ctypes
import os
import platform


HOST_KEYS = {"cpu": b"machdep.cpu.brand_string", "os_build": b"kern.osversion", "kernel_uuid": b"kern.uuid"}


def _sysctl_text(read, name):
    buffer = ctypes.create_string_buffer(4096)
    length = ctypes.c_size_t(len(buffer))
    ctypes.set_errno(0)
    if read(name, buffer, ctypes.byref(length), None, 0):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    if not 1 < length.value <= len(buffer):
        raise ValueError("Invalid native sysctl metadata length")
    raw = buffer.raw[:length.value]
    if not raw.endswith(b"\0") or not raw[:-1] or b"\0" in raw[:-1]:
        raise ValueError("Invalid native sysctl metadata string")
    return raw[:-1].decode("utf-8")


def capture_environment() -> dict:
    environment = {"system": platform.system(), "arch": platform.machine(), "os_version": platform.mac_ver()[0]}
    if environment["system"] != "Darwin" or environment["arch"] != "arm64":
        return environment
    try:
        native = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        read = native.sysctlbyname
        read.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        read.restype = ctypes.c_int
    except (OSError, AttributeError) as exc:
        environment["metadata_errors"] = {"sysctl": str(exc)}
        return environment
    for field, name in HOST_KEYS.items():
        try:
            environment[field] = _sysctl_text(read, name)
        except (OSError, ValueError) as exc:
            environment.setdefault("metadata_errors", {})[field] = str(exc)
    return environment
