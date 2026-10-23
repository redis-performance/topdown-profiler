"""Reject invalid scope and numeric encodings before treating XML as measured data."""

from pathlib import Path

import pytest

from topdown.collector.apple import AppleProfileError, parse_apple_bottlenecks, parse_profile, record_profile
from topdown.collector.apple_trace import parse_trace_profile
from tests.test_apple import xml_table
from tests.test_apple_bottlenecks import metric_xml


@pytest.mark.parametrize("pid,start,end", [(True, 0, None), (42.0, 0, None), ("42", 0, None),
                                          (42, False, None), (42, 0.5, None), (42, float("nan"), None),
                                          (42, None, None), (42, 0, True), (42, 0, 1.5),
                                          (42, 0, float("nan")), (42, 0, "100")])
@pytest.mark.parametrize("parser", ["stack", "guided", "trace", "record"])
def test_scope_requires_integer_pid_and_nanosecond_bounds_before_io(tmp_path, monkeypatch, pid, start, end, parser):
    monkeypatch.setattr("topdown.collector.apple._run", lambda *a: pytest.fail("tool invoked before scope validation"))
    path = tmp_path / "unread.xml"
    with pytest.raises(AppleProfileError, match="PID|window"):
        if parser == "stack":
            parse_profile(path, "time-profile", pid, start, end)
        elif parser == "guided":
            parse_apple_bottlenecks(path, pid, start, end)
        elif parser == "trace":
            parse_trace_profile(path, path, pid, start, end)
        else:
            record_profile(tmp_path / "unused", [str(path)], 1, "time", pid, start, end)
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("field,old", [("time", "<sample-time>100</sample-time>"),
                                      ("tid", "<tid>2</tid>"), ("core", '<core id="c" fmt="CPU 7 (P Core)">7</core>')])
@pytest.mark.parametrize("value", ["18446744073709551616", "1_000", "+1", "١"])
def test_stack_scope_cells_require_native_unsigned_integer_encodings(tmp_path, field, old, value):
    text = xml_table(second_row=False)
    tag = {"time": "sample-time", "tid": "tid", "core": "core"}[field]
    text = text.replace(old, f"<{tag}>{value}</{tag}>")
    path = tmp_path / "invalid.xml"
    path.write_text(text)
    with pytest.raises(AppleProfileError, match="integer|uint64"):
        parse_profile(path, "time-profile", 42)


def test_guided_scope_integers_do_not_accept_values_larger_than_native_uint64(tmp_path):
    text = metric_xml().replace('<start-time>100</start-time>', '<start-time>18446744073709551616</start-time>')
    path = tmp_path / "invalid-guided.xml"
    path.write_text(text)
    with pytest.raises(AppleProfileError):
        parse_apple_bottlenecks(path, 42)


@pytest.mark.parametrize("old,new", [
    ("<engineering-type>thread</engineering-type>", "<engineering-type>unknown</engineering-type>"),
    ("<engineering-type>process</engineering-type>", "<engineering-type>unknown</engineering-type>"),
    ("<engineering-type>core</engineering-type>", "<engineering-type>unknown</engineering-type>"),
    ("<engineering-type>tagged-backtrace</engineering-type>", "<engineering-type>unknown</engineering-type>"),
    ("<sample-time>100</sample-time>", "<duration>100</duration>"),
    ('<core id="c" fmt="CPU 7 (P Core)">7</core>', "<uint64>7</uint64>"),
    ('<process id="p"><pid>42</pid></process>', '<unknown id="p"><pid>42</pid></unknown>'),
])
def test_unknown_native_scope_representations_are_not_interpreted_as_measurements(tmp_path, old, new):
    path = tmp_path / "unknown.xml"
    path.write_text(xml_table(second_row=False).replace(old, new))
    with pytest.raises(AppleProfileError, match="Unknown|unknown"):
        parse_profile(path, "time-profile", 42)


def test_unknown_stack_container_cannot_be_reported_as_a_valid_empty_stack(tmp_path):
    text = xml_table(second_row=False).replace('<tagged-backtrace id="s">', '<unknown id="s">').replace(
        '</tagged-backtrace>', '</unknown>')
    path = tmp_path / "unknown-stack.xml"
    path.write_text(text)
    with pytest.raises(AppleProfileError, match="Unknown|unknown"):
        parse_profile(path, "time-profile", 42)


def test_native_unavailable_stack_sentinel_remains_an_observed_unresolved_sample(tmp_path):
    from xml.etree import ElementTree as ET

    root = ET.fromstring(xml_table(second_row=False))
    stack = root.find('./node/row/tagged-backtrace')
    stack.clear()
    stack.tag = 'sentinel'
    path = tmp_path / 'missing-stack.xml'
    ET.ElementTree(root).write(path)
    profile = parse_profile(path, 'time-profile', 42)
    assert profile['sample_count'] == 1 and profile['samples'][0]['frames'] == []


@pytest.mark.parametrize("schema", ["time-profile", "cpu-pmc-value"])
def test_oversized_integer_text_fails_with_a_profiling_error(tmp_path, schema):
    path = tmp_path / "oversized.xml"
    path.write_text(xml_table(schema, second_row=False).replace('<sample-time>100</sample-time>',
        '<sample-time>' + '9' * 5000 + '</sample-time>'))
    with pytest.raises(AppleProfileError, match="uint64"):
        parse_profile(path, schema, 42)


def test_unknown_guided_cell_types_are_not_interpreted_from_their_text(tmp_path):
    text = metric_xml().replace('<core fmt="CPU 7">7</core>', '<uint64>7</uint64>')
    path = tmp_path / "unknown-guided.xml"
    path.write_text(text)
    with pytest.raises(AppleProfileError, match="Unknown guided"):
        parse_apple_bottlenecks(path, 42)


def test_stack_sentinels_never_become_observed_frames(tmp_path):
    from xml.etree import ElementTree as ET

    root = ET.fromstring(xml_table(second_row=False))
    root.find('./node/row/tagged-backtrace').tag = 'sentinel'
    path = tmp_path / 'unavailable.xml'
    ET.ElementTree(root).write(path)
    result = parse_profile(path, 'time-profile', 42)
    assert result['samples'][0]['frames'] == [] and result['leaf_sample_counts'] == []


@pytest.mark.parametrize("field", ["time_ns", "tid", "core", "weight"])
def test_json_imports_apply_the_same_native_integer_bounds_as_xml(tmp_path, field):
    from topdown.apple_storage import validate_result

    path = tmp_path / "native.xml"
    path.write_text(xml_table(second_row=False))
    result = parse_profile(path, "time-profile", 42)
    result['measurement_kind'] = 'timer-sampled-stacks'
    result['samples'][0][field] = 2**64
    with pytest.raises(AppleProfileError):
        validate_result(result)


@pytest.mark.parametrize("old,new", [
    ("<engineering-type>uint64</engineering-type>", "<engineering-type>integer</engineering-type>"),
    ('<pmc-event-name fmt="FIXED_CYCLES">1</pmc-event-name>', '<string fmt="FIXED_CYCLES">1</string>'),
    ('<uint64>9007199254740993</uint64>', '<weight>9007199254740993</weight>'),
])
def test_unknown_pmc_representations_are_not_accepted_as_event_counts(tmp_path, old, new):
    path = tmp_path / "pmc.xml"
    path.write_text(xml_table("cpu-pmc-value", second_row=False).replace(old, new))
    with pytest.raises(AppleProfileError, match="Unknown"):
        parse_profile(path, "cpu-pmc-value", 42)


@pytest.mark.parametrize("value", ["1_000", "+1000", "١"])
def test_fixed_event_vectors_reject_non_native_integer_encodings(tmp_path, value):
    from topdown.collector.apple import FIXED_EVENTS

    source = (Path(__file__).parent / "test_data/apple_fixed_profile_m6.xml").read_text()
    path = tmp_path / "fixed.xml"
    path.write_text(source.replace('556168 914334', value + ' 914334'))
    with pytest.raises(AppleProfileError, match="fixed-counter integer"):
        parse_profile(path, "counters-profile", 3362, fixed_events=FIXED_EVENTS)
