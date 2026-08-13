"""Signing in must not lose the work you did before you signed in.

On a shared deployment a firm's own data is kept per visitor, so that one
person's filing register, with their firm's name on it, is never served to the
next visitor. An anonymous visitor is keyed to an opaque cookie; a signed-in
one is keyed to their account.

That is correct and it had a hole in the middle of it. A visitor could walk the
whole journey anonymously, create a firm, upload a register, run an assessment
and raise a task, then sign up, and the scope would change from the cookie
token to the account. Nothing moved the files. The screens would come back and
say the firm had never existed.

Nobody loses data here. The anonymous files are still on disk under the cookie
scope, so a reader looking afterwards would find them intact and conclude the
system worked. The user, who cannot see the disk, has watched their company
disappear at the exact moment they committed to the product.

**What this does not do.** It never overwrites an account's existing records.
If somebody signs in to an account that already has a firm, the anonymous
workspace is left where it is and the screens say so. Two compliance histories
silently merged into one is a worse outcome than one visibly not merged: an
assessment is a statement about a specific set of records, and interleaving two
sets makes every earlier assessment a statement about something that never
existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Adoption", "SIDECARS", "adopt_visitor_data"]

#: Every file that holds one firm's own data rather than the regulator's.
#: `company.json` sits at the store root; the rest sit beside each workspace's
#: `rules.json`. Adding a sidecar anywhere in the app means adding it here, and
#: the test suite asserts the two lists agree.
SIDECARS = (
    "company.json",
    "evidence.json",
    "review.json",
    "assessments.json",
    "remediation.json",
    "controls.json",
    "plans.json",
)


@dataclass
class Adoption:
    """What moving one visitor's data into an account actually did."""

    moved: list[str] = field(default_factory=list)
    #: Files left where they were because the account already had one. Their
    #: presence is why the screens must say the anonymous work was not merged.
    kept_back: list[str] = field(default_factory=list)

    @property
    def anything_moved(self) -> bool:
        return bool(self.moved)

    @property
    def collided(self) -> bool:
        return bool(self.kept_back)

    def describe(self) -> str:
        if self.collided:
            return (
                f"{len(self.kept_back)} record(s) from the anonymous session were "
                "not merged, because this account already has its own. Signing "
                "out and back in does not lose them; they stay in the anonymous "
                "session they were made in."
            )
        if self.moved:
            return f"{len(self.moved)} record(s) carried over from before you signed in."
        return ""


def _scoped(path: Path, scope: str) -> Path:
    stem, _, suffix = path.name.rpartition(".")
    return path.with_name(f"{stem}.{scope}.{suffix}")


def adopt_visitor_data(
    *, roots, visitor_scope: str, user_scope: str
) -> Adoption:
    """Move an anonymous visitor's firm data into their new account.

    ``roots`` are the directories that hold scoped sidecars: the store root,
    which holds ``company.json``, and each workspace folder, which holds the
    rest. Passing them in rather than discovering them keeps this function
    free of any knowledge of how workspaces are laid out.

    Both scopes are required and must differ. Calling this off a shared
    deployment, where scopes are empty, is a no-op rather than an error: there
    is one set of files and it already belongs to whoever is at the keyboard.
    """
    result = Adoption()
    if not visitor_scope or not user_scope or visitor_scope == user_scope:
        return result

    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for name in SIDECARS:
            source = _scoped(root / name, visitor_scope)
            if not source.is_file():
                continue
            target = _scoped(root / name, user_scope)
            if target.exists():
                # The account brought its own history. Leave both intact and
                # let the screen say the anonymous one was not merged.
                result.kept_back.append(source.name)
                continue
            try:
                source.replace(target)
            except OSError:  # pragma: no cover - a locked file is not worth a 500
                result.kept_back.append(source.name)
                continue
            result.moved.append(target.name)

    return result
