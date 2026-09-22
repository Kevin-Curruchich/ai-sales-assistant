import fnmatch
import os
import re
import stat
import subprocess
from pathlib import Path

# Resolved from __file__, not the cwd: with a relative Path, these tests only
# pass when pytest happens to run from the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "entrypoint.sh"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    """Write an executable shell stub named `name` into `bin_dir`."""
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_entrypoint(bin_dir: Path, extra_env: dict) -> subprocess.CompletedProcess:
    """Run entrypoint.sh with `bin_dir` shadowing `alembic`/`hypercorn` on PATH.

    Nothing here talks to a real database: the stub executables are the only
    "alembic" and "hypercorn" the script can find.
    """
    env = os.environ.copy()
    env.pop("GOOGLE_CREDENTIALS_BASE64", None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.update(extra_env)
    return subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_failed_migration_never_reaches_the_server(tmp_path):
    """A migration failure must abort the boot before hypercorn ever runs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "alembic", "exit 1")
    marker = tmp_path / "hypercorn_reached"
    _write_stub(bin_dir, "hypercorn", 'touch "$MARKER_FILE"')

    result = _run_entrypoint(bin_dir, {"MARKER_FILE": str(marker)})

    assert result.returncode != 0, (
        f"expected non-zero exit, got 0\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert not marker.exists(), "hypercorn must never run after a failed migration"


def test_successful_migration_runs_before_the_server(tmp_path):
    """A successful migration must run, and run before hypercorn starts."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "order.log"
    _write_stub(bin_dir, "alembic", 'echo alembic >> "$LOG_FILE"')
    _write_stub(bin_dir, "hypercorn", 'echo hypercorn >> "$LOG_FILE"')

    result = _run_entrypoint(bin_dir, {"LOG_FILE": str(log)})

    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert log.read_text().split() == ["alembic", "hypercorn"]


def test_alembic_is_a_declared_dependency():
    """A comment mentioning alembic would pass a plain substring check without
    pinning anything runnable. Require an actual pin."""
    requirements = REQUIREMENTS.read_text().lower()
    assert re.search(r"^alembic==", requirements, re.MULTILINE), (
        "requirements.txt must pin alembic with alembic==<version>"
    )


def _dockerignore_pattern_excludes(pattern: str, path: str) -> bool:
    """Approximate whether a .dockerignore line would exclude `path`.

    Not a full re-implementation of dockerignore semantics, but it covers
    the forms that matter here: a bare name (`alembic`), a leading anchor
    (`/alembic`), a trailing-slash directory marker (`alembic/`), a
    contents-only wildcard (`alembic/*`), and a `**/`-prefixed pattern
    (`**/alembic`) — the exact spellings that a literal `in ["alembic/",
    "alembic"]` check would miss.
    """
    pattern = pattern.strip()
    if not pattern or pattern.startswith("#") or pattern.startswith("!"):
        return False

    candidates = {pattern}
    if pattern.startswith("**/"):
        candidates.add(pattern[3:])
    if pattern.startswith("/"):
        candidates.add(pattern[1:])
    trimmed = pattern.rstrip("/")
    candidates.add(trimmed)
    candidates.add(trimmed + "/*")
    candidates.add(trimmed + "/**")

    parts = path.split("/")
    path_candidates = {path} | {"/".join(parts[i:]) for i in range(len(parts))}

    return any(
        fnmatch.fnmatch(p, c) for c in candidates for p in path_candidates
    )


def test_dockerignore_does_not_exclude_migrations():
    """Any pattern that would exclude the alembic/ directory, its contents,
    or alembic.ini breaks the container boot (entrypoint.sh runs `alembic
    upgrade head` before serving) — alembic.ini is just as load-bearing as
    the migrations directory and a config missing it would boot-loop
    silently. This checks more than the two literal spellings the original
    test covered.
    """
    lines = DOCKERIGNORE.read_text().splitlines()
    for path in ("alembic", "alembic/env.py", "alembic.ini"):
        for line in lines:
            assert not _dockerignore_pattern_excludes(line, path), (
                f".dockerignore pattern {line!r} would exclude {path!r}"
            )
