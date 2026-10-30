# Git integration

**Status: not implemented (placeholder).**

VCM is standalone today: it keeps its own `.vcm/` store and does not read or write Git objects. A project can live in a Git repository (`.vcm/` should be ignored there) but the two histories are independent.

Planned: a bridge that maps VCM revisions to Git commits and tags, and optionally keeps large artifacts in the VCM object store instead of Git LFS.
