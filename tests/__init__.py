"""Test package.

Present so that `from tests.conftest import requires_corpus` resolves under a
bare `pytest` invocation. Without it, collection fails with
`ModuleNotFoundError: No module named 'tests'` on any machine whose working
directory is not already on `sys.path`, which made a clean checkout look broken
when it was not.
"""
