"""Git-backed snapshots. Two users: the exploration guard (§6: explorations must leave the
working tree as they found it) and rewind (per-node file checkpoints, Claude-Code style:
only files the agent touched are ever restored).

Snapshots are plain git tree objects written through a private index (`.kaipi/index`), so
the user's index, stash and branches are never touched. Each completed node's tree is
pinned under `refs/kaipi/<session>/<node>` so gc keeps it. All paths are repo-root
relative, whatever subdirectory kaipi runs in."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

WARNING = (
    "<kaipi:guard>WARNING: the working tree changed during an exploration branch. "
    "Explorations are read-only; the user will be asked to revert.</kaipi:guard>"
)


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "GIT_INDEX_FILE": str((cwd / ".kaipi" / "index").resolve())}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False, env=env)


def _git(cwd: Path, *args: str) -> str:
    r = _run(cwd, *args)
    return r.stdout.decode() if r.returncode == 0 else ""


def root(cwd: Path) -> Path | None:
    top = _git(cwd, "rev-parse", "--show-toplevel").strip()
    return Path(top) if top else None


def is_repo(cwd: Path) -> bool:
    return root(cwd) is not None


def head(cwd: Path) -> str:
    """Commit HEAD points at; part of the exploration baseline so commits are noticed."""
    return _git(cwd, "rev-parse", "HEAD").strip()


def snapshot(cwd: Path) -> str | None:
    """Tree hash of the whole working tree (untracked included, .gitignore respected)."""
    top = root(cwd)
    if top is None:
        return None
    from kaipi.store import kaipi_dir

    kaipi_dir(cwd)
    skip = (cwd.resolve() / ".kaipi").relative_to(top.resolve()).as_posix()
    _git(cwd, "add", "-A", "--", f":(top,exclude){skip}", ":(top)")
    return _git(cwd, "write-tree").strip() or None


def changed(cwd: Path, a: str | None, b: str | None) -> list[str]:
    """Repo-root-relative paths that differ between two trees."""
    if not a or not b or a == b:
        return []
    return sorted(p for p in _git(cwd, "diff-tree", "-r", "--name-only", a, b).split("\n") if p)


def keep(cwd: Path, name: str, tree: str) -> None:
    """Pin a tree under refs/kaipi/<name> so `git gc` never prunes it."""
    commit = _git(cwd, "commit-tree", tree, "-m", f"kaipi snapshot {name}").strip()
    if commit:
        _git(cwd, "update-ref", f"refs/kaipi/{name}", commit)


def drop(cwd: Path, name: str) -> None:
    _git(cwd, "update-ref", "-d", f"refs/kaipi/{name}")


def restore(cwd: Path, tree: str, paths: list[str]) -> None:
    """Write `paths` back to their content in `tree`; a path absent there is deleted.
    Files outside `paths` are never touched. Uses `git show`, so the user's index stays."""
    top = root(cwd)
    if top is None or _git(cwd, "cat-file", "-t", tree).strip() != "tree":
        raise ValueError(f"snapshot {tree[:8]} is missing from this repository")
    for p in paths:
        target = top / p
        entry = _git(cwd, "ls-tree", "--full-tree", tree, "--", p).split()
        if len(entry) >= 2 and entry[1] == "blob":
            blob = _run(cwd, "show", f"{tree}:{p}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob.stdout)
            if entry[0] == "100755":
                target.chmod(target.stat().st_mode | 0o111)
        else:
            target.unlink(missing_ok=True)


def reset(cwd: Path) -> None:
    """The one-shot `git checkout -- . && git clean -fd`; caller must confirm first."""
    subprocess.run(["git", "checkout", "--", "."], cwd=cwd, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=cwd, check=False)
