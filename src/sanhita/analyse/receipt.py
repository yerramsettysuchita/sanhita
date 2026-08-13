"""A receipt for a compile run.

Determinism is a claim until somebody can check it. This produces the thing they
check with: a small record naming the exact inputs, the exact versions and the
exact outputs of one run, signed so it cannot be edited afterwards.

Anyone holding a receipt and the source PDF can re-run the compiler and compare.
If a single clause hash or the tree fingerprint differs, either the input is not
what the receipt says it is, or the compiler is not behaving deterministically,
and both are worth knowing.

This is build provenance applied to regulation. The idea is borrowed openly from
software supply chain attestation, where the same problem was solved first.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path

from sanhita.ir.canonical import canonical_json
from sanhita.ir.enums import RuleStatus
from sanhita.ir.schema import Obligation

__all__ = ["Receipt", "build_receipt", "verify_receipt"]


@dataclass
class Receipt:
    """What went in, what came out, and what produced it."""

    #: Inputs.
    source_name: str
    source_sha256: str
    source_bytes: int

    #: The parse.
    tree_fingerprint: str
    clauses_parsed: int

    #: The compile.
    engine: str
    ruleset_version: str
    model_id: str | None
    prompt_version: str | None

    #: Outputs.
    rules_total: int
    rules_certified: int
    rulebook_sha256: str
    ledger_head: str

    generated_at: _dt.datetime
    signature: str | None = None

    def payload(self) -> dict:
        """The bytes the signature covers. Order is fixed by canonical JSON."""
        return {
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "source_bytes": self.source_bytes,
            "tree_fingerprint": self.tree_fingerprint,
            "clauses_parsed": self.clauses_parsed,
            "engine": self.engine,
            "ruleset_version": self.ruleset_version,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "rules_total": self.rules_total,
            "rules_certified": self.rules_certified,
            "rulebook_sha256": self.rulebook_sha256,
            "ledger_head": self.ledger_head,
            "generated_at": self.generated_at.isoformat(),
        }

    def to_json(self) -> dict:
        return self.payload() | {"signature": self.signature}

    def how_to_check(self) -> list[str]:
        """Instructions a sceptic can follow without our help."""
        return [
            f"Take the PDF whose SHA-256 is {self.source_sha256[:32]}... and run "
            "`sanhita ingest` against it.",
            f"The tree fingerprint it prints must be {self.tree_fingerprint[:32]}"
            "... If it differs, the document is not the one this run used.",
            f"Run `sanhita compile --engine {self.engine}` and the rulebook's "
            f"SHA-256 must be {self.rulebook_sha256[:32]}...",
            "Run `sanhita receipt --check <this file>` to confirm the signature "
            "over all of the above.",
        ]


def _rulebook_hash(obligations: list[Obligation]) -> str:
    """One hash over every rule, in id order, using the canonical encoding.

    The same encoding a certification signs over, so the receipt and the
    signatures agree about what a rule is.
    """
    digest = hashlib.sha256()
    for o in sorted(obligations, key=lambda o: o.id):
        digest.update(canonical_json(o.model_dump(mode="python")).encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def _sign(payload: dict, key: str) -> str:
    body = canonical_json(payload).encode("utf-8")
    return hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()


def build_receipt(
    *,
    pdf: Path,
    tree,
    obligations: list[Obligation],
    ledger_head: str,
    engine: str = "rules",
    ruleset_version: str = "rules-1.0.0",
    model_id: str | None = None,
    prompt_version: str | None = None,
    key: str | None = None,
) -> Receipt:
    """Record one run. Signed when a key is available, unsigned otherwise."""
    data = pdf.read_bytes()
    body = [
        n
        for n in tree.nodes.values()
        if not n.section.startswith("ANX-") and n.kind != "APPENDIX"
    ]

    receipt = Receipt(
        source_name=pdf.name,
        source_sha256=hashlib.sha256(data).hexdigest(),
        source_bytes=len(data),
        tree_fingerprint=tree.fingerprint(),
        clauses_parsed=len(body),
        engine=engine,
        ruleset_version=ruleset_version,
        model_id=model_id,
        prompt_version=prompt_version,
        rules_total=len(obligations),
        rules_certified=sum(
            1 for o in obligations if o.status is RuleStatus.CERTIFIED
        ),
        rulebook_sha256=_rulebook_hash(obligations),
        ledger_head=ledger_head,
        generated_at=_dt.datetime.now(_dt.timezone.utc),
    )
    if key:
        receipt.signature = _sign(receipt.payload(), key)
    return receipt


def verify_receipt(raw: dict, key: str) -> tuple[bool, str]:
    """Check a receipt's signature. Returns whether it holds, and why not."""
    signature = raw.get("signature")
    if not signature:
        return False, "This receipt carries no signature, so nothing can be checked."

    payload = {k: v for k, v in raw.items() if k != "signature"}
    expected = _sign(payload, key)
    if hmac.compare_digest(expected, signature):
        return True, "The signature matches. Nothing in this receipt has been altered."
    return False, (
        "The signature does not match. Either the receipt was edited after it "
        "was written, or it was signed with a different key."
    )
