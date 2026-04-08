"""JSON and CSV export utilities."""

import csv
import io
import json


def export_json(data, pretty: bool = True) -> str:
    """Export data as JSON string."""
    # Handle dataclasses and custom objects
    def default(obj):
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        if hasattr(obj, "__dataclass_fields__"):
            from dataclasses import asdict
            return asdict(obj)
        return str(obj)

    return json.dumps(data, default=default, indent=2 if pretty else None)


def export_csv(data: list[dict], fieldnames: list[str] | None = None) -> str:
    """Export list of dicts as CSV string."""
    if not data:
        return ""

    if fieldnames is None:
        fieldnames = list(data[0].keys())

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(data)
    return output.getvalue()
