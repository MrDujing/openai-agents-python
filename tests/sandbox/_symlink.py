from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


def can_create_symlink() -> bool:
    if not hasattr(os, "symlink"):
        return False

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "target.txt"
        link = root / "link.txt"
        target.write_text("target", encoding="utf-8")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            return False
        return link.is_symlink()


requires_symlink = pytest.mark.skipif(
    not can_create_symlink(),
    reason="requires permission to create filesystem symlinks",
)
