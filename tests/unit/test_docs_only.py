"""T076: the `_docs_only` fanout-skip predicate.

Every changed file docs-shaped (prose extension or under `docs/`) ⇒
single-pass; ONE code file anywhere keeps the full battery; empty file
sets never skip.
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


def test_docs_prefix_is_exact_not_fuzzy():
    # "docsify/" is not under docs/ — code there keeps the battery.
    assert not _docs_only(SimpleNamespace(files=(dfile("docsify/app.py"),)))
    # A code-suffixed file UNDER docs/ is docs-shaped by path.
    assert _docs_only(SimpleNamespace(files=(dfile("docs/examples/x.py"),)))


def test_suffixes_are_exact():
    assert not _docs_only(SimpleNamespace(files=(dfile("notes.md.bak"),)))
    assert _docs_only(SimpleNamespace(files=(dfile("docs.md"),)))


def test_empty_file_set_never_skips():
    assert not _docs_only(SimpleNamespace(files=()))
    assert not _docs_only(SimpleNamespace(files=None))
