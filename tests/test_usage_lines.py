"""usage_lines: a name counts where code uses it, not where prose, strings or comments mention it.

    .venv\\Scripts\\python.exe tests\\test_usage_lines.py      (or pytest, if installed)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from faravex.navigate import usage_lines  # noqa: E402

PY = '''"""Module about docs."""
docs = load()          # docs: a real variable
print("see the docs")
def f(x=docs):
    """Return docs."""
    return f"{docs}" + other_docs
'''

JS = """const docs = load(); // docs comment
const s = 'docs' + "docs";
/* docs
   docs */
const t = `docs ${docs.length} docs`;
if (/docs/.test(s)) use(docs);
"""

MD = """# Docs
The docs explain `docs` and `api.docs()`.
plain docs here
"""


def test_python_counts_identifiers_only():
    assert usage_lines("a.py", PY, "docs") == [2, 4, 6]


def test_js_skips_strings_comments_templates_regexes():
    assert usage_lines("a.js", JS, "docs") == [1, 5, 6]


def test_docs_count_backticked_code_only():
    assert usage_lines("README.md", MD, "docs") == [2]


def test_other_files_match_whole_words():
    assert usage_lines("a.go", "docs := 1\nmydocs := docs\n// docsify\n", "docs") == [1, 2]


def test_broken_python_falls_back_to_whole_words():
    assert usage_lines("a.py", 'docs = (\n"docs"\n', "docs") == [1, 2]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
