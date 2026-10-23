"""Create a copy of an Instruments Metal template with a selected counter profile."""

import argparse
import plistlib
from pathlib import Path


PROFILE_NAMES = {3: "Performance Limiters", 4: "Performance Utilizations"}
COUNTER_KEYS = {"counterprofile", "counterprofileinternal"}


def select_profile(source: bytes, profile: int) -> bytes:
    if profile not in PROFILE_NAMES:
        raise ValueError(f"unsupported profile {profile}; choose 3 or 4")
    archive = plistlib.loads(source)
    if not isinstance(archive, dict) or not isinstance(archive.get("$objects"), list):
        raise ValueError("template is not an NSKeyedArchiver object table")
    objects = archive["$objects"]
    matches = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        keys = item.get("NS.keys")
        values = item.get("NS.objects")
        if not isinstance(keys, list) or not isinstance(values, list) or len(keys) != len(values):
            continue
        decoded = []
        for key in keys:
            if not isinstance(key, plistlib.UID) or key.data >= len(objects):
                break
            decoded.append(objects[key.data])
        else:
            if COUNTER_KEYS.issubset(decoded):
                matches.append((item, decoded))
    if len(matches) != 1:
        raise ValueError(f"expected one counter profile setting, found {len(matches)}")
    setting, keys = matches[0]
    for index, key in enumerate(keys):
        if key in COUNTER_KEYS:
            old = setting["NS.objects"][index]
            if not isinstance(old, plistlib.UID) or old.data >= len(objects) or objects[old.data] != 0:
                raise ValueError(f"unexpected default for {key}; inspect this Xcode template")
    selected = plistlib.UID(len(objects))
    objects.append(profile)
    for index, key in enumerate(keys):
        if key in COUNTER_KEYS:
            setting["NS.objects"][index] = selected
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Xcode's Metal System Trace.tracetemplate")
    parser.add_argument("output", type=Path, help="new template path")
    parser.add_argument("--profile", type=int, choices=sorted(PROFILE_NAMES), required=True)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error("output must differ from source")
    try:
        result = select_profile(args.source.read_bytes(), args.profile)
        with args.output.open("xb") as output:
            output.write(result)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"created {args.output} with candidate profile {PROFILE_NAMES[args.profile]}")


if __name__ == "__main__":
    main()
