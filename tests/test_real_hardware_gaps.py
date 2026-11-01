"""Gaps found by running VCM against a real NVIDIA GB10 (DGX Spark), 2026-11-01.

Real hardware reports fields as "[N/A]" that every synthetic fixture filled in, and a real
fleet changes firmware without changing a component. Each test below pins behaviour that was
wrong against that machine.
"""
from __future__ import annotations

from vcm_diff.semantic import diff_designs, render_text
from vcm_parsers.inventory import parse_nvidia_smi
from vcm_parsers.model import Design
from vcm_parsers.sanitize import Scrubber

# exactly what the Spark printed, header and all
GB10_CSV = (
    "index, name, serial, pci.bus_id, driver_version, power.draw [W], power.limit [W], "
    "pcie.link.gen.max, pcie.link.width.max\n"
    "0, NVIDIA GB10, [N/A], 0000000F:01:00.0, 580.178.04, 11.36 W, [N/A], 1, 16\n")


def test_placeholder_is_not_learned_as_an_identity():
    """"[N/A]" under `serial` must not become a pseudonym, and must not drag unrelated
    fields with it: the Spark reports the same token for its power limit."""
    sc = Scrubber(salt="test")
    sc.learn("[N/A]", "serial")
    assert sc.literals == {}, "a placeholder was registered as an identifier"
    assert sc.text(GB10_CSV).count("[N/A]") == 2, "placeholders were rewritten"


def test_unreported_fields_are_not_stored_as_values():
    obs = parse_nvidia_smi(GB10_CSV)
    assert obs["components"][0]["serial"] == "", "stored '[N/A]' as a serial number"


def test_doctor_reports_what_the_machine_did_not_answer():
    """The failure this catches: a report saying 'nothing skipped or missing' about a machine
    that answered neither serial nor power limit."""
    rep: dict = {}
    parse_nvidia_smi(GB10_CSV, rep)
    assert set(rep["null_columns"]) == {"serial", "power.limit"}


def _node(driver: str) -> Design:
    # meta["nodes"] is a list: a design may hold more than one node
    return Design(components={},
                  meta={"nodes": [{"node": "spark", "firmware": {"gpu_driver": driver}}]})


def test_firmware_change_is_a_change():
    """A driver bump with no component change reported 'No semantic hardware changes'."""
    d = diff_designs(_node("580.178.04"), _node("590.00.01"))
    assert d["summary"]["firmware_changed"] == 1
    assert d["firmware"] == [{"key": "gpu_driver", "old": "580.178.04", "new": "590.00.01"}]
    assert "No semantic hardware changes" not in render_text(d)
    assert "580.178.04 -> 590.00.01" in render_text(d)


def test_identical_firmware_is_not_a_change():
    assert diff_designs(_node("580.178.04"), _node("580.178.04"))["firmware"] == []
