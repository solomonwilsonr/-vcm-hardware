"""Every capture folder is a regression test; adding one needs no code."""
import json
import shutil
from pathlib import Path

import pytest

from vcm_cli.main import main
from vcm_parsers import captures

ROOT = Path(__file__).resolve().parent.parent
OBS = ROOT / "examples" / "gpu-node" / "observed"
FOLDERS = captures.discover(ROOT / "tests" / "captures", OBS)


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_capture_folder(folder):
    assert captures.check(folder) == []


def test_the_synthetic_sample_is_discovered_and_marked():
    assert OBS in FOLDERS
    assert json.loads((OBS / "expect.json").read_text())["synthetic"] is True


@pytest.fixture
def scrubbed(tmp_path, capsys):
    out = tmp_path / "acme-sys-1"
    code = main(["inventory", "sanitize", "--redfish", str(OBS / "redfish-capture.json"),
                 "--nvidia-smi", str(OBS / "nvidia-smi.csv"), "--out", str(out)])
    capsys.readouterr()
    assert code == 0
    return out


def test_sanitize_writes_a_sealed_folder_that_passes(scrubbed):
    assert sorted(p.name for p in scrubbed.iterdir()) == ["expect.json", "nvidia-smi.csv",
                                                           "redfish.json"]
    exp = json.loads((scrubbed / "expect.json").read_text())
    assert set(exp["sanitized"]["sha256"]) == {"nvidia-smi.csv", "redfish.json"}
    assert exp["components"]["gpu"] == 8 and exp["allow_problems"] == []
    assert captures.check(scrubbed) == []


def test_a_raw_capture_pasted_in_is_refused(tmp_path):
    folder = tmp_path / "raw"
    folder.mkdir()
    shutil.copy(OBS / "redfish-capture.json", folder / "redfish.json")
    (folder / "expect.json").write_text(json.dumps({"schema": "vcm-capture-expect/1"}))
    assert any("not sealed" in p for p in captures.check(folder))


def test_editing_after_scrubbing_needs_a_new_seal(scrubbed, capsys):
    target = scrubbed / "nvidia-smi.csv"
    target.write_text(target.read_text() + "\n")
    assert any("changed since it was sealed" in p for p in captures.check(scrubbed))
    (scrubbed / "extra.json").write_text("{}")
    assert any("not in the seal" in p for p in captures.check(scrubbed))
    (scrubbed / "extra.json").unlink()
    assert main(["inventory", "seal", str(scrubbed)]) == 0
    capsys.readouterr()
    assert captures.check(scrubbed) == []


def test_sealing_does_not_excuse_sensitive_content(scrubbed, capsys):
    f = scrubbed / "redfish.json"
    cap = json.loads(f.read_text())
    cap["resources"]["/redfish/v1/x"] = {
        "@odata.type": "#Thing.v1.Thing", "Contact": "jane@corp.example.com",
        "Password": "hunter2", "Url": "https://root:pw@bmc.internal/redfish"}
    f.write_text(json.dumps(cap))
    main(["inventory", "seal", str(scrubbed)])
    capsys.readouterr()
    problems = " ".join(captures.check(scrubbed))
    assert "e-mail address" in problems and "carries credentials" in problems
    assert "'Password'" in problems


def test_a_capture_the_parser_cannot_make_sense_of_fails_the_check(scrubbed, capsys):
    f = scrubbed / "redfish.json"
    cap = json.loads(f.read_text())
    cap["resources"]["/redfish/v1/Systems/1/Memory"] = {"_error": "HTTP 500"}
    f.write_text(json.dumps(cap))
    main(["inventory", "seal", str(scrubbed)])
    capsys.readouterr()
    assert any("could not be fetched" in p for p in captures.check(scrubbed))


def test_cli_doctor_check_and_exit_codes(scrubbed, tmp_path, capsys):
    red, csv = str(scrubbed / "redfish.json"), str(scrubbed / "nvidia-smi.csv")
    assert main(["inventory", "doctor", "--redfish", red, "--nvidia-smi", csv]) == 0
    assert "doctor: OK" in capsys.readouterr().out
    bad = tmp_path / "empty.json"
    bad.write_text("{}")
    assert main(["inventory", "doctor", "--redfish", str(bad)]) == 1
    assert main(["inventory", "check", str(scrubbed)]) == 0
    assert main(["inventory", "check", str(tmp_path)]) == 1
