"""The compiler front-end: clause text to proposed Obligations.

Nothing in this package decides anything. It *proposes*, and every proposal is a
candidate awaiting a named human. Two engines produce proposals:

  rules   deterministic, span-exact, incapable of inventing text
  llm     a language model constrained to the IR schema and validated by Pydantic

The rules run first. The model is asked only about what the rules decline, and
its output is checked against the clause's own characters before it is accepted.
"""

from sanhita.compile.temporal import (
    RULESET_VERSION,
    TemporalReading,
    parse_commencement,
    parse_recurrence,
    parse_temporal,
)

__all__ = [
    "RULESET_VERSION",
    "TemporalReading",
    "parse_commencement",
    "parse_recurrence",
    "parse_temporal",
]
