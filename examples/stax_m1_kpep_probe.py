"""Compare Stax's KPEP event setup with a forced allocation on Apple M1.

This reads the local event database. It neither enables counters nor needs root.
"""

import ctypes
import json
import platform
import subprocess


M1_CPU_FAMILY = 0x1B588BB3
EVENTS = ("L1D_CACHE_MISS_LD", "BRANCH_MISPRED_NONSPEC")


def api(library, name, result_type, argument_types):
    function = getattr(library, name)
    function.restype = result_type
    function.argtypes = argument_types
    return function


def probe() -> dict:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Apple M1 host required")
    family = int(subprocess.check_output(["sysctl", "-n", "hw.cpufamily"], text=True).strip())
    if family != M1_CPU_FAMILY:
        raise RuntimeError(f"Expected M1 CPU family {M1_CPU_FAMILY:#x}, found {family:#x}")

    pointer = ctypes.c_void_p
    integer = ctypes.c_int
    count_type = ctypes.c_size_t
    kpep = ctypes.CDLL("/System/Library/PrivateFrameworks/kperfdata.framework/kperfdata")
    kperf = ctypes.CDLL("/System/Library/PrivateFrameworks/kperf.framework/kperf")
    create_db = api(kpep, "kpep_db_create", integer, [ctypes.c_char_p, ctypes.POINTER(pointer)])
    free_db = api(kpep, "kpep_db_free", None, [pointer])
    create_config = api(kpep, "kpep_config_create", integer, [pointer, ctypes.POINTER(pointer)])
    free_config = api(kpep, "kpep_config_free", None, [pointer])
    force = api(kpep, "kpep_config_force_counters", integer, [pointer])
    find_event = api(kpep, "kpep_db_event", integer,
                     [pointer, ctypes.c_char_p, ctypes.POINTER(pointer)])
    add_event = api(kpep, "kpep_config_add_event", integer,
                    [pointer, ctypes.POINTER(pointer), ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)])
    get_map = api(kpep, "kpep_config_kpc_map", integer,
                  [pointer, ctypes.POINTER(count_type), count_type])
    get_classes = api(kpep, "kpep_config_kpc_classes", integer,
                      [pointer, ctypes.POINTER(ctypes.c_uint32)])
    fixed_count = api(kperf, "kpc_get_counter_count", ctypes.c_uint32, [ctypes.c_uint32])(1)

    def configuration(force_first: bool) -> dict:
        database, config = pointer(), pointer()
        try:
            if create_db(None, ctypes.byref(database)) or create_config(database, ctypes.byref(config)):
                raise RuntimeError("KPEP could not create the local event configuration")
            if force_first and force(config):
                raise RuntimeError("KPEP could not force counter allocation")
            added = []
            event_errors = {}
            for name in EVENTS:
                event = pointer()
                if find_event(database, name.encode(), ctypes.byref(event)):
                    raise RuntimeError(f"KPEP event unavailable: {name}")
                error = ctypes.c_uint32()
                code = add_event(config, ctypes.byref(event), 0, ctypes.byref(error))
                event_errors[name] = {"return_code": code, "event_error": error.value}
                if code == 0:
                    added.append(name)
            if not force_first and force(config):
                raise RuntimeError("KPEP could not force counter allocation")
            mapping = (count_type * 32)()
            classes = ctypes.c_uint32()
            if get_map(config, mapping, ctypes.sizeof(mapping)) or get_classes(config, ctypes.byref(classes)):
                raise RuntimeError("KPEP could not return the event map")
            return {
                "force_before_add": force_first,
                "classes": classes.value,
                "event_errors": event_errors,
                "event_to_configurable_counter": dict(zip(added, mapping[:len(added)])),
                "event_to_sample_index": {name: fixed_count + mapping[index]
                                          for index, name in enumerate(added)},
            }
        finally:
            if config.value:
                free_config(config)
            if database.value:
                free_db(database)

    return {
        "cpu_family": f"{family:#x}",
        "fixed_counter_count": fixed_count,
        "requested_events": EVENTS,
        "stax_order": configuration(False),
        "forced_first": configuration(True),
        "hardware_access_verified": False,
    }


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2))
