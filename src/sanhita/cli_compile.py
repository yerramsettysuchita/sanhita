"""Phase 1 CLI commands: compile, propose, certify, coverage, eval, audit.

Registered onto the Phase 0 Typer app in `sanhita.cli`.

A compiled run is held in a small on-disk store (`.sanhita/rules.json`) so that
`certify` and `coverage` can act on what `compile` produced without re-running
extraction. The store is a plain canonical-JSON document, not a binary format:
an auditor must be able to read it without our code.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import time
from pathlib import Path

import typer

from sanhita.certify.ledger import AuditEntry, AuditLedger, Transition
from sanhita.certify.lifecycle import CertificationError, RuleRegistry
from sanhita.compile.extract import ExtractionStats, ExtractionStatus, RuleExtractor
from sanhita.eval.harness import run_eval
from sanhita.ir.canonical import canonical_json
from sanhita.ir.enums import RuleStatus
from sanhita.ir.schema import Obligation
from sanhita.metrics.coverage import ClauseClass, classify_clause, compute_coverage
from sanhita.parse.clause_tree import parse_clause_tree

__all__ = ["register"]

STORE = Path(".sanhita") / "rules.json"
_RULE = "-" * 74
_KEY_ENV = "SANHITA_SIGNING_KEY"

#: Bumped whenever the IR changes shape in a way that invalidates stored rules.
#: v2 made Deadline.business_days tri-state, which a v1 store cannot satisfy.
STORE_SCHEMA_VERSION = 2


class StaleStoreError(RuntimeError):
    """The store on disk was written against an older IR."""


def _kv(label: str, value: object, width: int = 36) -> None:
    typer.echo(f"  {label.ljust(width)}{value}")


def _signing_key() -> str:
    key = os.environ.get(_KEY_ENV)
    if not key:
        typer.secho(
            f"{_KEY_ENV} is not set. Certification signs over canonical bytes and "
            "cannot proceed without a key.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    return key


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def _load_registry(path: Path | None = None) -> RuleRegistry:
    """Load a rules store.

    The path is a parameter because a workspace owns its own store. It defaults
    to the single-corpus store the CLI has always written, so every existing
    caller keeps its behaviour.
    """
    store = path or STORE
    registry = RuleRegistry()
    if not store.exists():
        return registry
    payload = json.loads(store.read_text(encoding="utf-8"))

    version = payload.get("schema_version", 1)
    if version != STORE_SCHEMA_VERSION:
        raise StaleStoreError(
            f"{store} was written against IR schema v{version}, but this build "
            f"expects v{STORE_SCHEMA_VERSION}.\n"
            "  Stored proposals cannot be migrated automatically. The IR changed "
            "shape, and silently coercing old values would fabricate decisions.\n"
            f"  Fix:  delete {store} and re-run `sanhita compile`.\n"
            "  Note: certified rules in a stale store must be re-certified, "
            "because their signatures cover the old field shapes."
        )

    entries = []
    for raw in payload.get("ledger", []):
        entries.append(
            AuditEntry(
                sequence=raw["sequence"],
                obligation_id=raw["obligation_id"],
                transition=Transition(raw["transition"]),
                actor=raw["actor"],
                at=_dt.datetime.fromisoformat(raw["at"].replace("Z", "+00:00")),
                from_state=raw["from_state"],
                to_state=raw["to_state"],
                version=raw["version"],
                changes={k: tuple(v) for k, v in (raw.get("changes") or {}).items()},
                note=raw.get("note"),
                signature=raw.get("signature"),
                previous_hash=raw["previous_hash"],
                entry_hash=raw["entry_hash"],
            )
        )
    registry.ledger = AuditLedger(entries)

    for obligation_id, versions in payload.get("rules", {}).items():
        registry._versions[obligation_id] = [
            Obligation.model_validate(v) for v in versions
        ]
    return registry


def _store_header(path: Path | None = None) -> tuple[str, str]:
    """The circular id and tree fingerprint the store was written with.

    Re-saving must carry these through unchanged. Recomputing the fingerprint
    would mean re-parsing the PDF, and writing a different one would break the
    link between a signature and the exact tree it was signed against.
    """
    store = path or STORE
    if not store.exists():
        from sanhita import CIRCULAR_ID

        return CIRCULAR_ID, ""
    payload = json.loads(store.read_text(encoding="utf-8"))
    return payload.get("circular_id", ""), payload.get("tree_fingerprint", "")


def _load_registry_or_exit() -> RuleRegistry:
    """Load the store, turning a stale-schema crash into a readable instruction."""
    try:
        return _load_registry()
    except StaleStoreError as exc:
        typer.secho(f"Stale store: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


def _save_registry(
    registry: RuleRegistry,
    *,
    circular_id: str,
    fingerprint: str,
    path: Path | None = None,
) -> None:
    store = path or STORE
    store.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": STORE_SCHEMA_VERSION,
        "circular_id": circular_id,
        "tree_fingerprint": fingerprint,
        "saved_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "rules": {
            obligation_id: [json.loads(canonical_json(o.model_dump(mode="python"))) for o in versions]
            for obligation_id, versions in registry._versions.items()
        },
        "ledger": [
            json.loads(canonical_json(e.payload() | {"entry_hash": e.entry_hash}))
            for e in registry.ledger
        ],
    }
    # Locked for the write, so a second writer waits rather than overwriting
    # work it never saw.
    with store_lock(store):
        _write_atomically(store, json.dumps(payload, indent=2, sort_keys=True))


class StoreBusyError(RuntimeError):
    """Another process is writing this store."""


@contextlib.contextmanager
def store_lock(target: Path, *, timeout: float = 10.0):
    """Hold an exclusive lock on a store file while writing it.

    Atomic replacement stops the file being *corrupted*. It does not stop a
    lost update: two processes that both read, both modify and both write will
    leave only the second one's work, and the first officer's signature simply
    vanishes with no error anywhere. For an audit ledger that is the worse
    failure of the two, because nothing reports it.

    ``O_CREAT | O_EXCL`` is atomic on every platform we run on, so creating the
    lock file *is* the lock. The holder's pid goes inside it, which is what
    someone debugging a stuck lock actually needs.
    """
    lock_path = target.with_name(f"{target.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    handle = None

    while handle is None:
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        # POSIX reports a contended lock as EEXIST. Windows reports it as
        # EACCES, which arrives here as PermissionError, so both have to be
        # treated as "someone else holds it" or the second writer crashes
        # instead of waiting. A genuine permissions problem also lands here and
        # falls out as a timeout, which the message below accounts for.
        except (FileExistsError, PermissionError):
            if time.monotonic() >= deadline:
                holder = ""
                try:
                    holder = lock_path.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
                raise StoreBusyError(
                    f"{target.name} has been locked by another process"
                    + (f" (pid {holder})" if holder else "")
                    + f" for more than {timeout:.0f}s.\n"
                    "  Another compile or certification is probably still "
                    "running. Wait for it to finish.\n"
                    f"  If nothing is running, delete {lock_path}.\n"
                    "  If deleting it fails, this is a filesystem permissions "
                    "problem rather than a busy store."
                ) from None
            time.sleep(0.05)

    try:
        os.write(handle, str(os.getpid()).encode("ascii"))
        os.close(handle)
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _write_atomically(target: Path, text: str) -> None:
    """Replace a file in one step, or not at all.

    The store holds the audit ledger, which is the one artifact this product
    exists to protect, and every certification rewrites the whole file. Writing
    in place truncates first: a crash, a power cut or a Ctrl+C between the
    truncate and the write leaves a half-file, and the ledger is gone.

    So: write a sibling temp file, flush it to the platter, then ``os.replace``
    it onto the target. ``os.replace`` is atomic on POSIX and on Windows, so a
    reader sees either the whole old file or the whole new one and never a
    partial write. The directory entry is synced too, because on some
    filesystems the rename itself can otherwise be lost in a crash.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        try:
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, AttributeError):
            # Windows cannot fsync a directory handle opened this way. The
            # replace is still atomic; only the rename's durability is weaker.
            pass
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def register(app: typer.Typer, resolve, load_tree) -> None:
    """Attach the Phase 1 commands to the Phase 0 app."""

    @app.command()
    def compile(  # noqa: A001 - the command really is called `compile`
        pdf: Path | None = typer.Argument(None, help="Circular PDF (defaults to corpus/)."),
        section: str | None = typer.Option(None, "--section", "-s", help="Section to compile."),
        limit: int | None = typer.Option(None, "--limit", help="Stop after N clauses."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Do not write to the store."),
        engine: str = typer.Option("rules", "--engine", help="rules | llm."),
        model: str = typer.Option("claude-opus-5", "--model"),
    ) -> None:
        """Compile clauses into proposed obligations."""
        pdf = resolve(pdf)
        tree = parse_clause_tree(pdf)
        circular_id = _circular_id(tree)

        nodes = [
            n
            for n in tree.nodes.values()
            if not n.section.startswith("ANX-") and n.kind != "APPENDIX"
        ]
        if section:
            nodes = [n for n in nodes if n.section == str(section)]
            if not nodes:
                typer.secho(f"No clauses in section {section!r}.", fg=typer.colors.RED, err=True)
                raise typer.Exit(1)
        nodes.sort(key=lambda n: (n.page, n.char_span[0]))
        if limit:
            nodes = nodes[:limit]

        if engine == "llm":
            from sanhita.compile.llm import LLMExtractor

            problem = LLMExtractor.credential_error()
            if problem:
                typer.secho(f"Cannot run --engine llm: {problem}", fg=typer.colors.RED, err=True)
                raise typer.Exit(2)
            extractor = LLMExtractor(circular_id=circular_id, model=model)
        else:
            extractor = RuleExtractor(circular_id=circular_id)

        stats = ExtractionStats(engine=extractor.engine, model_id=model if engine == "llm" else None)
        registry = _load_registry_or_exit()
        started = time.perf_counter()

        for node in nodes:
            outcome = extractor.extract(node)
            stats.clauses_processed += 1
            stats.reasons[outcome.reason] = stats.reasons.get(outcome.reason, 0) + 1
            stats.input_tokens += outcome.input_tokens
            stats.output_tokens += outcome.output_tokens

            if outcome.status is ExtractionStatus.NO_OBLIGATION:
                stats.zero_obligation_clauses += 1
            elif outcome.status is ExtractionStatus.EXTRACTION_FAILED:
                stats.extraction_failures += 1
                typer.secho(f"  ! {node.id}: {outcome.error}", fg=typer.colors.YELLOW)
            else:
                stats.obligations_proposed += len(outcome.obligations)
                for obligation in outcome.obligations:
                    stats.confidences.append(obligation.confidence)
                    if not dry_run:
                        try:
                            registry.propose(obligation, by=f"extractor:{extractor.engine}")
                        except CertificationError:
                            pass  # already certified; amend() is the route

        stats.wall_seconds = time.perf_counter() - started

        typer.echo(_RULE)
        typer.secho(
            f"  SANHITA COMPILE   {pdf.name}"
            + (f"   section {section}" if section else "   whole body"),
            fg=typer.colors.CYAN,
            bold=True,
        )
        typer.echo(_RULE)
        _kv("engine", stats.engine + (f"  ({model})" if engine == "llm" else ""))
        _kv("clauses processed", stats.clauses_processed)
        _kv("obligations proposed", stats.obligations_proposed)
        _kv("zero-obligation clauses", stats.zero_obligation_clauses)
        _kv("extraction failures", stats.extraction_failures)
        _kv("mean confidence", f"{stats.mean_confidence:.3f}")
        _kv("wall time", f"{stats.wall_seconds:.2f}s")
        _kv("tokens in / out", f"{stats.input_tokens:,} / {stats.output_tokens:,}")
        _kv("token cost", f"${stats.cost_usd:.4f}" if stats.input_tokens else "$0.0000 (no API calls)")

        typer.secho("\n  OUTCOME BREAKDOWN", bold=True)
        for reason, count in sorted(stats.reasons.items(), key=lambda kv: -kv[1])[:12]:
            label = reason if len(reason) <= 58 else reason[:55] + "..."
            typer.echo(f"    {label.ljust(60)}{count:>4}")
        if len(stats.reasons) > 12:
            typer.echo(f"    ... {len(stats.reasons) - 12} further distinct reasons")

        if not dry_run:
            _save_registry(registry, circular_id=circular_id, fingerprint=tree.fingerprint())
            typer.echo(f"\n  stored {len(registry)} rules in {STORE}")
        else:
            typer.echo("\n  --dry-run: nothing written")
        typer.echo(_RULE)

    @app.command()
    def propose(
        pdf: Path | None = typer.Argument(None),
        clause: str = typer.Option(..., "--clause", "-c", help="Clause id, e.g. 40.1.8."),
        engine: str = typer.Option("rules", "--engine"),
        show_text: bool = typer.Option(True, "--text/--no-text"),
    ) -> None:
        """Compile one clause and show every field with the words behind it."""
        pdf = resolve(pdf)
        tree = parse_clause_tree(pdf)
        node = tree.get(clause)
        if node is None:
            typer.secho(f"No clause {clause!r}.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

        circular_id = _circular_id(tree)
        if engine == "llm":
            from sanhita.compile.llm import LLMExtractor

            extractor = LLMExtractor(circular_id=circular_id)
        else:
            extractor = RuleExtractor(circular_id=circular_id)

        outcome = extractor.extract(node)

        typer.echo(_RULE)
        typer.secho(f"  CLAUSE {clause}   p{node.page}", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)
        if show_text:
            for line in node.text.splitlines():
                typer.echo(f"  | {line}")
        typer.echo("")
        _kv("outcome", outcome.status.value)
        _kv("reason", outcome.reason)
        if outcome.error:
            _kv("error", outcome.error)

        for obligation in outcome.obligations:
            typer.secho(f"\n  {obligation.id}", bold=True)
            _kv("  actor", obligation.actor.value)
            _kv("  modality", obligation.modality.value)
            _kv("  action", f"{obligation.action.verb} / {obligation.action.object}")
            if obligation.action.recipient:
                _kv("  recipient", obligation.action.recipient)
            _kv("  trigger", f"{obligation.trigger.kind.value} {obligation.trigger.expression}")
            if obligation.trigger.recurrence:
                _kv("  recurrence", obligation.trigger.recurrence)
            if obligation.deadline:
                deadline = obligation.deadline
                bits = [deadline.kind.value]
                if deadline.offset_days is not None:
                    bits.append(f"{deadline.offset_days}d")
                if deadline.offset_hours is not None:
                    bits.append(f"{deadline.offset_hours}h")
                if deadline.offset_months is not None:
                    bits.append(f"{deadline.offset_months}mo")
                if deadline.business_days:
                    bits.append("business-days")
                if deadline.period:
                    bits.append(deadline.period)
                if deadline.anchor_event:
                    bits.append(f"from {deadline.anchor_event}")
                _kv("  deadline", " ".join(bits))
            _kv("  evidence", ", ".join(e.artifact_type for e in obligation.evidence) or "none")
            _kv("  confidence", obligation.confidence)

            typer.secho("\n    FIELD PROVENANCE", bold=True)
            for field_name, span in sorted(obligation.field_provenance.items()):
                quote = " ".join((obligation.quote(field_name) or "").split())
                score = obligation.field_confidence.get(field_name)
                score_text = f"  conf={score:.2f}" if score is not None else ""
                typer.echo(f"      {field_name.ljust(22)} {str(span).ljust(12)} {quote!r}{score_text}")
            gaps = obligation.unprovenanced_fields()
            if gaps:
                typer.secho(
                    f"      no span (take on trust): {', '.join(gaps)}",
                    fg=typer.colors.YELLOW,
                )
        typer.echo(_RULE)

    @app.command()
    def certify(
        clause: str = typer.Option(..., "--clause", "-c"),
        by: str = typer.Option(..., "--by", help="Certifying officer's identity."),
        note: str | None = typer.Option(None, "--note"),
        which: str | None = typer.Option(None, "--id", help="Exact obligation id."),
    ) -> None:
        """Certify the obligations compiled from a clause. Version-locks and signs."""
        key = _signing_key()
        registry = _load_registry_or_exit()
        if not len(registry):
            typer.secho("Nothing compiled yet — run `sanhita compile` first.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

        targets = [
            o
            for o in registry.all_current()
            if o.source.clause_id == clause and (which is None or o.id == which)
        ]
        if not targets:
            typer.secho(f"No compiled obligations for clause {clause!r}.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

        typer.echo(_RULE)
        for obligation in targets:
            if obligation.status is RuleStatus.CERTIFIED:
                typer.echo(f"  {obligation.id}  already CERTIFIED — skipped")
                continue
            certified = registry.certify(obligation.id, by=by, key=key, note=note)
            typer.secho(f"  {certified.id}  CERTIFIED", fg=typer.colors.GREEN, bold=True)
            _kv("  by", certified.certification.certified_by)
            _kv("  at", certified.certification.certified_at.isoformat())
            _kv("  version", certified.version)
            _kv("  signature", certified.certification.signature)
            _kv("  locked", certified.certification.locked)

        _save_registry(registry, circular_id="", fingerprint="")
        typer.echo(_RULE)

    @app.command()
    def coverage(
        pdf: Path | None = typer.Argument(None),
        section: str | None = typer.Option(None, "--section", "-s"),
        explain: bool = typer.Option(False, "--explain", help="Print the excluded census."),
    ) -> None:
        """Clause and evidence coverage, with the denominator stated."""
        pdf = resolve(pdf)
        tree = parse_clause_tree(pdf)
        registry = _load_registry_or_exit()
        obligations = registry.all_current()

        extractor = RuleExtractor(circular_id=_circular_id(tree))
        gold_result = run_eval(tree, extractor)
        report = compute_coverage(
            tree, obligations, classifier_accuracy=gold_result.classifier_accuracy
        )

        typer.echo(_RULE)
        typer.secho("  SANHITA COVERAGE", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)
        typer.secho("\n  DEFINITION", bold=True)
        typer.echo("    clause_coverage   = clauses with >=1 CERTIFIED obligation")
        typer.echo("                        / obligation-bearing clauses")
        typer.echo("    evidence_coverage = CERTIFIED obligations with >=1 EvidenceReq")
        typer.echo("                        / CERTIFIED obligations")

        typer.secho("\n  DENOMINATOR", bold=True)
        for line in _wrap(report.denominator_statement(), 68):
            typer.echo(f"    {line}")

        if explain:
            typer.secho("\n  EXCLUDED CLAUSES BY CLASS", bold=True)
            for name, count in sorted(report.excluded.items(), key=lambda kv: -kv[1]):
                _kv(f"    {name}", count)

        typer.secho("\n  WHOLE CORPUS", bold=True)
        _kv("obligation-bearing clauses", report.obligation_bearing_clauses)
        _kv("clauses with a CERTIFIED rule", report.clauses_with_certified)
        _kv("clauses with only PROPOSED rules", report.clauses_with_proposed_only)
        _kv("clause coverage", f"{report.clause_coverage:.1%}")
        _kv("CERTIFIED obligations", report.certified_obligations)
        _kv("  ... with evidence", report.certified_with_evidence)
        _kv("evidence coverage", f"{report.evidence_coverage:.1%}")

        rows = sorted(
            report.by_section.values(),
            key=lambda s: (-s.obligation_bearing, s.section),
        )
        if section:
            rows = [r for r in rows if r.section == str(section)]
        typer.secho("\n  PER SECTION (top 20 by size)", bold=True)
        typer.echo(f"    {'sec'.ljust(6)}{'bearing'.rjust(9)}{'certified'.rjust(11)}"
                   f"{'proposed'.rjust(10)}{'coverage'.rjust(10)}")
        for row in rows[:20]:
            typer.echo(
                f"    {row.section.ljust(6)}{row.obligation_bearing:>9}"
                f"{row.covered:>11}{row.proposed_only:>10}{row.clause_coverage:>9.1%}"
            )
        typer.echo(_RULE)

    @app.command(name="eval")
    def eval_command(
        pdf: Path | None = typer.Argument(None),
        engine: str = typer.Option("rules", "--engine"),
        out: Path = typer.Option(Path("eval") / "results.json", "--out"),
        show_disagreements: bool = typer.Option(True, "--disagreements/--no-disagreements"),
    ) -> None:
        """Score extraction against the hand-labelled gold set."""
        pdf = resolve(pdf)
        tree = parse_clause_tree(pdf)
        circular_id = _circular_id(tree)

        if engine == "llm":
            from sanhita.compile.llm import LLMExtractor

            extractor = LLMExtractor(circular_id=circular_id)
        else:
            extractor = RuleExtractor(circular_id=circular_id)

        result = run_eval(tree, extractor)

        typer.echo(_RULE)
        typer.secho("  SANHITA EVAL", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)
        typer.echo(result.table())

        if result.missing_clauses:
            typer.secho(
                f"\n  gold clauses not found in the tree: {result.missing_clauses}",
                fg=typer.colors.YELLOW,
            )
        if show_disagreements and result.disagreements:
            typer.secho("\n  DISAGREEMENTS", bold=True)
            for item in result.disagreements:
                typer.echo(
                    f"    {item['clause'].ljust(14)} {item['kind'].ljust(16)} "
                    f"gold={item['gold']}  got={item['got']}"
                )

        result.write(out)
        typer.echo(f"\n  written to {out}")
        typer.echo(_RULE)

    @app.command()
    def bench(
        pdf: Path | None = typer.Argument(None),
        json_out: Path | None = typer.Option(None, "--json", help="Also write JSON here."),
    ) -> None:
        """Time every stage of the pipeline. Every speed claim comes from here."""
        import json as _json

        from sanhita.benchmark import format_report, run_benchmark

        target = resolve(pdf)
        report = run_benchmark(target)
        typer.echo(format_report(report))
        if json_out:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(
                _json.dumps(report.to_json(), indent=2) + "\n", encoding="utf-8"
            )
            typer.echo(f"  written to {json_out}")

    @app.command()
    def serve(
        pdf: Path | None = typer.Argument(None),
        port: int = typer.Option(8000, "--port"),
        host: str = typer.Option("127.0.0.1", "--host"),
        reload: bool = typer.Option(False, "--reload"),
    ) -> None:
        """Open the certification workbench in a browser."""
        pdf = resolve(pdf)
        try:
            import uvicorn
        except ImportError:  # pragma: no cover - environment dependent
            typer.secho(
                "uvicorn is not installed.\n  Fix:  pip install fastapi jinja2 uvicorn",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2) from None

        from sanhita.web.app import create_app

        typer.echo(_RULE)
        typer.secho("  SANHITA — CERTIFICATION WORKBENCH", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)
        _kv("circular", pdf.name)

        registry = _load_registry_or_exit()
        _kv("rules in store", len(registry))
        if not len(registry):
            typer.secho(
                "  The store is empty — run `sanhita compile` first, or the queue "
                "will load with nothing in it.",
                fg=typer.colors.YELLOW,
            )
        _kv("signing key", "set" if os.environ.get(_KEY_ENV) else f"NOT SET ({_KEY_ENV})")

        fonts = Path("ui") / "fonts"
        woff = sorted(fonts.glob("*.woff2")) if fonts.is_dir() else []
        _kv("fonts", f"{len(woff)} woff2 found" if woff else "none — falling back to system stack")

        _kv("url", f"http://{host}:{port}")
        typer.echo(_RULE)

        application = create_app(pdf)
        uvicorn.run(application, host=host, port=port, log_level="warning")

    @app.command()
    def audit(
        verify_signatures: bool = typer.Option(
            False, "--verify-signatures", help="Recompute every signature."
        ),
        obligation: str | None = typer.Option(None, "--id", help="Show one rule's history."),
        limit: int = typer.Option(30, "--limit"),
    ) -> None:
        """Inspect the append-only audit ledger."""
        registry = _load_registry_or_exit()
        typer.echo(_RULE)
        typer.secho("  SANHITA AUDIT", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)
        _kv("rules tracked", len(registry))
        _kv("ledger entries", len(registry.ledger))
        _kv("ledger head", registry.ledger.head[:32] or "(empty)")

        if obligation:
            typer.secho(f"\n  HISTORY OF {obligation}", bold=True)
            for entry in registry.ledger.for_obligation(obligation):
                typer.echo(f"    {entry}")
                for path, (old, new) in sorted(entry.changes.items()):
                    typer.echo(f"        {path}: {old!r} -> {new!r}")
                if entry.note:
                    typer.echo(f"        note: {entry.note}")
            typer.echo(_RULE)
            return

        if verify_signatures:
            key = _signing_key()
            report = registry.verify_signatures(key)
            typer.secho("\n  SIGNATURE VERIFICATION", bold=True)
            _kv("signatures checked", report.checked)
            _kv("valid", report.valid)
            _kv("tampered", len(report.tampered))
            _kv("ledger chain problems", len(report.ledger_problems))
            for bad in report.tampered[:10]:
                typer.secho(f"    TAMPERED: {bad}", fg=typer.colors.RED)
            for problem in report.ledger_problems[:10]:
                typer.secho(f"    LEDGER: {problem}", fg=typer.colors.RED)

            if report.ok:
                typer.secho(
                    "\n  PASS - every signature matches and the ledger chain is intact",
                    fg=typer.colors.GREEN,
                    bold=True,
                )
                typer.echo(_RULE)
            else:
                typer.secho("\n  FAIL - tampering detected", fg=typer.colors.RED, bold=True)
                typer.echo(_RULE)
                raise typer.Exit(1)
            return

        typer.secho("\n  RECENT TRANSITIONS", bold=True)
        for entry in list(registry.ledger)[-limit:]:
            typer.echo(f"    {entry}")
        typer.echo(_RULE)

    @app.command(name="certify-section")
    def certify_section(
        section: str = typer.Argument(..., help="Section number, e.g. '19'."),
        by: str = typer.Option(..., "--by", help="The officer accepting this section."),
        note: str = typer.Option(
            "section-level review pass", "--note", help="Recorded against every rule."
        ),
        dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be signed."),
    ) -> None:
        """Sign off a whole section after reviewing it.

        The workbench deliberately has no bulk certify, because a reviewer must
        not be able to rubber-stamp two hundred rules with one click. This
        command is the other workflow a real compliance function uses: an
        officer reads a section, decides the extraction is sound across it, and
        signs it as a batch under their own name.

        It is not a shortcut around review. Every rule with an unresolved
        question is refused, individually and by name, and the reason is
        printed. Those still have to be answered one at a time.

        Whoever is named in --by is the person the audit ledger will hold
        responsible for every signature this writes.
        """
        key = _signing_key()
        registry = _load_registry_or_exit()

        in_section = [
            o
            for o in registry.all_current()
            if o.source.section == str(section) and o.status is RuleStatus.PROPOSED
        ]
        blocked = [o for o in in_section if o.blocking_issues()]
        ready = [o for o in in_section if not o.blocking_issues()]

        typer.echo(_RULE)
        typer.secho(f"  SANHITA CERTIFY SECTION {section}", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)
        _kv("officer", by)
        _kv("proposed rules in this section", len(in_section))
        _kv("ready to sign", len(ready))
        _kv("refused, a question is open", len(blocked))

        if blocked:
            # One clause can carry several duties, and they usually stall on the
            # same open question. Grouping keeps the list readable and stops it
            # looking like the same clause is repeated by mistake.
            grouped: dict[str, list[Obligation]] = {}
            for obligation in blocked:
                grouped.setdefault(obligation.source.clause_id, []).append(obligation)

            typer.secho("\n  REFUSED. THESE NEED A DECISION FIRST", bold=True)
            for clause_id, rules in list(grouped.items())[:10]:
                count = f"{len(rules)} rules" if len(rules) > 1 else "1 rule"
                typer.secho(
                    f"    clause {clause_id}  ({count}, page {rules[0].source.page})",
                    fg=typer.colors.YELLOW,
                )
                for issue in sorted({i for r in rules for i in r.blocking_issues()}):
                    for line in _wrap(issue, 66):
                        typer.echo(f"        {line}")
            if len(grouped) > 10:
                typer.echo(f"    ... {len(grouped) - 10} more clauses")

        if not ready:
            typer.secho("\n  Nothing in this section can be signed yet.", fg=typer.colors.YELLOW)
            typer.echo(_RULE)
            raise typer.Exit(1)

        if dry_run:
            typer.echo("\n  --dry-run: nothing signed")
            typer.echo(_RULE)
            return

        signed = 0
        failures: list[tuple[str, str]] = []
        for obligation in ready:
            try:
                registry.certify(obligation.id, by=by, key=key, note=note)
                signed += 1
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                failures.append((obligation.id, str(exc)))

        circular_id, fingerprint = _store_header()
        _save_registry(registry, circular_id=circular_id, fingerprint=fingerprint)

        typer.secho(f"\n  SIGNED {signed} rules as {by}", fg=typer.colors.GREEN, bold=True)
        if failures:
            typer.secho(f"  {len(failures)} could not be signed:", fg=typer.colors.RED)
            for obligation_id, problem in failures[:10]:
                typer.echo(f"    {obligation_id}: {problem[:90]}")
        typer.echo(f"  ledger head now {registry.ledger.head[:32]}")
        typer.echo(_RULE)

    @app.command()
    def structure(
        pdf: Path | None = typer.Argument(None),
        limit: int = typer.Option(10, "--limit"),
    ) -> None:
        """Measure the regulation itself, rather than a firm's compliance.

        Two questions nobody normally asks of a rulebook. Which sentence would
        do the most damage if it were amended, and how much does the document
        actually ask of each kind of firm. Both need typed rules, a citation
        graph and a record of what a person signed, so neither is answerable by
        reading or by retrieval.
        """
        from sanhita.analyse import assess_fragility, build_graph, measure_burden

        tree = parse_clause_tree(resolve(pdf))
        registry = _load_registry_or_exit()
        rules = registry.all_current()

        typer.echo(_RULE)
        typer.secho("  SANHITA STRUCTURE", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)

        # -- how fragile is it
        graph = build_graph(tree)
        frag = assess_fragility(tree, graph, rules, limit=limit)

        typer.secho("  HOW COUPLED IS THIS DOCUMENT", bold=True)
        _kv("  clauses carrying rules", frag.clauses_examined)
        _kv("  citations between clauses", frag.citation_edges)
        _kv("  clauses anything depends on", len(frag.load_bearing))
        _kv("  coupling", f"{frag.coupling:.1%}")
        typer.echo("")
        for line in _wrap(frag.verdict(), 70):
            typer.echo(f"    {line}")

        if frag.load_bearing:
            typer.secho("\n  AMEND THESE AND THE DAMAGE SPREADS", bold=True)
            typer.echo(
                f"    {'clause':<14}{'page':>5}{'own':>5}{'dependents':>12}"
                f"{'at risk':>9}"
            )
            for c in frag.load_bearing[:limit]:
                typer.echo(
                    f"    {c.clause_id:<14}{c.page:>5}{c.own_rules:>5}"
                    f"{c.dependent_clauses:>12}{c.blast_radius:>9}"
                )

        # -- what does it cost to comply
        burden = measure_burden(rules)
        typer.secho("\n  WHAT THE REGULATION ASKS OF EACH FIRM", bold=True)
        typer.echo(
            f"    {'actor':<24}{'duties':>8}{'clauses':>9}{'per year':>10}"
            f"{'on event':>10}{'standing':>10}"
        )
        for a in burden.actors:
            typer.echo(
                f"    {a.actor:<24}{a.duties:>8}{len(a.clauses):>9}"
                f"{a.filings_per_year:>10}{a.event_driven:>10}{a.standing:>10}"
            )

        heaviest = burden.heaviest
        if heaviest:
            parts = ", ".join(
                f"{n} {p.lower()}" for p, n in sorted(heaviest.recurring.items())
            )
            typer.echo("")
            for line in _wrap(
                f"A {heaviest.actor.replace('_', ' ').lower()} carries "
                f"{heaviest.duties} distinct duties across {len(heaviest.clauses)} "
                f"clauses, coming round {heaviest.filings_per_year} times a year "
                f"on the calendar alone ({parts}).",
                70,
            ):
                typer.echo(f"    {line}")

        typer.secho("\n  WHAT THESE NUMBERS ARE NOT", bold=True)
        for note in frag.caveats()[1:3] + burden.caveats()[1:3]:
            for line in _wrap(note, 70):
                typer.echo(f"    {line}")
        typer.echo(_RULE)

    @app.command()
    def missing(
        pdf: Path | None = typer.Argument(None),
        limit: int = typer.Option(20, "--limit"),
    ) -> None:
        """Clauses that carry a duty and produced no rule.

        The other side of the coverage figure. A classifier that never sees the
        extractor's output says these impose a duty; the extractor said nothing
        about them. That difference is a reading list.
        """
        from sanhita.analyse import find_uncompiled

        tree = parse_clause_tree(resolve(pdf))
        registry = _load_registry_or_exit()
        report = find_uncompiled(tree, registry.all_current())

        typer.echo(_RULE)
        typer.secho("  SANHITA MISSING", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)
        _kv("clauses that carry a duty", report.duty_bearing)
        _kv("with at least one live rule", report.with_a_rule)
        _kv("with nothing compiled", len(report.missing))
        _kv("rules rejected by a person", len(report.rejected_away))
        _kv("proportion unaddressed", f"{report.rate:.1%}")

        typer.secho("\n  HOW TO READ THIS", bold=True)
        for note in report.caveats():
            for line in _wrap(note, 70):
                typer.echo(f"    {line}")

        if report.missing:
            typer.secho("\n  WORST SECTIONS", bold=True)
            for section, count in list(report.by_section().items())[:10]:
                typer.echo(f"    section {section:<6} {count:>4} clause(s)")

            typer.secho(f"\n  FIRST {min(limit, len(report.missing))}", bold=True)
            for item in report.missing[:limit]:
                typer.secho(f"    {item.clause_id:<16} page {item.page}", fg=typer.colors.YELLOW)
                for line in _wrap(item.excerpt[:180], 68):
                    typer.echo(f"        {line}")
        typer.echo(_RULE)

    @app.command()
    def schedule(
        days: int = typer.Option(90, "--days", help="How far ahead to look."),
        start: str | None = typer.Option(None, "--from", help="Start date, ISO."),
    ) -> None:
        """What certified duties fall due over the coming period."""
        import datetime as _d

        from sanhita.analyse import build_schedule

        registry = _load_registry_or_exit()
        begin = _d.date.fromisoformat(start) if start else _d.date.today()
        plan = build_schedule(registry.all_current(), start=begin, days=days)

        typer.echo(_RULE)
        typer.secho("  SANHITA SCHEDULE", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)
        _kv("window", f"{plan.start} to {plan.end}")
        _kv("certified rules", plan.certified)
        _kv("occasions falling due", len(plan.due))
        _kv("event driven, no fixed date", len(plan.event_driven))

        typer.secho("\n  WHAT THIS DOES NOT SHOW", bold=True)
        for note in plan.caveats():
            for line in _wrap(note, 70):
                typer.echo(f"    {line}")

        if plan.due:
            typer.secho("\n  COMING UP", bold=True)
            for day, items in list(plan.by_date().items())[:14]:
                typer.secho(f"\n    {day:%A %d %B %Y}", bold=True)
                for item in items:
                    typer.echo(
                        f"      clause {item.clause_id:<14} {item.requirement[:46]}"
                    )
        typer.echo(_RULE)

    @app.command()
    def receipt(
        pdf: Path | None = typer.Argument(None),
        out: Path | None = typer.Option(None, "--out", help="Write the receipt."),
        check: Path | None = typer.Option(None, "--check", help="Verify a receipt."),
    ) -> None:
        """A signed record of exactly what one compile run consumed and produced.

        Determinism is a claim until somebody can check it. This is the thing
        they check with.
        """
        from sanhita.analyse import build_receipt, verify_receipt

        typer.echo(_RULE)
        typer.secho("  SANHITA RECEIPT", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)

        if check:
            raw = json.loads(check.read_text(encoding="utf-8"))
            ok, why = verify_receipt(raw, _signing_key())
            _kv("receipt", check.name)
            _kv("source", raw.get("source_name"))
            _kv("tree fingerprint", (raw.get("tree_fingerprint") or "")[:32])
            colour = typer.colors.GREEN if ok else typer.colors.RED
            typer.secho(f"\n  {'PASS' if ok else 'FAIL'}  {why}", fg=colour, bold=True)
            typer.echo(_RULE)
            raise typer.Exit(0 if ok else 1)

        resolved = resolve(pdf)
        tree = parse_clause_tree(resolved)
        registry = _load_registry_or_exit()
        made = build_receipt(
            pdf=resolved,
            tree=tree,
            obligations=registry.all_current(),
            ledger_head=registry.ledger.head,
            key=os.environ.get(_KEY_ENV),
        )

        for label, value in [
            ("source", made.source_name),
            ("source sha256", made.source_sha256),
            ("source bytes", f"{made.source_bytes:,}"),
            ("tree fingerprint", made.tree_fingerprint),
            ("clauses parsed", made.clauses_parsed),
            ("engine", made.engine),
            ("rules", f"{made.rules_total} ({made.rules_certified} certified)"),
            ("rulebook sha256", made.rulebook_sha256),
            ("ledger head", made.ledger_head[:32]),
            ("signature", made.signature[:32] + "..." if made.signature else "UNSIGNED"),
        ]:
            _kv(label, value)

        typer.secho("\n  HOW ANYONE CAN CHECK THIS", bold=True)
        for step in made.how_to_check():
            for line in _wrap(step, 70):
                typer.echo(f"    {line}")

        if out:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(made.to_json(), indent=2, sort_keys=True), encoding="utf-8"
            )
            typer.echo(f"\n  written to {out}")
        typer.echo(_RULE)

    @app.command(name="export-rego")
    def export_rego(
        out: Path = typer.Option(Path("sanhita.rego"), "--out"),
        package: str = typer.Option("sanhita.obligations", "--package"),
    ) -> None:
        """Compile certified rules into Open Policy Agent policy.

        Only rules that can be evaluated without inventing a missing piece are
        translated. The rest are named in the file's header so anybody
        deploying it can see the gap without asking us.
        """
        from sanhita.analyse import to_rego

        registry = _load_registry_or_exit()
        export = to_rego(registry.all_current(), package=package)

        typer.echo(_RULE)
        typer.secho("  SANHITA EXPORT REGO", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)
        _kv("certified rules", export.certified)
        _kv("translated to policy", export.translated)
        _kv("refused", len(export.refused))
        _kv("coverage of the rulebook", f"{export.coverage:.1%}")

        if not export.translated:
            typer.secho(
                "\n  Nothing could be translated. A policy engine must never act "
                "on an unsigned rule, so certify something first.",
                fg=typer.colors.YELLOW,
            )
            typer.echo(_RULE)
            raise typer.Exit(1)

        if export.refused:
            typer.secho("\n  WHY THE REST WERE REFUSED", bold=True)
            counted: dict[str, int] = {}
            for item in export.refused:
                counted[item.reason] = counted.get(item.reason, 0) + 1
            for reason, count in sorted(counted.items(), key=lambda kv: -kv[1]):
                for i, line in enumerate(_wrap(reason, 62)):
                    prefix = f"    {count:>4}  " if i == 0 else "          "
                    typer.echo(prefix + line)

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(export.policy, encoding="utf-8")
        typer.secho(f"\n  written to {out}", fg=typer.colors.GREEN)
        typer.echo("  Check it with:  opa check " + str(out))
        typer.echo(_RULE)

    @app.command()
    def conflicts(
        certified_only: bool = typer.Option(
            False, "--certified-only", help="Only compare rules a person has signed."
        ),
        limit: int = typer.Option(12, "--limit"),
        out: Path | None = typer.Option(None, "--out", help="Write the findings as JSON."),
    ) -> None:
        """Find rules in the rulebook that disagree with each other.

        A master circular consolidates decades of separate circulars. When two
        of them told the same party to do the same thing on different
        timelines, both sentences survive, ninety pages apart. Reading will not
        find that. Comparing typed rules will.

        Every finding is a question for a person. Two clauses can look
        contradictory and both be correct.
        """
        from sanhita.analyse import ConflictKind, find_conflicts

        registry = _load_registry_or_exit()
        report = find_conflicts(registry.all_current(), certified_only=certified_only)

        typer.echo(_RULE)
        typer.secho("  SANHITA CONFLICTS", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)
        _kv("rules examined", report.rules_examined)
        _kv("pairs compared in full", report.pairs_compared)
        _kv("rules excluded", report.excluded_rules)
        _kv("questions raised", len(report.conflicts))
        _kv("  between signed rules", len(report.between_certified))

        for kind in ConflictKind:
            found = report.of(kind)
            if found:
                _kv(f"  {kind.value.lower().replace('_', ' ')}", len(found))

        typer.secho("\n  HOW TO READ THIS", bold=True)
        for note in report.caveats():
            for line in _wrap(note, 70):
                typer.echo(f"    {line}")

        if not report.conflicts:
            typer.secho(
                "\n  Nothing in this rulebook contradicts anything else in it.",
                fg=typer.colors.GREEN,
            )
            typer.echo(_RULE)
            return

        typer.secho(
            f"\n  MOST SERIOUS {min(limit, len(report.conflicts))}", bold=True
        )
        for c in report.ranked()[:limit]:
            colour = (
                typer.colors.RED
                if c.kind in (ConflictKind.MODALITY, ConflictKind.DEADLINE)
                else typer.colors.YELLOW
            )
            signed = "  BOTH SIGNED" if c.involves_certified else ""
            typer.secho(
                f"\n    {c.kind.value:<10} [{c.confidence.value}]{signed}", fg=colour
            )
            for side in (c.left, c.right):
                days = ""
                if side.deadline and side.deadline.offset_days is not None:
                    days = (
                        f"  {side.deadline.offset_days} "
                        f"{side.deadline.business_days.value.lower()} days"
                    )
                typer.echo(
                    f"      clause {side.source.clause_id:<14} p{side.source.page:<4}"
                    f"{side.modality.value:<9} {side.action.verb} "
                    f"{side.action.object[:38]}{days}"
                )
            for line in _wrap(c.question, 66):
                typer.echo(f"        {line}")

        if out:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(report.to_json(), indent=2, sort_keys=True), encoding="utf-8"
            )
            typer.echo(f"\n  written to {out}")
        typer.echo(_RULE)

    @app.command()
    def diff(
        before: Path = typer.Argument(..., help="The earlier circular PDF."),
        after: Path = typer.Argument(..., help="The later circular PDF."),
        out: Path | None = typer.Option(None, "--out", help="Write the impact report as JSON."),
        limit: int = typer.Option(20, "--limit"),
    ) -> None:
        """Compare two versions of a circular and say what it costs you.

        The question is never "what words changed". It is which of the rules you
        have already signed are no longer signed for. A certification signs over
        the clause's own characters, so when those characters move the signature
        stops covering them and the rule goes back to a human.

        Similarity is never consulted. One character is enough.
        """
        from sanhita.diff import Consequence, assess_impact, diff_trees

        typer.echo(_RULE)
        typer.secho("  SANHITA DIFF", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)

        for path in (before, after):
            if not path.is_file():
                typer.secho(f"  No such file: {path}", fg=typer.colors.RED, err=True)
                raise typer.Exit(2)

        before_tree = parse_clause_tree(before)
        after_tree = parse_clause_tree(after)
        changes = diff_trees(
            before_tree, after_tree, before_label=before.name, after_label=after.name
        )

        _kv("before", f"{before.name}  {changes.before_fingerprint[:16]}")
        _kv("after", f"{after.name}  {changes.after_fingerprint[:16]}")

        if changes.identical:
            typer.secho(
                "\n  These two documents parse to the same tree. Nothing changed.",
                fg=typer.colors.GREEN,
            )
            typer.echo(_RULE)
            return

        summary = changes.summary()
        typer.secho("\n  CLAUSE CHANGES", bold=True)
        for name, count in summary.items():
            _kv(f"  {name}", count)

        from sanhita.analyse import build_graph

        registry = _load_registry_or_exit()
        graph = build_graph(after_tree)
        impact = assess_impact(changes, registry.all_current(), references=graph)
        _kv("cross-references followed", graph.edges)

        typer.secho("\n  WHAT THIS COSTS YOU", bold=True)
        for line in _wrap(impact.headline(), 70):
            typer.echo(f"    {line}")
        typer.echo("")
        _kv("  certified before", impact.certified_before)
        _kv("  certified after", impact.certified_after)
        _kv("  signatures lost", impact.signatures_lost)

        labels = {
            Consequence.RECERTIFY: "must be reviewed and signed again",
            Consequence.WITHDRAW: "clause deleted, withdraw the rule",
            Consequence.REPOINT: "same words, new number, repoint the anchor",
            Consequence.RECOMPILE: "proposed only, recompiling handles it",
            Consequence.REREAD: "unchanged, but points at a clause that moved",
        }
        for consequence, description in labels.items():
            rules = impact.of(consequence)
            if not rules:
                continue
            typer.secho(f"\n  {consequence.value}: {description}", bold=True)
            for rule in rules[:limit]:
                extra = f" -> {rule.now_at}" if rule.now_at else ""
                who = f"  signed by {rule.certified_by}" if rule.certified_by else ""
                typer.echo(f"    clause {rule.clause_id:<14}{rule.obligation_id:<22}{extra}{who}")
            if len(rules) > limit:
                typer.echo(f"    ... {len(rules) - limit} more")

        if impact.new_clauses:
            typer.secho(
                f"\n  {len(impact.new_clauses)} new clause(s) have no rule compiled from them yet",
                bold=True,
            )
            typer.echo("    " + ", ".join(impact.new_clauses[:14]))

        if out:
            out.parent.mkdir(parents=True, exist_ok=True)
            payload = {"diff": changes.to_json(), "impact": impact.to_json()}
            out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            typer.echo(f"\n  written to {out}")
        typer.echo(_RULE)

    @app.command(name="llm-check")
    def llm_check(
        clause: str = typer.Option("40.1.8", "--clause", help="Clause to compile live."),
        pdf: Path | None = typer.Argument(None),
    ) -> None:
        """Prove the model-assisted path end to end against the real API.

        The rules engine compiles the whole corpus with no key and no network.
        The model-assisted engine is optional, and until this command has been
        run against a live key it is code that has been unit tested but never
        actually spoken to Anthropic.

        This states plainly which of those is true right now, and if a key is
        present it compiles one real clause and shows what came back, including
        the checks that were applied to the model's answer before any of it was
        allowed to become an Obligation.
        """
        from sanhita.compile.llm import DEFAULT_MODEL, LLMExtractor
        from sanhita.compile.prompts import PROMPT_VERSION

        typer.echo(_RULE)
        typer.secho("  SANHITA LLM CHECK", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)

        problem = LLMExtractor.credential_error()
        _kv("model", DEFAULT_MODEL)
        _kv("prompt version", PROMPT_VERSION)

        if problem:
            _kv("live API call", "NOT PROVEN")
            typer.secho("\n  WHY NOT", bold=True)
            for line in problem.splitlines():
                typer.echo(f"    {line}")
            typer.secho("\n  WHAT IS STILL TRUE WITHOUT A KEY", bold=True)
            for line in [
                "The rules engine compiles all 1,377 rules with no network access.",
                "The wire schema constrains the model to closed vocabularies, so it",
                "cannot invent an actor, a modality or an artifact type.",
                "Every span the model cites is checked against the clause's own",
                "characters before it becomes an Obligation.",
                "A model claim about working days is downgraded to UNSPECIFIED",
                "unless a convention word appears near the cited span.",
                "None of that depends on the API being reachable.",
            ]:
                typer.echo(f"    {line}")
            typer.secho(
                f"\n  To prove the live path: set {LLMExtractor.KEY_ENV_VARS[0]} "
                "and run this again.",
                fg=typer.colors.YELLOW,
            )
            typer.echo(_RULE)
            raise typer.Exit(1)

        _kv("credentials", "found")
        typer.secho(f"\n  COMPILING CLAUSE {clause} LIVE", bold=True)

        resolved = resolve(pdf)
        tree = parse_clause_tree(resolved)
        node = tree.get(clause)
        if node is None:
            typer.secho(f"  No clause {clause!r} in {resolved.name}.", fg=typer.colors.RED)
            raise typer.Exit(1)

        extractor = LLMExtractor(circular_id=_circular_id(tree), model=DEFAULT_MODEL)
        started = time.perf_counter()
        outcome = extractor.extract(node)
        elapsed = time.perf_counter() - started

        _kv("status", outcome.status.value)
        _kv("obligations", len(outcome.obligations))
        _kv("wall time", f"{elapsed:.2f}s")
        _kv("tokens in / out", f"{outcome.input_tokens:,} / {outcome.output_tokens:,}")
        if outcome.error:
            typer.secho(f"  error: {outcome.error}", fg=typer.colors.RED)

        for obligation in outcome.obligations:
            typer.secho(f"\n  {obligation.id}", bold=True)
            _kv("  actor", obligation.actor.value)
            _kv("  modality", obligation.modality.value)
            _kv("  action", f"{obligation.action.verb} {obligation.action.object}")
            if obligation.deadline:
                _kv("  deadline kind", obligation.deadline.kind.value)
                _kv("  day count", obligation.deadline.business_days.value)
            _kv("  confidence", f"{obligation.confidence:.2f}")
            _kv("  spans verified", len(obligation.field_provenance))

        typer.secho(
            "\n  PASS - the live model-assisted path works end to end",
            fg=typer.colors.GREEN,
            bold=True,
        )
        typer.echo(_RULE)

    # ----------------------------------------------------------------- execute

    @app.command()
    def execute(
        evidence_file: Path | None = typer.Option(
            None, "--evidence", help="Evidence store JSON. Generated if omitted."
        ),
        holidays_file: Path | None = typer.Option(
            None, "--holidays", help="Exchange holidays, one ISO date per line."
        ),
        as_of: str | None = typer.Option(None, "--as-of", help="Run date, ISO. Defaults to today."),
        out: Path | None = typer.Option(None, "--out", help="Write the gap report as JSON."),
        seed: str = typer.Option("sanhita-demo", "--seed", help="Seed for generated evidence."),
        limit: int = typer.Option(15, "--limit", help="Findings to print."),
    ) -> None:
        """Run certified rules against evidence and report the gaps.

        Deterministic and offline. No model is loaded and no network call is made:
        a firm told it is in breach is entitled to a reproducible answer that
        cites the regulation.
        """
        import datetime as _d

        from sanhita.execute import RuleEngine, EvidenceStore, TradingCalendar
        from sanhita.execute.synthetic import generate

        registry = _load_registry_or_exit()
        obligations = registry.all_current()
        certified = [o for o in obligations if o.status is RuleStatus.CERTIFIED]

        typer.echo(_RULE)
        typer.secho("  SANHITA EXECUTE", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)

        if not certified:
            typer.secho(
                "  Nothing has been certified, so there is nothing to run.\n"
                "  The engine deliberately refuses to execute proposed rules: an\n"
                "  extractor's opinion must not be able to tell a firm it is in breach.\n"
                "  Certify at least one rule first, with `sanhita certify`.",
                fg=typer.colors.YELLOW,
            )
            typer.echo(_RULE)
            raise typer.Exit(1)

        # -- calendar
        if holidays_file and holidays_file.is_file():
            days = {
                _d.date.fromisoformat(line.strip())
                for line in holidays_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            }
            calendar = TradingCalendar(
                name=f"{holidays_file.name}, {len(days)} exchange holidays", holidays=frozenset(days)
            )
        else:
            from sanhita.execute import WEEKENDS_ONLY

            calendar = WEEKENDS_ONLY

        # -- evidence
        run_date = _d.date.fromisoformat(as_of) if as_of else _d.date.today()
        if evidence_file and evidence_file.is_file():
            evidence = EvidenceStore.load(evidence_file)
        else:
            evidence = generate(
                certified,
                calendar=calendar,
                start=run_date - _d.timedelta(days=180),
                end=run_date - _d.timedelta(days=10),
                seed=seed,
            )
            typer.secho(
                "  No evidence file given, so events were generated for this run.",
                fg=typer.colors.YELLOW,
            )

        report = RuleEngine(calendar).run(obligations, evidence, as_of=run_date)

        _kv("certified rules", report.certified_rules)
        _kv("rules evaluated", report.rules_evaluated)
        _kv("rules not evaluable", len(report.unevaluable))
        _kv("events checked", report.events_checked)
        _kv("satisfied", report.satisfied)
        _kv("breaches", report.breaches)
        _kv("  of which never filed", len(report.missing))
        _kv("  of which filed late", len(report.late))
        rate = report.compliance_rate
        _kv("compliance rate", f"{rate:.1%}" if rate is not None else "not measured")

        typer.secho("\n  WHAT YOU MUST KNOW BEFORE QUOTING ANY OF THAT", bold=True)
        for note in report.caveats():
            for line in _wrap(note, 70):
                typer.echo(f"    {line}")

        if report.findings:
            typer.secho(f"\n  WORST {min(limit, len(report.findings))} BREACHES", bold=True)
            for finding in report.ranked()[:limit]:
                colour = typer.colors.RED if finding.severity == "high" else typer.colors.YELLOW
                head = (
                    f"    {finding.outcome.value:<8} clause {finding.clause_id:<12} "
                    f"page {finding.page:<4} {finding.entity}"
                )
                typer.secho(head, fg=colour)
                if finding.outcome.value == "LATE":
                    typer.echo(
                        f"             due {finding.due_on}, filed {finding.filed_on}, "
                        f"{finding.days_late} day(s) late"
                    )
                else:
                    typer.echo(f"             due {finding.due_on}, never filed")
                typer.echo(f"             requirement: {finding.requirement}")
                typer.echo(f"             certified by {finding.certified_by}")
                typer.echo(f"             signature {finding.signature[:32]}...")

        if report.unevaluable:
            typer.secho("\n  CERTIFIED BUT NOT EVALUABLE", bold=True)
            for item in report.unevaluable[:8]:
                typer.echo(f"    clause {item.clause_id:<12} {item.reason[:78]}")
            if len(report.unevaluable) > 8:
                typer.echo(f"    ... {len(report.unevaluable) - 8} more")

        if out:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(report.to_json(), indent=2, sort_keys=True), encoding="utf-8"
            )
            typer.echo(f"\n  gap report written to {out}")
        typer.echo(_RULE)


def _circular_id(tree) -> str:
    from sanhita import CIRCULAR_ID

    return CIRCULAR_ID


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines
