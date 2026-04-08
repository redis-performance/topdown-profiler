"""Tests for export utilities."""

import json
import csv
import io
from dataclasses import dataclass

from topdown.output.export import export_json, export_csv


class TestExportJson:
    def test_dict(self):
        result = export_json({"key": "value"})
        data = json.loads(result)
        assert data["key"] == "value"

    def test_list(self):
        result = export_json([1, 2, 3])
        data = json.loads(result)
        assert data == [1, 2, 3]

    def test_pretty(self):
        result = export_json({"a": 1}, pretty=True)
        assert "\n" in result

    def test_compact(self):
        result = export_json({"a": 1}, pretty=False)
        assert "\n" not in result

    def test_dataclass_with_to_dict(self):
        @dataclass
        class Dummy:
            x: int = 1
            def to_dict(self):
                return {"x": self.x}

        result = export_json(Dummy())
        data = json.loads(result)
        assert data["x"] == 1


class TestExportCsv:
    def test_basic(self):
        data = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        result = export_csv(data)
        reader = csv.DictReader(io.StringIO(result))
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["a"] == "1"

    def test_empty(self):
        assert export_csv([]) == ""

    def test_custom_fieldnames(self):
        data = [{"a": 1, "b": 2, "c": 3}]
        result = export_csv(data, fieldnames=["a", "b"])
        assert "c" not in result.split("\n")[0]
