"""Exploration guard (§6): convention, not sandbox. Explorations must leave the git
working tree exactly as they found it."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from pydantic import BaseModel


def h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return r.stdout if r.returncode == 0 else ""


class Baseline(BaseModel):
    head: str
    status: str  # sha256 of `git status --porcelain -z` (covers untracked files)
    diff: str  # sha256 of `git diff HEAD` (covers staged + unstaged content)

    @classmethod
    def snapshot(cls, cwd: Path) -> Baseline:
        return cls(
            head=_git(cwd, "rev-parse", "HEAD").strip(),
            status=h(_git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")),
            diff=h(_git(cwd, "diff", "HEAD")),
        )

    def clean(self, cwd: Path) -> bool:
        return Baseline.snapshot(cwd) == self


def is_repo(cwd: Path) -> bool:
    return _git(cwd, "rev-parse", "--is-inside-work-tree").strip() == "true"


def reset(cwd: Path) -> None:
    """The one-shot `git checkout -- . && git clean -fd`; caller must confirm first."""
    subprocess.run(["git", "checkout", "--", "."], cwd=cwd, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=cwd, check=False)


WARNING = (
    "<kaipi:guard>WARNING: the working tree changed during an exploration branch. "
    "Explorations are read-only; the user will be asked to revert.</kaipi:guard>"
)
