"""Compiling a document takes longer than a web request should.

The master circular is 1,377 rules over 399 pages. Doing that inside a GET
would hold the socket open for a minute and then time out somewhere between the
browser and the server, so compilation runs on a worker thread and the page asks
how it is going.

The job is deliberately small and boring:

  * one job per workspace at a time, because two compilers writing the same
    store would interleave proposals and corrupt the audit chain
  * progress is a count of clauses, not a percentage guess
  * the store is written once at the end, so a cancelled or crashed run leaves
    the previous rulebook exactly as it was

No LLM call is made unless the caller explicitly asks for it and a key is
present. The default engine is the deterministic rules extractor, which needs no
network at all.
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from sanhita.certify.lifecycle import CertificationError, RuleRegistry
from sanhita.compile.extract import ExtractionStatus, RuleExtractor
from sanhita.parse.clause_tree import ClauseTree

__all__ = ["CompileJob", "JobRunner"]


@dataclass
class CompileJob:
    """The live state of one compile run."""

    workspace_id: str
    engine: str
    total: int
    done: int = 0
    proposed: int = 0
    no_obligation: int = 0
    failed: int = 0
    state: str = "running"  # running | finished | failed | cancelled
    error: str | None = None
    started_at: _dt.datetime = field(
        default_factory=lambda: _dt.datetime.now(_dt.timezone.utc)
    )
    finished_at: _dt.datetime | None = None
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def percent(self) -> float:
        return round(self.done / self.total * 100, 1) if self.total else 0.0

    @property
    def seconds(self) -> float:
        end = self.finished_at or _dt.datetime.now(_dt.timezone.utc)
        return (end - self.started_at).total_seconds()

    @property
    def running(self) -> bool:
        return self.state == "running"

    def cancel(self) -> None:
        self._cancel.set()

    def to_json(self) -> dict:
        return {
            "workspace": self.workspace_id,
            "engine": self.engine,
            "state": self.state,
            "done": self.done,
            "total": self.total,
            "percent": self.percent,
            "proposed": self.proposed,
            "no_obligation": self.no_obligation,
            "failed": self.failed,
            "seconds": round(self.seconds, 1),
            "error": self.error,
        }


class JobRunner:
    """Holds at most one compile job per workspace."""

    def __init__(self) -> None:
        self._jobs: dict[str, CompileJob] = {}
        self._lock = threading.Lock()

    def get(self, workspace_id: str) -> CompileJob | None:
        return self._jobs.get(workspace_id)

    def start(
        self,
        *,
        workspace_id: str,
        tree: ClauseTree,
        circular_id: str,
        store_path: Path,
        engine: str = "rules",
        model: str = "claude-opus-5",
        on_done=None,
    ) -> CompileJob:
        """Begin a compile run. Raises RuntimeError if one is already going."""
        with self._lock:
            current = self._jobs.get(workspace_id)
            if current is not None and current.running:
                raise RuntimeError("A compile is already running for this document.")

            nodes = [
                n
                for n in tree.nodes.values()
                if not n.section.startswith("ANX-") and n.kind != "APPENDIX"
            ]
            nodes.sort(key=lambda n: (n.page, n.char_span[0]))

            job = CompileJob(
                workspace_id=workspace_id, engine=engine, total=len(nodes)
            )
            self._jobs[workspace_id] = job

        thread = threading.Thread(
            target=self._run,
            args=(job, nodes, tree, circular_id, store_path, engine, model, on_done),
            name=f"compile:{workspace_id}",
            daemon=True,
        )
        thread.start()
        return job

    # ------------------------------------------------------------------ worker

    def _run(
        self, job, nodes, tree, circular_id, store_path, engine, model, on_done
    ) -> None:
        from sanhita.cli_compile import _load_registry, _save_registry

        try:
            if engine == "llm":
                from sanhita.compile.llm import LLMExtractor

                problem = LLMExtractor.credential_error()
                if problem:
                    raise RuntimeError(problem)
                extractor = LLMExtractor(circular_id=circular_id, model=model)
            else:
                extractor = RuleExtractor(circular_id=circular_id)

            registry = _load_registry(store_path)

            for node in nodes:
                if job._cancel.is_set():
                    job.state = "cancelled"
                    job.finished_at = _dt.datetime.now(_dt.timezone.utc)
                    return

                outcome = extractor.extract(node)
                job.done += 1

                if outcome.status is ExtractionStatus.NO_OBLIGATION:
                    job.no_obligation += 1
                elif outcome.status is ExtractionStatus.EXTRACTION_FAILED:
                    job.failed += 1
                else:
                    for obligation in outcome.obligations:
                        try:
                            registry.propose(
                                obligation, by=f"extractor:{extractor.engine}"
                            )
                            job.proposed += 1
                        except CertificationError:
                            # Already certified. Amending is a human decision,
                            # never something a bulk recompile does silently.
                            pass

            _save_registry(
                registry,
                circular_id=circular_id,
                fingerprint=tree.fingerprint(),
                path=store_path,
            )
            job.state = "finished"

        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            job.state = "failed"
            job.error = str(exc) or exc.__class__.__name__
        finally:
            if job.finished_at is None:
                job.finished_at = _dt.datetime.now(_dt.timezone.utc)
            if on_done is not None and job.state == "finished":
                try:
                    on_done(job)
                except Exception:  # noqa: BLE001 - a reload failure is not the job's
                    pass


def wait_for(job: CompileJob, timeout: float = 60.0) -> CompileJob:
    """Block until a job leaves the running state. For tests, not for routes."""
    deadline = time.monotonic() + timeout
    while job.running and time.monotonic() < deadline:
        time.sleep(0.01)
    return job
