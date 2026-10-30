"""Validation: built-in checks, optional kicad-cli wrapper, versioned evidence."""
from .checks import check_bom_text, check_components, check_nets, run_checks
from .evidence import compare_validation, validate_revision
from .kicad_cli import run_kicad_checks

__all__ = ["check_bom_text", "check_components", "check_nets", "run_checks",
           "compare_validation", "validate_revision", "run_kicad_checks"]
