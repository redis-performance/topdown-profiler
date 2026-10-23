"""Check M1 PMU event groups with Apple's local KPEP database, without enabling counters."""

import argparse
import ctypes
import json
import platform
import subprocess


EVENT_GROUPS = {
    "controls": (
        "FIXED_CYCLES", "FIXED_INSTRUCTIONS", "RETIRE_UOP", "BRANCH_MISPRED_NONSPEC",
        "SCHEDULE_UOP", "CORE_ACTIVE_CYCLE", "MAP_DISPATCH_BUBBLE", "MAP_STALL_DISPATCH",
        "L1D_CACHE_MISS_LD", "L1I_CACHE_MISS_DEMAND",
    ),
    "map-state": (
        "FIXED_CYCLES", "FIXED_INSTRUCTIONS", "RETIRE_UOP", "MAP_DISPATCH_BUBBLE",
        "MAP_STALL", "MAP_REWIND", "MAP_STALL_DISPATCH", "MAP_INT_UOP",
        "MAP_LDST_UOP", "MAP_SIMD_UOP",
    ),
    "fetch": (
        "FIXED_CYCLES", "FIXED_INSTRUCTIONS", "RETIRE_UOP", "CORE_ACTIVE_CYCLE",
        "MAP_DISPATCH_BUBBLE", "SCHEDULE_EMPTY", "L1I_CACHE_MISS_DEMAND",
        "L1I_TLB_MISS_DEMAND", "L2_TLB_MISS_INSTRUCTION", "FETCH_RESTART",
    ),
    "calibration": (
        "FIXED_CYCLES", "FIXED_INSTRUCTIONS", "RETIRE_UOP", "MAP_DISPATCH_BUBBLE",
        "MAP_STALL", "MAP_REWIND", "CORE_ACTIVE_CYCLE", "MAP_INT_UOP",
        "MAP_LDST_UOP", "MAP_SIMD_UOP",
    ),
}
M1_CPU_FAMILY = 0x1B588BB3


def _api(library, name, result_type, argument_types):
    function = getattr(library, name)
    function.restype = result_type
    function.argtypes = argument_types
    return function


def plan(group="controls", swap=None):
    events = list(EVENT_GROUPS[group])
    if swap is not None:
        if len(swap) != 2 or swap[0] == swap[1] or any(name not in events[2:] for name in swap):
            raise ValueError("Swap two distinct programmable events in the selected group")
        first, second = (events.index(name) for name in swap)
        events[first], events[second] = events[second], events[first]
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Apple silicon host required")
    family = int(subprocess.check_output(["sysctl", "-n", "hw.cpufamily"], text=True).strip())
    if family != M1_CPU_FAMILY:
        raise RuntimeError(f"Expected M1 CPU family {M1_CPU_FAMILY:#x}, found {family:#x}")

    library = ctypes.CDLL("/System/Library/PrivateFrameworks/kperfdata.framework/kperfdata")
    pointer = ctypes.c_void_p
    create_db = _api(library, "kpep_db_create", ctypes.c_int, [ctypes.c_char_p, ctypes.POINTER(pointer)])
    free_db = _api(library, "kpep_db_free", None, [pointer])
    create_config = _api(library, "kpep_config_create", ctypes.c_int, [pointer, ctypes.POINTER(pointer)])
    free_config = _api(library, "kpep_config_free", None, [pointer])
    force = _api(library, "kpep_config_force_counters", ctypes.c_int, [pointer])
    find_event = _api(library, "kpep_db_event", ctypes.c_int,
                      [pointer, ctypes.c_char_p, ctypes.POINTER(pointer)])
    add_event = _api(library, "kpep_config_add_event", ctypes.c_int,
                     [pointer, ctypes.POINTER(pointer), ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)])
    get_classes = _api(library, "kpep_config_kpc_classes", ctypes.c_int,
                       [pointer, ctypes.POINTER(ctypes.c_uint32)])
    get_count = _api(library, "kpep_config_kpc_count", ctypes.c_int,
                     [pointer, ctypes.POINTER(ctypes.c_size_t)])
    get_map = _api(library, "kpep_config_kpc_map", ctypes.c_int,
                   [pointer, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t])
    get_config = _api(library, "kpep_config_kpc", ctypes.c_int,
                      [pointer, ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t])

    database, config = pointer(), pointer()
    try:
        if create_db(None, ctypes.byref(database)):
            raise RuntimeError("KPEP could not open the local CPU event database")
        if create_config(database, ctypes.byref(config)) or force(config):
            raise RuntimeError("KPEP could not create a forced-counter configuration")
        for name in events:
            event = pointer()
            if find_event(database, name.encode(), ctypes.byref(event)):
                raise RuntimeError(f"KPEP event unavailable: {name}")
            error = ctypes.c_uint32()
            code = add_event(config, ctypes.byref(event), 0, ctypes.byref(error))
            if code:
                raise RuntimeError(f"KPEP rejected {name}: code={code}, event_error={error.value}")

        classes, count = ctypes.c_uint32(), ctypes.c_size_t()
        mapping = (ctypes.c_size_t * 32)()
        registers = (ctypes.c_uint64 * 32)()
        if (get_classes(config, ctypes.byref(classes)) or get_count(config, ctypes.byref(count))
                or get_map(config, mapping, ctypes.sizeof(mapping))
                or get_config(config, registers, ctypes.sizeof(registers))):
            raise RuntimeError("KPEP could not produce the KPC counter mapping")
        if classes.value != 3 or count.value != 8 or len(set(mapping[:len(events)])) != len(events):
            raise RuntimeError("Unexpected M1 counter classes, count, or overlapping event assignments")
        return {
            "cpu_family": f"{family:#x}", "group": group, "counter_classes": classes.value,
            "requested_events": events,
            "configurable_registers": [f"{value:#x}" for value in registers[:count.value]],
            "event_to_counter": dict(zip(events, mapping[:len(events)])),
            "hardware_access_verified": False,
        }
    finally:
        if config.value:
            free_config(config)
        if database.value:
            free_db(database)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=EVENT_GROUPS, default="controls")
    parser.add_argument("--swap", nargs=2, metavar=("FIRST", "SECOND"),
                        help="test one requested event-order permutation without changing the hardware")
    args = parser.parse_args()
    try:
        result = plan(args.group, args.swap)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
