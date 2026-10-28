"""Bounded owner-only operator records, shared by funding and provider adapters."""
import os
from pathlib import Path
import stat

OPERATOR = os.environ.get("ROCOTO_OPERATOR_ID", "workspace-owner").strip() or "workspace-owner"

class PrivateRecordError(ValueError):
    """An operator record failed its filesystem ownership checks."""

def read_private(path: Path, *, code_root: Path) -> bytes:
    try:
        path = path.absolute()
        if path != path.resolve() or path.is_relative_to(code_root):
            raise PrivateRecordError("Keep approval in a private directory outside the checkout.")
        directory = path.parent.stat()
        if directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
            raise PrivateRecordError("Approval directory requires owner-only access (0700).")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            details = os.fstat(stream.fileno())
            if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid()
                    or details.st_mode & 0o077 or details.st_nlink != 1):
                raise PrivateRecordError("Approval requires a private, owner-held regular file (0600).")
            data = stream.read(16385)
            if len(data) > 16384:
                raise PrivateRecordError("Approval record exceeds the supported size.")
            return data
    except OSError as exc:
        raise PrivateRecordError("Provide the owner's verified private budget approval file.") from exc

