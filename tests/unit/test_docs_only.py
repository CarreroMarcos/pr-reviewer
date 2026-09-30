"""T076: the `_docs_only` fanout-skip predicate.

Every changed file prose-suffixed (.md/.txt/.rst/.mdx, exact case) ⇒
single-pass; ONE code file anywhere keeps the full battery; empty or
attribute-less file sets never skip. Path prefixes are deliberately not
consulted (PR #173 r1: docs/ must never route executable code away from
the battery).
"""

from types import SimpleNamespace

from worker_handler import _docs_only


def dfile(name):
    return SimpleNamespace(filename=name)


def test_all_docs_shapes_skip():
    files = (
        dfile("README.md"),
        dfile("docs/guide.md"),
        dfile("NOTES.txt"),
        dfile("docs/api.rst"),
        dfile("CHANGELOG.mdx"),
    )
    assert _docs_only(SimpleNamespace(files=files))


def test_one_code_file_keeps_fanout():
    files = (dfile("README.md"), dfile("src/main.py"))
    assert not _docs_only(SimpleNamespace(files=files))


def test_code_under_docs_keeps_fanout():
    """PR #173 r1 MEDIUM: a docs/ path never routes executable code away
    from the security battery — classification is suffix-only."""
    assert not _docs_only(SimpleNamespace(files=(dfile("docs/examples/x.py"),)))
    assert not _docs_only(SimpleNamespace(files=(dfile("docs/Makefile"),)))


def test_docsify_is_not_docs():
    assert not _docs_only(SimpleNamespace(files=(dfile("docsify/app.py"),)))
    assert not _docs_only(SimpleNamespace(files=(dfile("docsify/notes.md.bak"),)))


def test_suffixes_are_exact():
    assert not _docs_only(SimpleNamespace(files=(dfile("notes.md.bak"),)))
    assert _docs_only(SimpleNamespace(files=(dfile("docs.md"),)))
    # Case-sensitive by design — fail-safe direction: an uppercase suffix
    # fans out (full battery) rather than silently widening the skip
    # (Gate-58 coverage-2 pin).
    assert not _docs_only(SimpleNamespace(files=(dfile("README.MD"),)))


def test_empty_file_set_never_skips():
    assert not _docs_only(SimpleNamespace(files=()))
    assert not _docs_only(SimpleNamespace(files=None))
    # Defensive getattr contract: no files attribute at all.
    assert not _docs_only(SimpleNamespace())
