import importlib.util
import plistlib
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "apple_gpu" / "make_counter_template.py"
SPEC = importlib.util.spec_from_file_location("make_counter_template", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def template_bytes(duplicate=False):
    objects = ["$null", "counterprofile", "counterprofileinternal", "other", 0, "keep"]
    setting = {
        "NS.keys": [plistlib.UID(1), plistlib.UID(2), plistlib.UID(3)],
        "NS.objects": [plistlib.UID(4), plistlib.UID(4), plistlib.UID(5)],
    }
    objects.append(setting)
    if duplicate:
        objects.append(dict(setting))
    return plistlib.dumps({"$objects": objects}, fmt=plistlib.FMT_BINARY)


@pytest.mark.parametrize("profile", [3, 4])
def test_select_profile_updates_both_settings(profile):
    source = template_bytes()
    result = MODULE.select_profile(source, profile)
    assert source != result
    objects = plistlib.loads(result)["$objects"]
    setting = objects[6]
    assert [objects[value.data] for value in setting["NS.objects"]] == [profile, profile, "keep"]


def test_select_profile_rejects_ambiguous_template():
    with pytest.raises(ValueError, match="expected one"):
        MODULE.select_profile(template_bytes(duplicate=True), 3)


def test_select_profile_rejects_changed_default():
    archive = plistlib.loads(template_bytes())
    archive["$objects"][4] = 1
    with pytest.raises(ValueError, match="unexpected default"):
        MODULE.select_profile(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY), 3)


def test_select_profile_rejects_unknown_profile():
    with pytest.raises(ValueError, match="unsupported profile"):
        MODULE.select_profile(template_bytes(), 5)
