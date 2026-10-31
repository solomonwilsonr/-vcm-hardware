"""VCM core: object store, snapshots, repository."""
from .audit import GENESIS, AuditLog
from .categories import CATEGORIES, categorize
from .model import Artifact, Revision, canonical_json
from .objects import ObjectStore, sha256_bytes, sha256_file
from .records import SHARED_KINDS, FileRecordStore, RecordError, RecordStore, check_name
from .repo import NothingToCommit, Repository, VcmError, now_iso

__version__ = "0.1.0"
__all__ = ["AuditLog", "GENESIS", "CATEGORIES", "categorize", "Artifact", "Revision", "canonical_json", "ObjectStore",
           "sha256_bytes", "sha256_file", "Repository", "VcmError", "NothingToCommit", "now_iso",
           "RecordStore", "FileRecordStore", "RecordError", "SHARED_KINDS", "check_name"]
