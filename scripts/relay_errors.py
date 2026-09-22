"""Named protocol errors for the project-continuity object store (v4).

Every code here is part of the documented public contract: readers, the CLI
and the resolver must be able to tell 'the data is known to be corrupt' apart
from 'the check did not finish'.  A budget exhaustion is never corruption and
never a PASS; a missing object is never an 'unverified' check.
"""
from __future__ import annotations

# The document is v4 but the caller asked for a state without a resolver/root.
RELAY_SCHEMA_V4_REQUIRES_RESOLVER = "RELAY_SCHEMA_V4_REQUIRES_RESOLVER"

# Known corruption of a bound object.
RELAY_OBJECT_MISSING = "RELAY_OBJECT_MISSING"
RELAY_OBJECT_HASH_MISMATCH = "RELAY_OBJECT_HASH_MISMATCH"
RELAY_OBJECT_SCHEMA_INVALID = "RELAY_OBJECT_SCHEMA_INVALID"
RELAY_OBJECT_PROJECT_MISMATCH = "RELAY_OBJECT_PROJECT_MISMATCH"
RELAY_INDEX_INVALID = "RELAY_INDEX_INVALID"
RELAY_CHUNK_MISSING = "RELAY_CHUNK_MISSING"
RELAY_REFERENCE_CYCLE = "RELAY_REFERENCE_CYCLE"
RELAY_OBJECT_LIMIT_EXCEEDED = "RELAY_OBJECT_LIMIT_EXCEEDED"
RELAY_OBJECT_PATH_INVALID = "RELAY_OBJECT_PATH_INVALID"
RELAY_OBJECT_TYPE_INVALID = "RELAY_OBJECT_TYPE_INVALID"

# The deep check used its time or I/O budget: the check is INCOMPLETE.
RELAY_VALIDATION_BUDGET_EXCEEDED = "RELAY_VALIDATION_BUDGET_EXCEEDED"

# Correction targets this build cannot bind to a real object.  The code is the
# stable refusal: a syntactically valid digest is never accepted as a verified
# target merely because it looks like a digest.
RELAY_CORRECTION_TARGET_UNSUPPORTED = "RELAY_CORRECTION_TARGET_UNSUPPORTED"
# A supported target type whose object is not inside the traceable range.
RELAY_CORRECTION_TARGET_UNREACHABLE = "RELAY_CORRECTION_TARGET_UNREACHABLE"

# A relationship target claimed by two different corrections in one commit.
RELAY_CORRECTION_TARGET_CONFLICT = "RELAY_CORRECTION_TARGET_CONFLICT"

# A v5 document declares the digest of its externalised Markdown but the bound
# section objects no longer reproduce it: the text was changed, reordered or
# replaced without the document being rewritten.
RELAY_MARKDOWN_DIGEST_MISMATCH = "RELAY_MARKDOWN_DIGEST_MISMATCH"

# The document's acceptance map disagrees with the acceptance conditions the
# tasks actually carry.  The AC identity is a stable function of the task id and
# the condition text, so a mismatch means a recorded coverage relationship no
# longer names the text it was minted for.  It is a binding defect, never a
# silent difference.
RELAY_AC_MAP_MISMATCH = "RELAY_AC_MAP_MISMATCH"

# A pagination cursor minted for a different document revision, content hash,
# schema or filter.  Pages of different identities are never concatenated.
RELAY_PAGE_CURSOR_STALE = "RELAY_PAGE_CURSOR_STALE"

# Pre-write budget refusals.  Both happen before the CURRENT replacement.
RELAY_STORAGE_QUOTA_EXCEEDED = "RELAY_STORAGE_QUOTA_EXCEEDED"
RELAY_DISK_SPACE_INSUFFICIENT = "RELAY_DISK_SPACE_INSUFFICIENT"
RELAY_CURRENT_CAPACITY_EXCEEDED = "RELAY_CURRENT_CAPACITY_EXCEEDED"

# A read-only patch preview was asked for while another writer holds a live
# lease.  The preview never predicts a commit that the lease would reject, and
# it never takes the lease over.
RELAY_PREVIEW_LEASE_CONFLICT = "RELAY_PREVIEW_LEASE_CONFLICT"

# Refusals that belong to the write/validation contract rather than to
# corruption of already-committed bytes.
CORRECTION_TARGET_CODES = frozenset({
    RELAY_CORRECTION_TARGET_UNSUPPORTED,
    RELAY_CORRECTION_TARGET_UNREACHABLE,
})

# Corruption that is known to be corruption (used to classify status).
CORRUPTION_CODES = frozenset({
    RELAY_OBJECT_MISSING,
    RELAY_OBJECT_HASH_MISMATCH,
    RELAY_OBJECT_SCHEMA_INVALID,
    RELAY_OBJECT_PROJECT_MISMATCH,
    RELAY_INDEX_INVALID,
    RELAY_CHUNK_MISSING,
    RELAY_REFERENCE_CYCLE,
    RELAY_OBJECT_LIMIT_EXCEEDED,
    RELAY_OBJECT_PATH_INVALID,
    RELAY_OBJECT_TYPE_INVALID,
    RELAY_AC_MAP_MISMATCH,
})


class RelayError(RuntimeError):
    """A named protocol failure.  The code is stable; detail is bounded.

    Detail text never contains a secret or an arbitrary raw payload: callers
    pass identifiers, digests and counts only.
    """

    def __init__(self, code, detail=""):
        super().__init__(code + (": " + detail if detail else ""))
        self.code = code
        self.detail = detail

    def as_result(self):
        if self.detail:
            return {"error": self.code, "message": self.detail}
        return {"error": self.code}
