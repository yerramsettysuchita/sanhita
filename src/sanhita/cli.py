"""The Sanhita command line.

    sanhita ingest corpus/stock-brokers-master-circular-2025-06-17.pdf
    sanhita tree --section 21
    sanhita footnotes --stats
    sanhita verify

`verify` is the load-bearing command. It re-parses the same bytes and asserts
that every id, span and hash is unchanged. If that ever fails, nothing
downstream - certification, signatures, deterministic execution - can be
trusted, so it exits non-zero and says exactly what moved.

Output is deliberately ASCII-only. These commands run in CI and in Windows
consoles whose code page is not UTF-8, and a compliance tool that crashes on a
box-drawing character while printing an audit summary is not a serious tool.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import typer

from sanhita import __version__
from sanhita.parse.clause_tree import ClauseTree, parse_clause_tree
from sanhita.parse.footnotes import FootnoteReport, extract_footnotes

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Sanhita - a regulation compiler for India's securities markets.",
)

_RULE = "-" * 74
_PDF_ARG = typer.Argument(None, help="Circular PDF. Defaults to the single PDF in corpus/.")


def _kv(label: str, value: object, width: int = 36) -> None:
    typer.echo(f"  {label.ljust(width)}{value}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(pdf: Path) -> tuple[ClauseTree, FootnoteReport]:
    tree = parse_clause_tree(pdf)
    report = extract_footnotes(tree.document, tree.clause_of_line)
    return tree, report


def _resolve(pdf: Path | None) -> Path:
    """Resolve the circular to work on.

    Omitting the path is supported so `sanhita tree --section 21` reads the way
    it is meant to be typed. With no argument, look in `corpus/` and accept the
    answer only if it is unambiguous: silently guessing between two circulars is
    how a rule gets certified against the wrong document.
    """
    if pdf is not None:
        if not pdf.exists():
            typer.secho(f"No such file: {pdf}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        return pdf

    for directory in (Path("corpus"), Path(".")):
        found = sorted(directory.glob("*.pdf"))
        if len(found) == 1:
            return found[0]
        # More than one circular is now normal: corpus/ carries several so the
        # upload path can be tested against real documents of different
        # typography. Preferring the worked example by name is not the guess
        # this function refuses to make. It is the one document the built-in
        # store's rules were certified against, and pairing any other PDF with
        # that store would show a rulebook next to the wrong text.
        for candidate in found:
            if candidate.name.startswith("stock-brokers-master-circular"):
                return candidate
        if len(found) > 1:
            typer.secho(
                f"{len(found)} PDFs in {directory}/ - name the one you mean:",
                fg=typer.colors.RED,
                err=True,
            )
            for candidate in found:
                typer.echo(f"    {candidate}", err=True)
            raise typer.Exit(2)

    typer.secho("No PDF given and none found in corpus/.", fg=typer.colors.RED, err=True)
    raise typer.Exit(2)


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------


@app.command()
def ingest(
    pdf: Path | None = _PDF_ARG,
    json_out: Path | None = typer.Option(None, "--json", help="Write full statistics as JSON."),
    show_warnings: bool = typer.Option(True, "--warnings/--no-warnings"),
) -> None:
    """Parse a circular and report what was found."""
    pdf = _resolve(pdf)
    tree, notes = _load(pdf)
    stats = tree.stats

    typer.echo(_RULE)
    typer.secho(f"  SANHITA INGEST   {pdf.name}", fg=typer.colors.CYAN, bold=True)
    typer.echo(_RULE)

    typer.secho("\n  DOCUMENT", bold=True)
    _kv("pages", stats.page_count)
    _kv("source sha256", _file_sha256(pdf))
    _kv(
        "table of contents",
        f"pp {stats.toc_pages[0]}-{stats.toc_pages[1]}" if stats.toc_pages else "not found",
    )
    _kv("body starts", f"p {stats.body_page_start}")
    _kv("annexures start", f"p {stats.annexure_page_start}" if stats.annexure_page_start else "-")
    _kv("appendix starts", f"p {stats.appendix_page_start}" if stats.appendix_page_start else "-")
    _kv("extracted characters", f"{stats.document_chars:,}")

    typer.secho("\n  STRUCTURE", bold=True)
    _kv("sections found", stats.sections)
    _kv("clauses at X.Y  (exactly)", stats.depth_counts.get(2, 0))
    _kv("clauses at X.Y.Z  (exactly)", stats.depth_counts.get(3, 0))
    _kv(
        "clauses at X.Y.Z.W and deeper",
        stats.depth_counts.get(4, 0) + stats.depth_counts.get(5, 0),
    )
    _kv("numbered depth >= 2  (cumulative)", stats.clauses_depth_2_plus)
    _kv("numbered depth >= 3  (cumulative)", stats.clauses_depth_3_plus)
    _kv("lettered items   body / all", f"{stats.lettered_items} / {stats.lettered_items_total}")
    _kv("roman items      body / all", f"{stats.roman_items} / {stats.roman_items_total}")
    _kv("annexures parsed", stats.annexures)
    _kv("annexure references in text", stats.annexure_mentions)
    _kv("appendix circular entries", stats.appendix_entries)
    _kv("total nodes", stats.total_nodes)

    typer.secho("\n  PROVENANCE", bold=True)
    _kv("footnote definitions", notes.definition_count)
    _kv("superscript markers in body", notes.marker_count)
    _kv("footnotes resolved to clauses", notes.resolved_count)
    _kv("markers with no definition", f"{len(notes.unresolved_markers)} {notes.unresolved_markers}")
    _kv("definitions with no marker", f"{len(notes.orphan_definitions)} {notes.orphan_definitions}")
    _kv("ambiguous markers", len(notes.ambiguous_markers))
    _kv("body circular references", len(notes.body_refs))
    _kv("body dated mentions", notes.dated_mentions)

    dated = [f.dated for f in notes.footnotes if f.dated]
    if dated:
        _kv("lineage span", f"{min(dated).isoformat()} to {max(dated).isoformat()}")

    if show_warnings:
        typer.secho("\n  INTEGRITY", bold=True)
        _kv("section numbering gaps", stats.section_gaps or "none")
        _kv("out-of-sequence sections", stats.out_of_sequence or "none")
        _kv("duplicate clause ids", len(stats.duplicate_ids))
        _kv("repeated item markers", len(stats.repeated_item_ids))
        _kv("cross-refs not read as clauses", len(stats.cross_references))
        _kv("rejected section candidates", len(stats.rejected_section_candidates))
        _kv("indent mismatches", len(stats.indent_mismatches))
        if stats.non_ascii is not None:
            _kv("character scan", stats.non_ascii.summary())
            for anomaly in stats.non_ascii.anomalies[:8]:
                typer.echo(f"      - {anomaly}")

    typer.secho("\n  RESULT", bold=True)
    _kv("tree fingerprint", tree.fingerprint())
    _kv("parse time", f"{stats.parse_seconds:.2f}s")
    typer.echo(_RULE)

    if json_out is not None:
        payload = {
            "pdf": str(pdf),
            "source_sha256": _file_sha256(pdf),
            "fingerprint": tree.fingerprint(),
            "parser_version": __version__,
            "structure": {
                "pages": stats.page_count,
                "sections": stats.sections,
                "depth_counts": dict(sorted(stats.depth_counts.items())),
                "depth_2_plus": stats.clauses_depth_2_plus,
                "depth_3_plus": stats.clauses_depth_3_plus,
                "lettered_items": stats.lettered_items,
                "lettered_items_total": stats.lettered_items_total,
                "roman_items": stats.roman_items,
                "roman_items_total": stats.roman_items_total,
                "annexures": stats.annexures,
                "annexure_mentions": stats.annexure_mentions,
                "appendix_entries": stats.appendix_entries,
                "total_nodes": stats.total_nodes,
            },
            "provenance": {
                "footnote_definitions": notes.definition_count,
                "markers": notes.marker_count,
                "resolved": notes.resolved_count,
                "unresolved_markers": notes.unresolved_markers,
                "orphan_definitions": notes.orphan_definitions,
                "body_refs": len(notes.body_refs),
                "dated_mentions": notes.dated_mentions,
            },
            "integrity": {
                "section_gaps": stats.section_gaps,
                "out_of_sequence": stats.out_of_sequence,
                "duplicate_ids": sorted(set(stats.duplicate_ids)),
                "cross_references": stats.cross_references,
                "rejected_section_candidates": stats.rejected_section_candidates,
                "indent_mismatches": stats.indent_mismatches,
            },
            "parse_seconds": round(stats.parse_seconds, 3),
        }
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        typer.echo(f"\n  statistics written to {json_out}")


# --------------------------------------------------------------------------
# tree
# --------------------------------------------------------------------------


@app.command()
def tree(
    pdf: Path | None = _PDF_ARG,
    section: str | None = typer.Option(None, "--section", "-s", help="Section number, e.g. 21."),
    annexure: str | None = typer.Option(None, "--annexure", "-a", help="Annexure number, e.g. 7."),
    text: bool = typer.Option(False, "--text", help="Print each node's verbatim text."),
    limit: int = typer.Option(60, "--limit", help="Maximum nodes to print."),
) -> None:
    """Show the parsed clause tree for one section or annexure."""
    pdf = _resolve(pdf)
    parsed = parse_clause_tree(pdf)

    if section is None and annexure is None:
        typer.secho("  SECTIONS", bold=True)
        for node_id in parsed.roots:
            node = parsed.nodes[node_id]
            if node.kind == "SECTION":
                typer.echo(f"  {node.number.rjust(4)}.  p{str(node.page).ljust(4)} {node.title}")
        raise typer.Exit(0)

    key = f"ANX-{annexure}" if annexure is not None else str(section)
    nodes = parsed.section(key)
    if not nodes:
        typer.secho(f"Nothing found for {key!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    typer.echo(_RULE)
    typer.secho(f"  {key}   {len(nodes)} nodes", fg=typer.colors.CYAN, bold=True)
    typer.echo(_RULE)

    for node in nodes[:limit]:
        indent = "  " * max(0, node.depth - 1)
        flag = "  [indent?]" if node.indent_mismatch else ""
        typer.echo(f"\n  {indent}{node.id}  [{node.kind}]  p{node.page}{flag}")
        if node.title:
            typer.echo(f"  {indent}  {node.title}")
        typer.echo(f"  {indent}  sha256 {node.sha256[:16]}...  span {node.char_span}")
        if node.footnote_markers:
            typer.echo(f"  {indent}  footnote markers {node.footnote_markers}")
        if text:
            for line in node.text.splitlines():
                typer.echo(f"  {indent}  | {line}")

    if len(nodes) > limit:
        typer.echo(f"\n  ... {len(nodes) - limit} more (raise --limit)")


# --------------------------------------------------------------------------
# footnotes
# --------------------------------------------------------------------------


@app.command()
def footnotes(
    pdf: Path | None = _PDF_ARG,
    stats: bool = typer.Option(False, "--stats", help="Summary only."),
    unresolved: bool = typer.Option(False, "--unresolved", help="Only what failed to bind."),
    limit: int = typer.Option(40, "--limit"),
) -> None:
    """Show the footnote provenance recovered from the circular."""
    pdf = _resolve(pdf)
    _parsed, report = _load(pdf)

    if stats:
        typer.echo(_RULE)
        typer.secho("  FOOTNOTE PROVENANCE", fg=typer.colors.CYAN, bold=True)
        typer.echo(_RULE)
        _kv("definitions below separator", report.definition_count)
        _kv("superscript markers in body", report.marker_count)
        _kv("resolved to a clause", report.resolved_count)
        _kv("markers with no definition", report.unresolved_markers or "none")
        _kv("definitions with no marker", report.orphan_definitions or "none")
        _kv("ambiguous", len(report.ambiguous_markers))
        _kv("with a circular number", sum(1 for f in report.footnotes if f.circular_ref))
        _kv("with a parsed date", sum(1 for f in report.footnotes if f.dated))
        _kv("body circular references", len(report.body_refs))
        _kv("body dated mentions", report.dated_mentions)

        dated = sorted(f.dated for f in report.footnotes if f.dated)
        if dated:
            _kv("earliest source circular", dated[0].isoformat())
            _kv("latest source circular", dated[-1].isoformat())
        _kv("clauses carrying lineage", len(report.by_clause()))
        typer.echo(_RULE)
        raise typer.Exit(0)

    shown = 0
    for ref in report.footnotes:
        if unresolved and ref.is_resolved:
            continue
        if shown >= limit:
            break
        shown += 1
        where = ref.clause_id or "UNBOUND"
        typer.echo(f"\n  [{str(ref.marker).rjust(3)}]  p{ref.page}  -> clause {where}")
        if ref.circular_ref:
            when = ref.dated.isoformat() if ref.dated else "date not parsed"
            typer.echo(f"        {ref.circular_ref}   {when}")
        for extra in ref.extra_circular_refs:
            typer.echo(f"        also {extra}")
        typer.echo(f"        {ref.raw_text[:110]}")

    if not shown:
        typer.echo("  nothing to show")


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


@app.command()
def verify(
    pdf: Path | None = _PDF_ARG,
    runs: int = typer.Option(2, "--runs", min=2, help="Independent parses to compare."),
) -> None:
    """Re-parse and assert that every id, span and hash is unchanged.

    This is the determinism proof the product rests on. A certified rule is
    signed over bytes that include its clause hash, so if parsing the same PDF
    twice produced different hashes, every signature would be meaningless.
    """
    pdf = _resolve(pdf)

    typer.echo(_RULE)
    typer.secho(f"  SANHITA VERIFY   {pdf.name}   {runs} runs", fg=typer.colors.CYAN, bold=True)
    typer.echo(_RULE)

    baseline: ClauseTree | None = None
    fingerprints: list[str] = []
    failures: list[str] = []

    for index in range(runs):
        parsed = parse_clause_tree(pdf)
        fingerprints.append(parsed.fingerprint())
        _kv(f"run {index + 1}", f"{parsed.fingerprint()}  ({parsed.stats.parse_seconds:.2f}s)")

        if baseline is None:
            baseline = parsed
            continue

        if set(baseline.nodes) != set(parsed.nodes):
            missing = sorted(set(baseline.nodes) - set(parsed.nodes))[:5]
            added = sorted(set(parsed.nodes) - set(baseline.nodes))[:5]
            failures.append(f"run {index + 1}: id sets differ (missing {missing}, added {added})")
            continue

        for node_id, first in baseline.nodes.items():
            second = parsed.nodes[node_id]
            if first.sha256 != second.sha256:
                failures.append(f"{node_id}: sha256 {first.sha256[:12]} != {second.sha256[:12]}")
            if first.char_span != second.char_span:
                failures.append(f"{node_id}: span {first.char_span} != {second.char_span}")
            if first.page != second.page:
                failures.append(f"{node_id}: page {first.page} != {second.page}")

    assert baseline is not None
    typer.echo("")
    _kv("nodes compared", len(baseline.nodes))
    _kv("distinct fingerprints", len(set(fingerprints)))

    if failures or len(set(fingerprints)) != 1:
        typer.secho("\n  FAIL - parsing is not deterministic", fg=typer.colors.RED, bold=True)
        for failure in failures[:20]:
            typer.echo(f"    - {failure}")
        if len(failures) > 20:
            typer.echo(f"    ... {len(failures) - 20} more")
        typer.echo(_RULE)
        raise typer.Exit(1)

    typer.secho(
        "\n  PASS - ids, spans and hashes are identical across runs",
        fg=typer.colors.GREEN,
        bold=True,
    )
    typer.echo(_RULE)


@app.command("demo-seed")
def demo_seed(
    root: Path = typer.Option(
        Path(".sanhita"), "--root", help="The store directory to seed."
    ),
    keep_accounts: bool = typer.Option(
        False, "--keep-accounts", help="Leave users.json alone."
    ),
    amendment: bool = typer.Option(
        False,
        "--amendment",
        help="Also register both Investment Adviser editions, so the "
        "comparison screen opens ready instead of empty.",
    ),
    corpus: Path = typer.Option(
        Path("corpus"), "--corpus", help="Where the circulars are."
    ),
    backup: bool = typer.Option(
        True, "--backup/--no-backup", help="Keep existing data aside first."
    ),
) -> None:
    """Build a clean demonstration state, backing up whatever is there.

    Recording a demo off a development store is how a submission leaks a real
    address, a throwaway account and an assessment attributed to nobody. This
    generates the demonstration state instead: one synthetic firm, one
    synthetic officer, four filing occasions of which one was never filed, and
    one recorded assessment run by a named account.

    The rulebook is read and never written. Existing firm data is moved into a
    timestamped backup folder rather than deleted.
    """
    from sanhita.demo_seed import DEMO_EMAIL, DEMO_PASSWORD, seed_demo_state

    typer.echo(_RULE)
    typer.secho("  SANHITA DEMO SEED", fg=typer.colors.CYAN, bold=True)
    typer.echo(_RULE)
    try:
        result = seed_demo_state(
            root,
            include_account=not keep_accounts,
            amendment=amendment,
            corpus=corpus,
            backup=backup,
        )
    except (RuntimeError, OSError) as exc:
        typer.secho(f"  {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    if result.backup:
        _kv("existing data moved to", result.backup)
        _kv("files moved", ", ".join(result.moved_aside) or "none")
    _kv("firm", result.firm + "  (synthetic)")
    _kv("officer", result.officer)
    if not keep_accounts:
        _kv("sign in with", f"{DEMO_EMAIL} / {DEMO_PASSWORD}")
    _kv("certified rules", result.certified)
    _kv("filing occasions", f"{result.occasions}, of which 1 was never filed")
    _kv("assessment recorded", result.assessment_id or "none")
    if result.editions:
        _kv("editions to compare", "; ".join(result.editions))
    _kv("confirmed gaps", result.open_gaps)
    _kv("not verifiable", f"{result.unverified}, duties with no record either way")
    typer.echo()
    typer.echo("  Nothing in rules.json was touched. The 183 certifications and")
    typer.echo("  their signatures are exactly as they were.")
    typer.echo(_RULE)


@app.command()
def version() -> None:
    """Print the parser version."""
    typer.echo(__version__)


# Phase 1 commands (compile, propose, certify, coverage, eval, audit) live in
# their own module and are attached here, so the Phase 0 parsing commands stay
# importable without pulling in the compiler and its dependencies.
from sanhita.cli_compile import register as _register_compile  # noqa: E402

_register_compile(app, _resolve, _load)


if __name__ == "__main__":  # pragma: no cover
    app()
