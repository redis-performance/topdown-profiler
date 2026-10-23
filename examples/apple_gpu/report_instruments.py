"""Report sampled M1 Instruments GPU counters with their capture coverage."""

import argparse
import json
import math
from pathlib import Path
import re


CAPTURE_HASHES = ("source_sha256", "binary_sha256", "template_sha256", "toc_sha256",
                  "counter_xml_sha256", "gpu_xml_sha256", "probe_json_sha256")


def _fraction(value, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"Invalid {label} in M1 GPU profile")
    return value


def _sampled_time(metric: dict, name: str) -> int:
    duration = metric.get("sampled_time_ns")
    if type(duration) is not int or duration <= 0:
        raise ValueError(f"Invalid sampled time for {name}")
    return duration


def report(profile: dict) -> dict:
    if (not isinstance(profile, dict) or profile.get("schema") != "metal-gpu-counter-intervals"
            or profile.get("counter_scope") != "device-wide"):
        raise ValueError("Expected an M1 Instruments device-wide GPU counter profile")
    capture = profile.get("capture")
    coverage = profile.get("probe_coverage")
    means = profile.get("sampled_counter_means")
    clean = profile.get("sampled_counter_means_without_observed_other_activity")
    if (not isinstance(capture, dict) or not isinstance(coverage, dict) or not isinstance(means, dict)
            or not means or not isinstance(clean, dict) or capture.get("checksum_verified") is not True
            or type(capture.get("target_exit_status")) is not int or capture["target_exit_status"] != 0
            or coverage.get("status") != "sampled"):
        raise ValueError("M1 GPU profile lacks a completed, sampled, checksum-verified capture")
    source = capture.get("probe_source")
    mode = capture.get("mode")
    items = capture.get("items")
    if (source not in ("m1", "packaged")
            or mode not in (("alu", "stream") if source == "m1" else ("compute", "alu", "memory", "stream"))
            or capture.get("counter_profile") not in ("Performance Limiters", "Performance Utilizations")
            or (source == "m1" and items is not None)
            or (source == "packaged" and (type(items) is not int or not 1 <= items <= 16777216
                or (mode == "stream" and items % 64)))):
        raise ValueError("Invalid M1 GPU capture configuration")
    process_verified = capture.get("probe_process_verified")
    if process_verified is not None and type(process_verified) is not bool:
        raise ValueError("Invalid M1 GPU probe process verification")
    selected_pid = coverage.get("selected_pid")
    if selected_pid is not None and (type(selected_pid) is not int or selected_pid <= 0):
        raise ValueError("Invalid M1 GPU selected probe PID")
    if process_verified is True and selected_pid is None:
        raise ValueError("Verified M1 GPU probe process lacks a selected PID")
    hashes = {key: capture[key] for key in CAPTURE_HASHES if key in capture}
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
           for value in hashes.values()):
        raise ValueError("Invalid M1 GPU capture hash")
    passes = capture.get("passes")
    observed = coverage.get("passes_observed")
    sampled = coverage.get("passes_with_counter_samples")
    if (type(passes) is not int or passes <= 0 or type(observed) is not int
            or type(sampled) is not int or not 0 <= sampled <= observed <= passes):
        raise ValueError("Invalid M1 GPU pass coverage")
    if passes > 500 or (source == "packaged" and passes * items > 268435456):
        raise ValueError("Invalid M1 GPU capture configuration")
    sparse = coverage.get("passes_below_10_percent_sampled")
    if type(sparse) is not int or not 0 <= sparse <= observed:
        raise ValueError("Invalid M1 GPU sparse-pass count")
    sampled_fraction = _fraction(coverage.get("sampled_gpu_work_fraction"), "sampled GPU work fraction")
    minimum = _fraction(coverage.get("sampled_pass_fraction_min"), "minimum pass sample fraction")
    other = coverage.get("observed_other_activity_fraction_of_sampled_gpu_work")
    if other is not None:
        other = _fraction(other, "observed other GPU activity fraction")

    def check_distributions(distributions, metric_means, original=None):
        if not isinstance(distributions, dict):
            raise ValueError("Invalid M1 GPU per-pass metric distributions")
        for name, stats in distributions.items():
            metric = metric_means.get(name)
            max_passes = original[name]["sampled_passes"] if original is not None and name in original else sampled
            if (not isinstance(metric, dict) or not isinstance(stats, dict)
                    or (original is not None and name not in original)
                    or type(stats.get("sampled_passes")) is not int
                    or not 1 <= stats["sampled_passes"] <= max_passes
                    or stats.get("unit") != metric.get("unit")):
                raise ValueError(f"Invalid M1 GPU per-pass distribution for {name}")
            values = [stats.get(key) for key in ("minimum", "p10", "median", "p90", "maximum")]
            if (any(type(value) not in (int, float) or not math.isfinite(value) for value in values)
                    or values != sorted(values)):
                raise ValueError(f"Invalid M1 GPU per-pass distribution for {name}")

    distributions = profile.get("sampled_per_pass_metric_distributions", {})
    check_distributions(distributions, means)
    clean_distributions = profile.get("sampled_per_pass_metric_distributions_without_observed_other_activity")
    if clean_distributions is not None:
        check_distributions(clean_distributions, clean, distributions)

    ranked = []
    for name, metric in means.items():
        if not name.endswith((" Limiter", " Utilization")) or name == "Top Performance Limiter":
            continue
        if not isinstance(metric, dict) or metric.get("unit") != "%":
            raise ValueError(f"Invalid M1 GPU percentage metric {name}")
        value = metric.get("value")
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid M1 GPU metric value for {name}")
        sampled_time = _sampled_time(metric, name)
        clean_metric = clean.get(name)
        if clean_metric is not None and (not isinstance(clean_metric, dict)
                                         or clean_metric.get("unit") != "%"):
            raise ValueError(f"Invalid clean M1 GPU percentage metric {name}")
        clean_value = clean_metric.get("value") if isinstance(clean_metric, dict) else None
        if clean_value is not None and (type(clean_value) not in (int, float)
                                        or not math.isfinite(clean_value) or clean_value < 0):
            raise ValueError(f"Invalid clean M1 GPU metric value for {name}")
        clean_time = _sampled_time(clean_metric, name) if clean_metric is not None else 0
        if clean_time > sampled_time:
            raise ValueError(f"Clean M1 GPU sampled time exceeds total for {name}")
        ranked.append({"name": name, "sampled_mean_percent": value,
                       "sampled_time_ns": sampled_time,
                       "mean_without_observed_other_activity_percent": clean_value,
                       "clean_sampled_time_fraction": clean_time / sampled_time})
    ranked.sort(key=lambda row: (-row["sampled_mean_percent"], row["name"]))

    bandwidth = {}
    for name in ("GPU Read Bandwidth", "GPU Write Bandwidth"):
        metric = means.get(name)
        if metric is None:
            continue
        if (not isinstance(metric, dict) or metric.get("unit") != "GB/s"
                or type(metric.get("value")) not in (int, float)
                or not math.isfinite(metric["value"]) or metric["value"] < 0):
            raise ValueError(f"Invalid M1 GPU bandwidth metric {name}")
        sampled_time = _sampled_time(metric, name)
        clean_metric = clean.get(name)
        if clean_metric is not None and (not isinstance(clean_metric, dict)
                                         or clean_metric.get("unit") != "GB/s"):
            raise ValueError(f"Invalid clean M1 GPU bandwidth metric {name}")
        clean_value = clean_metric.get("value") if isinstance(clean_metric, dict) else None
        if clean_value is not None and (type(clean_value) not in (int, float)
                                        or not math.isfinite(clean_value) or clean_value < 0):
            raise ValueError(f"Invalid clean M1 GPU bandwidth metric {name}")
        clean_time = _sampled_time(clean_metric, name) if clean_metric is not None else 0
        if clean_time > sampled_time:
            raise ValueError(f"Clean M1 GPU sampled time exceeds total for {name}")
        bandwidth[name] = {"sampled_mean_gb_s": metric["value"],
                           "sampled_time_ns": sampled_time,
                           "mean_without_observed_other_activity_gb_s": clean_value,
                           "clean_sampled_time_fraction": clean_time / sampled_time}

    categories = profile.get("top_limiter_categories", [])
    unparsed = profile.get("top_limiter_unparsed_sampled_time_ns")
    if (not isinstance(categories, list) or type(unparsed) is not int or unparsed < 0):
        raise ValueError("Invalid M1 GPU top-limiter coverage")
    for category in categories:
        if (not isinstance(category, dict) or not isinstance(category.get("name"), str)
                or not category["name"] or type(category.get("sampled_time_ns")) is not int
                or category["sampled_time_ns"] < 0
                or type(category.get("share_of_top_limiter_samples_percent")) not in (int, float)
                or not math.isfinite(category["share_of_top_limiter_samples_percent"])
                or not 0 <= category["share_of_top_limiter_samples_percent"] <= 100):
            raise ValueError("Invalid M1 GPU top-limiter category")
    top_metric = means.get("Top Performance Limiter")
    if (top_metric is None and (categories or unparsed)
            or top_metric is not None and (not isinstance(top_metric, dict)
                or sum(category["sampled_time_ns"] for category in categories) + unparsed
                != _sampled_time(top_metric, "Top Performance Limiter"))):
        raise ValueError("M1 GPU top-limiter sample time does not match its categories")
    parsed_time = sum(category["sampled_time_ns"] for category in categories)
    if (categories != sorted(categories, key=lambda row: (-row["sampled_time_ns"], row["name"]))
            or len({category["name"] for category in categories}) != len(categories)
            or any(category["sampled_time_ns"] <= 0
                   or category["share_of_top_limiter_samples_percent"]
                   != round(100 * category["sampled_time_ns"] / parsed_time, 4)
                   for category in categories)):
        raise ValueError("M1 GPU top-limiter category shares or ordering disagree with sampled time")

    return {
        "measurement_kind": "m1-instruments-device-wide-sampled-gpu",
        "capture": {key: capture.get(key) for key in
                    ("mode", "probe_source", "passes", "items", "counter_profile", "probe_process_verified")}
                   | hashes,
        "coverage": {"selected_pid": selected_pid, "passes_observed": observed, "passes_with_counter_samples": sampled,
                     "sampled_gpu_work_fraction": sampled_fraction,
                     "sampled_pass_fraction_min": minimum,
                     "passes_below_10_percent_sampled": sparse,
                     "observed_other_activity_fraction_of_sampled_gpu_work": other},
        "ranked_sampled_percent_metrics": ranked,
        "sampled_bandwidth": bandwidth,
        "sampled_per_pass_metric_distributions": distributions,
        "sampled_per_pass_metric_distributions_without_observed_other_activity": clean_distributions,
        "top_limiter_categories": categories,
        "top_limiter_unparsed_sampled_time_ns": unparsed,
    }


def format_report(data: dict) -> str:
    capture, coverage = data["capture"], data["coverage"]

    def pass_count(stats):
        count = stats["sampled_passes"]
        return f"{count} {'pass' if count == 1 else 'passes'}"

    lines = [f"M1 sampled GPU: {capture['probe_source']} {capture['mode']}, "
             f"{capture['passes']} passes, {capture['counter_profile']}",
             "Counter scope: device-wide Instruments samples",
             f"Sampled passes: {coverage['passes_with_counter_samples']}/{capture['passes']}; "
             f"sampled GPU work: {coverage['sampled_gpu_work_fraction']:.2%}; "
             f"least-sampled pass: {coverage['sampled_pass_fraction_min']:.2%}"]
    pid_text = f" (PID {coverage['selected_pid']})" if coverage["selected_pid"] is not None else ""
    verification = ("verified against the compiled binary" if capture["probe_process_verified"]
                    else "not independently verified in this profile")
    lines.append(f"Probe process: {verification}{pid_text}")
    other = coverage["observed_other_activity_fraction_of_sampled_gpu_work"]
    if other is not None:
        lines.append(f"Observed other GPU activity within sampled work: {other:.2%}")
    if coverage["passes_below_10_percent_sampled"]:
        lines.append(f"Passes below 10% sampled: {coverage['passes_below_10_percent_sampled']}")
    if data["sampled_per_pass_metric_distributions"]:
        lines.append("Per-pass ranges use unfiltered, overlap-weighted device-wide samples.")
    clean_passes = data["sampled_per_pass_metric_distributions_without_observed_other_activity"] or {}
    if clean_passes:
        lines.append("Clean pass summaries exclude intervals with observed other activity; "
                     "individual pass coverage can be sparse and remains device-wide.")
    for suffix, heading in ((" Limiter", "Highest sampled limiter means"),
                            (" Utilization", "Highest sampled utilization means")):
        metrics = [row for row in data["ranked_sampled_percent_metrics"] if row["name"].endswith(suffix)]
        if not metrics:
            continue
        lines.append(f"{heading} (clean excludes counter intervals with observed other activity):")
        for metric in metrics[:5]:
            clean = metric["mean_without_observed_other_activity_percent"]
            fraction = metric["clean_sampled_time_fraction"]
            clean_text = f"; clean {clean:.2f}% ({fraction:.1%} of sampled time)" if clean is not None else ""
            lines.append(f"  {metric['name']}: {metric['sampled_mean_percent']:.2f}%{clean_text}")
            pass_stats = data["sampled_per_pass_metric_distributions"].get(metric["name"])
            if pass_stats:
                lines.append(f"    sampled pass median {pass_stats['median']:.2f}%; "
                             f"range {pass_stats['minimum']:.2f}–{pass_stats['maximum']:.2f}% "
                             f"({pass_count(pass_stats)})")
            clean_stats = clean_passes.get(metric["name"])
            if clean_stats:
                lines.append(f"    clean sampled pass median {clean_stats['median']:.2f}%; "
                             f"p10–p90 {clean_stats['p10']:.2f}–{clean_stats['p90']:.2f}% "
                             f"({pass_count(clean_stats)})")
    for name, metric in data["sampled_bandwidth"].items():
        clean = metric["mean_without_observed_other_activity_gb_s"]
        fraction = metric["clean_sampled_time_fraction"]
        clean_text = f"; clean {clean:.2f} GB/s ({fraction:.1%} of sampled time)" if clean is not None else ""
        lines.append(f"  {name}: {metric['sampled_mean_gb_s']:.2f} GB/s{clean_text}")
        pass_stats = data["sampled_per_pass_metric_distributions"].get(name)
        if pass_stats:
            lines.append(f"    sampled pass median {pass_stats['median']:.2f} GB/s; "
                         f"range {pass_stats['minimum']:.2f}–{pass_stats['maximum']:.2f} GB/s "
                         f"({pass_count(pass_stats)})")
        clean_stats = clean_passes.get(name)
        if clean_stats:
            lines.append(f"    clean sampled pass median {clean_stats['median']:.2f} GB/s; "
                         f"p10–p90 {clean_stats['p10']:.2f}–{clean_stats['p90']:.2f} GB/s "
                         f"({pass_count(clean_stats)})")
    if data["top_limiter_categories"]:
        winner = data["top_limiter_categories"][0]
        lines.append(f"Most frequent parsed top-limiter label: {winner['name']} "
                     f"({winner['share_of_top_limiter_samples_percent']:.2f}% of parsed label sample time)")
    if data["top_limiter_unparsed_sampled_time_ns"]:
        lines.append(f"Unparsed top-limiter sample time: {data['top_limiter_unparsed_sampled_time_ns']} ns")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path, help="checked M1 runner profile.json")
    parser.add_argument("--json", action="store_true", help="print the compact report as JSON")
    args = parser.parse_args()
    try:
        data = report(json.loads(args.profile.read_text()))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(data, indent=2, allow_nan=False) if args.json else format_report(data))


if __name__ == "__main__":
    main()
