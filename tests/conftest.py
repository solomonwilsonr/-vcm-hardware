import shutil
from pathlib import Path

import pytest

from vcm_core import Repository

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "sample-pcb"


def _copy_tree(src: Path, dst: Path) -> None:
    for old in list(dst.iterdir()):
        if old.name == ".vcm":
            continue
        shutil.rmtree(old) if old.is_dir() else old.unlink()
    for p in src.rglob("*"):
        if p.is_file():
            (dst / p.relative_to(src)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst / p.relative_to(src))


@pytest.fixture
def repo(tmp_path):
    return Repository.init(tmp_path / "work")


@pytest.fixture
def sample_repo(tmp_path):
    """Repository with the sample project committed as v1.0 and v1.1 (tagged)."""
    r = Repository.init(tmp_path / "hw")
    _copy_tree(SAMPLE / "v1.0", r.root)
    r.commit("v1.0 initial", author="alice", timestamp="2026-01-10T10:00:00Z")
    r.tag("v1.0")
    _copy_tree(SAMPLE / "v1.1", r.root)
    r.commit("v1.1 swap regulator, change output cap", author="bob",
             timestamp="2026-02-10T10:00:00Z")
    r.tag("v1.1")
    return r
