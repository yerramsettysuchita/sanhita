"""Build the submission archive, without the things that must not leave here.

`git` already knows what belongs in this repository, and `.dockerignore`
already keeps accounts and uploads out of the image. Neither of them governs a
zip made by right-clicking the folder, and that is how the submission was being
built: with `__pycache__`, `.pytest_cache`, a development store carrying a real
personal email address and three throwaway accounts, an assessment recorded
before actor hardening whose actor reads `unattributed`, and whatever workspaces
had been uploaded while testing.

None of that is wrong on a development machine. All of it is wrong in a file
handed to a competition.

    python make-submission.py

Writes `dist/sanhita-submission-<date>.zip` and prints exactly what it kept out
and why. It reads the working tree and never modifies it.

**What it deliberately keeps.** The corpus circular and `rules.json`, because a
clone that boots to an empty screen demonstrates nothing, and the 183
certifications are the artifact the whole product is a view of. Both are
SEBI's own published text and this project's own compiled output.

**What it deliberately drops.** Anything that is one person's rather than the
project's: accounts, uploaded documents, a firm's filing records, assessment
history. A reviewer should build the demonstration state with `sanhita
demo-seed`, which generates it from nothing in about a second.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

#: Dropped, with the reason printed beside each one. The reasons are the point:
#: a bare exclusion list rots because nobody remembers why a line is there.
EXCLUDE: tuple[tuple[str, str], ...] = (
    ("**/__pycache__/**", "Python bytecode, regenerated on import"),
    ("**/*.pyc", "Python bytecode, regenerated on import"),
    ("**/*.pyo", "Python bytecode, regenerated on import"),
    (".pytest_cache/**", "test runner cache"),
    (".ruff_cache/**", "linter cache"),
    (".mypy_cache/**", "type checker cache"),
    (".git/**", "version control history"),
    (".venv/**", "a virtual environment is one machine's"),
    ("venv/**", "a virtual environment is one machine's"),
    ("dist/**", "previous submission archives"),
    ("build/**", "build output, regenerated"),
    ("**/*.egg-info/**", "packaging metadata"),
    (".coverage", "coverage data from one run"),
    ("htmlcov/**", "coverage report"),
    # The four that actually matter.
    (".sanhita/users.json", "accounts, including password hashes and a real address"),
    (".sanhita/workspaces/**", "circulars somebody uploaded on this machine"),
    (".sanhita/company.json", "a firm's own profile"),
    (".sanhita/evidence.json", "a firm's own filing records"),
    (".sanhita/review.json", "a firm's own documents awaiting review"),
    (".sanhita/assessments.json", "a firm's own assessment history"),
    (".sanhita/remediation.json", "a firm's own remediation tasks"),
    (".sanhita/controls.json", "a firm's own control bindings"),
    (".sanhita/plans.json", "a firm's own approvals"),
    (".sanhita/backup-*/**", "a backup the demo seeder moved aside"),
    # Scoped copies of all of the above, on a shared deployment.
    (".sanhita/*.*.json", "per-visitor copies of a firm's own data"),
    ("**/*.log", "run logs from one machine"),
    ("**/.DS_Store", "macOS directory metadata"),
    ("**/Thumbs.db", "Windows thumbnail cache"),
)

#: Kept on purpose, and worth saying so: these look like the things above.
KEEP_ANYWAY: tuple[tuple[str, str], ...] = (
    (".sanhita/rules.json", "the compiled rulebook and its 183 certifications"),
    (
        "corpus/stock-brokers-master-circular-2025-06-17.pdf",
        "SEBI's own published circular, which the whole demonstration is of",
    ),
)


def _excluded(relative: str) -> str:
    """The reason this path is dropped, or an empty string."""
    posix = relative.replace("\\", "/")
    for keep, _ in KEEP_ANYWAY:
        if posix == keep:
            return ""
    for pattern, reason in EXCLUDE:
        if fnmatch.fnmatch(posix, pattern) or fnmatch.fnmatch(posix, pattern.rstrip("/*")):
            return reason
        # `a/**` should also match `a/b/c`, which fnmatch does not do alone.
        if pattern.endswith("/**") and posix.startswith(pattern[:-3] + "/"):
            return reason
    return ""


def build(destination: Path | None = None) -> Path:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = destination or ROOT / "dist" / f"sanhita-submission-{stamp}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)

    kept, dropped, reasons = 0, 0, {}
    total_bytes = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or path == target:
                continue
            relative = path.relative_to(ROOT).as_posix()
            reason = _excluded(relative)
            if reason:
                dropped += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            archive.write(path, arcname=f"sanhita/{relative}")
            kept += 1
            total_bytes += path.stat().st_size

    rule = "-" * 74
    print(rule)
    print("  SANHITA SUBMISSION ARCHIVE")
    print(rule)
    # An output path outside the project has no relative form, and printing
    # it crashed the report after the archive had already been written: the
    # work succeeded and the command looked like it failed.
    try:
        shown = target.relative_to(ROOT)
    except ValueError:
        shown = target
    print(f"  wrote            {shown}")
    print(f"  files included   {kept:,}  ({total_bytes / 1e6:.1f} MB uncompressed)")
    print(f"  files excluded   {dropped:,}")
    print()
    print("  LEFT OUT")
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"    {str(count).rjust(5)}  {reason}")
    print()
    print("  KEPT ON PURPOSE")
    for kept_path, why in KEEP_ANYWAY:
        exists = "" if (ROOT / kept_path).is_file() else "   (not present)"
        print(f"           {kept_path}{exists}")
        print(f"           {' ' * len(kept_path)}  {why}")
    print()
    print("  The archive carries no account, no uploaded document and no firm's")
    print("  own records. Build the demonstration state with:")
    print()
    print("      sanhita demo-seed")
    print(rule)
    return target


if __name__ == "__main__":
    out = build(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    sys.exit(0 if out.is_file() else 1)
