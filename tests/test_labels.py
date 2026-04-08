"""Tests for label system."""

import pytest

from topdown.collector.labels import parse_label_args, merge_labels


class TestParseLabelArgs:
    def test_basic(self):
        labels = parse_label_args(["key1=val1", "key2=val2"])
        assert labels == {"key1": "val1", "key2": "val2"}

    def test_value_with_equals(self):
        labels = parse_label_args(["dsn=postgres://user:pass@host/db"])
        assert labels["dsn"] == "postgres://user:pass@host/db"

    def test_empty(self):
        assert parse_label_args(None) == {}
        assert parse_label_args([]) == {}

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid label"):
            parse_label_args(["no_equals_sign"])

    def test_whitespace_stripped(self):
        labels = parse_label_args([" key = value "])
        assert labels["key"] == "value"


class TestMergeLabels:
    def test_user_overrides_auto(self):
        auto = {"arch": "x86_64", "node": "host1"}
        user = {"arch": "custom_arch", "git_branch": "unstable"}
        merged = merge_labels(auto, user)
        assert merged["arch"] == "custom_arch"
        assert merged["node"] == "host1"
        assert merged["git_branch"] == "unstable"

    def test_empty_user(self):
        auto = {"arch": "x86_64"}
        merged = merge_labels(auto, {})
        assert merged == auto

    def test_empty_auto(self):
        user = {"git_branch": "main"}
        merged = merge_labels({}, user)
        assert merged == user
