"""Tag the release train automatically once a stamped version lands on main.

Intended to run in CI on pushes to ``main`` after ``verify.sh`` passes:

1. Read the single release-train version from ``marketplace.json``.
2. If ``vX.Y.Z`` already exists on origin, exit 0 (nothing to do).
3. Otherwise run :func:`validate_release.validate` (manifests, runtime
   versions, CHANGELOG section). Any failure exits non-zero: a bad train
   is never tagged.
4. Create the annotated tag and push it to origin.

Why a PAT: tags pushed with ``GITHUB_TOKEN`` do not trigger new workflow
runs, so pushing with it would silently skip the tag-push release
workflow. Pushing with ``RELEASE_TOKEN`` (a fine-grained PAT with
Contents read+write on this repo) triggers ``release.yml`` normally.
Without ``RELEASE_TOKEN`` set, this exits 0 after logging a skip so
ordinary pushes stay green until the one-time setup is done.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_release as vr  # noqa: E402


def train_version(root: Path = ROOT) -> str:
    """The single version carried by every marketplace entry."""
    data = json.loads(
        (root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    versions = {p["version"] for p in data["plugins"]}
    if len(versions) != 1:
        raise ValueError(f"release train diverged: {sorted(versions)}")
    return next(iter(versions))


def remote_tag_exists(tag: str, remote: str = "origin", cwd: Path = ROOT) -> bool:
    """Whether ``tag`` already exists on the remote (network read-only)."""
    out = subprocess.run(
        ["git", "ls-remote", "--tags", remote, tag],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    return bool(out.stdout.strip())


def release_ready(tag: str, root: Path = ROOT) -> list[str]:
    """Errors blocking a release of ``tag`` (empty means ready to tag)."""
    return vr.validate(tag, root)


def create_and_push_tag(
    tag: str, token: str, remote: str = "origin", cwd: Path = ROOT
) -> None:
    """Create the annotated tag and push it; tolerates a lost race."""
    subprocess.run(
        ["git", "tag", "-a", tag, "-m", tag],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    push = subprocess.run(
        [
            "git",
            "push",
            f"https://x-access-token:{token}@github.com/"
            "bibutikoley/mcps-marketplace.git",
            f"refs/tags/{tag}",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if push.returncode != 0:
        # Another run may have tagged first; only fail if still absent.
        if remote_tag_exists(tag, remote, cwd):
            print(f"tag {tag} appeared concurrently; treating as success")
            return
        raise RuntimeError(f"tag push failed: {push.stderr.strip()[-2000:]}")


def run(root: Path = ROOT, token: str | None = None) -> int:
    if not token:
        print("RELEASE_TOKEN not configured; skipping auto-tag")
        return 0
    try:
        version = train_version(root)
    except (ValueError, FileNotFoundError, KeyError) as e:
        print(f"ERROR: {e}")
        return 1
    tag = f"v{version}"
    if remote_tag_exists(tag, cwd=root):
        print(f"tag {tag} already exists; nothing to do")
        return 0
    errors = release_ready(tag, root)
    if errors:
        print(f"release not ready for {tag}:")
        for e in errors:
            print(f"  - {e}")
        return 1
    create_and_push_tag(tag, token, cwd=root)
    print(f"tagged and pushed {tag}")
    return 0


def main() -> int:
    return run(ROOT, os.environ.get("RELEASE_TOKEN"))


if __name__ == "__main__":
    sys.exit(main())
