"""Report GPU workload costs and overlapping native limiters from a replay profile."""

import argparse
import json
import math
from pathlib import Path
import re

from topdown.collector.apple_files import decode_json_object, read_regular_bytes


def percent(value, allow_above_100=False):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:[.,]\d+)?%", value):
        raise ValueError(f"Unexpected native percentage: {value!r}")
    number = float(value[:-1].replace(",", "."))
    if not math.isfinite(number) or number < 0 or (number > 100 and not allow_above_100):
        raise ValueError(f"Native percentage outside the expected range: {value!r}")
    return number


def _duration(value):
    return isinstance(value, str) and re.fullmatch(r"\d+(?:[.,]\d+)?\s*(?:ns|µs|μs|us|ms|s)", value) is not None


def _timeline_metrics(groups):
    if not isinstance(groups, dict) or any(not isinstance(group, str) for group in groups):
        raise ValueError("Require native GPU timeline counter groups")
    metrics = []
    for group, entry in sorted(groups.items()):
        if (not isinstance(group, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", group)
                or not isinstance(entry, dict) or not isinstance(entry.get("listing"), dict)
                or not isinstance(entry.get("counters"), dict)):
            raise ValueError("Invalid native GPU timeline counter group")
        listing, counters = entry["listing"], entry["counters"]
        children = listing.get("children")
        if (not isinstance(children, list) or type(listing.get("totalCount")) is not int
                or listing["totalCount"] != len(children)
                or any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                       or not re.fullmatch(r"[a-z][a-z0-9_]*", row["name"]) for row in children)
                or len({row["name"] for row in children}) != len(children)
                or set(counters) != {row["name"] for row in children}):
            raise ValueError("Incomplete native GPU timeline counter listing")
        for row in children:
            values, detail = row.get("values"), counters[row["name"]]
            if values is not None and (not isinstance(values, list)
                                      or any(not isinstance(value, dict) or value.get("type") != "string"
                                             or not isinstance(value.get("value"), str) for value in values)):
                raise ValueError("Invalid native GPU timeline counter observation")
            if values:
                if (not isinstance(detail, dict) or not all(isinstance(key, str) and isinstance(value, str)
                                                          for key, value in detail.items())
                        or not detail.get("Name") or not detail.get("Value")
                        or not detail.get("Description") or detail.get("Scope") != "timeline"):
                    raise ValueError("Require native timeline counter units, description and scope")
            elif detail is not None:
                raise ValueError("Unobserved GPU timeline counters require null metadata")
            metrics.append({"group": group, "id": row["name"], "observed": bool(values),
                            "name": detail["Name"] if detail else row["name"],
                            "native_value": detail["Value"] if detail else None,
                            "description": detail["Description"] if detail else None})
    return metrics


def summarize(profile, limit=None, offset=0):
    if (not isinstance(profile, dict) or profile.get("measurement_kind") != "apple-gpu-replay-profile"
            or profile.get("tma_available") is not False or not isinstance(profile.get("timeline"), dict)
            or not _duration(profile["timeline"].get("GPU time"))
            or not isinstance(profile.get("encoders"), list) or not profile["encoders"]):
        raise ValueError("Require a native GPU replay profile.json with measured encoders")
    if (type(offset) is not int or offset < 0
            or (limit is not None and (type(limit) is not int or not 1 <= limit <= 1000))):
        raise ValueError("Require offset >= 0 and limit 1–1000")
    settings, scope = profile.get("settings"), profile.get("scope")
    if (not isinstance(profile.get("device"), str) or not profile["device"].strip()
            or not isinstance(settings, dict) or settings.get("execution") not in ("overlapping", "serial")
            or settings.get("gpu_state") not in ("default", "low", "medium", "high") or settings.get("replayed") is not True
            or not isinstance(scope, dict) or scope.get("kind") != "captured-metal-workload"
            or not isinstance(scope.get("trace_name"), str) or not scope["trace_name"].endswith(".gputrace")
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in profile["timeline"].items())):
        raise ValueError("Require native GPU device, replay settings, capture scope and timeline strings")
    rows = []
    identities = set()
    for row in profile["encoders"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not isinstance(row.get("counters"), dict) or not isinstance(row.get("listing"), dict)):
            raise ValueError("Missing native encoder counters")
        counters = row["counters"]
        if (not re.fullmatch(r"[a-z]+\d+", row["id"]) or row["id"] in identities
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in counters.items())
                or not _duration(counters.get("Duration"))):
            raise ValueError("Require unique native encoder identities, string counters and measured durations")
        identities.add(row["id"])
        listing = row["listing"].get("values")
        if (not isinstance(listing, list) or not listing or not isinstance(listing[0], dict)
                or not isinstance(listing[0].get("value"), str)):
            raise ValueError("Missing native encoder label")
        limiters = [{"name": key, "percent": percent(value, allow_above_100=True), "native_value": value,
                     "exceeds_100": percent(value, allow_above_100=True) > 100}
                    for key, value in counters.items() if key.endswith(" Limiter")]
        limiters.sort(key=lambda entry: (-entry["percent"], entry["name"]))
        label = listing[0]["value"]
        if len(label) >= 2 and label[0] == label[-1] == '"':
            label = label[1:-1]
        rows.append({"id": row["id"], "label": label,
                     "cost_percent": percent(counters.get("Cost")), "duration": counters.get("Duration"),
                     "limiters": limiters, "native_counters": counters})
    rows.sort(key=lambda row: (-row["cost_percent"], row["id"]))
    report = {"measurement_kind": "apple-gpu-replay-bottleneck-report", "device": profile.get("device"),
            "settings": profile.get("settings"), "scope": profile.get("scope"), "timeline": profile["timeline"],
            "encoders": rows[offset:offset + limit] if limit is not None else rows[offset:],
            "total_encoders": len(rows), "offset": offset, "limit": limit,
            "tma_available": False, "limitations": profile.get("limitations", [])}
    if isinstance(profile.get("environment"), dict):
        report["environment"] = profile["environment"]
    if "timeline_counter_groups" in profile:
        report["timeline_metrics"] = _timeline_metrics(profile["timeline_counter_groups"])
        report["timeline_counter_scope"] = {"kind": "gpu-timeline", "target_exclusive": False}
    return report


def render(report):
    lines = [f"Apple GPU replay — {report['device']}",
             f"GPU time: {report['timeline'].get('GPU time', 'unavailable')} | "
             f"Execution: {(report.get('settings') or {}).get('execution', 'unavailable')}",
             f"Cores: {report['timeline'].get('Cores', 'unavailable')} | "
             f"Profiled state: {report['timeline'].get('Profiled state', 'unavailable')}"]
    if "Consistent state" in report["timeline"]:
        lines.append(f"Consistent state: {report['timeline']['Consistent state']}")
    lines.append(f"Encoders: {len(report['encoders'])} of {report['total_encoders']} (offset {report['offset']})")
    if "timeline_metrics" in report:
        observed = [row for row in report["timeline_metrics"] if row["observed"]]
        lines.append(f"Timeline counters: {len(observed)} observed / {len(report['timeline_metrics'])} listed")
        lines.append("Timeline counter scope: GPU timeline; other device activity may contribute.")
        for metric in observed:
            lines.append(f"  {metric['name']}: {metric['native_value']}")
    for row in report["encoders"]:
        lines.append(f"  {row['label']} | cost {row['cost_percent']:.2f}% | {row['duration']}")
        for limiter in row["limiters"]:
            flag = " (native value exceeds 100%)" if limiter["exceeds_100"] else ""
            lines.append(f"    {limiter['name']:<38} {limiter['percent']:6.2f}%{flag}")
        if not row["limiters"]:
            lines.append("    Native limiter data unavailable")
    lines.extend(["Limiters overlap; they do not sum to 100%.",
                  "Cost and duration describe GPU replay, not application wall time or CPU slot TMA."])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    try:
        report = summarize(decode_json_object(read_regular_bytes(args.profile)))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, allow_nan=False) if args.as_json else render(report))


if __name__ == "__main__":
    main()
