"""Tests for the frontend.

There is no build step and no bundler, so nothing would catch a syntax error
in a page or a reference to a file that was renamed. These tests stand in for
the bundler that this architecture deliberately does not have.

Node is optional: when it is absent the syntax checks skip, and the
dependency-free checks (assets exist, pages are wired up) still run.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import STATIC_DIR

STATIC = Path(STATIC_DIR)
NODE = shutil.which("node")

SCRIPT_BLOCK = re.compile(r"<script type=\"module\">(.*?)</script>", re.DOTALL)
PAGES = sorted(STATIC.glob("*.html"))
MODULES = sorted((STATIC / "js").glob("*.js"))


def _check_syntax(source: str, name: str) -> None:
    result = subprocess.run(
        [NODE, "--input-type=module", "--check"],
        input=source,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"syntax error in {name}:\n{result.stderr[:800]}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
class TestSyntax:
    def test_modules_parse(self):
        for module in MODULES:
            _check_syntax(module.read_text(), module.name)

    def test_inline_page_scripts_parse(self):
        for page in PAGES:
            for index, block in enumerate(SCRIPT_BLOCK.findall(page.read_text())):
                _check_syntax(block, f"{page.name} block {index}")


class TestAssets:
    def test_pages_exist(self):
        names = {p.name for p in PAGES}
        assert {
            "index.html",
            "learn.html",
            "practice.html",
            "exercise.html",
            "playground.html",
            "progress.html",
            "review.html",
        } <= names

    @pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
    def test_referenced_assets_exist(self, page):
        """Every local href/src a page names must actually be there."""
        text = page.read_text()
        refs = re.findall(r'(?:href|src)="(/[^"#?]*)"', text)
        for ref in refs:
            target = STATIC / ref.lstrip("/")
            assert target.exists(), f"{page.name} references missing asset: {ref}"

    def test_javascript_imports_resolve(self):
        """Imports between modules must point at files that exist."""
        for module in MODULES:
            for spec in re.findall(r"from\s+'([^']+)'", module.read_text()):
                if spec.startswith("/"):
                    assert (STATIC / spec.lstrip("/")).exists(), f"{module.name}: {spec}"

    def test_every_api_method_used_exists(self):
        """`api.foo(...)` must correspond to a method on the api object.

        Found the hard way in the SQL trainer: the playground called
        `api.datasets()`, which was never defined. The page loaded, rendered,
        and reported no error other than a banner reading "api.datasets is
        not a function" -- the dataset selector was simply gone. Syntax
        checks, asset checks and HTTP checks all passed.
        """
        source = (STATIC / "js" / "api.js").read_text()
        defined = set(re.findall(r"^\s{2}([A-Za-z_]\w*):", source, re.MULTILINE))
        assert "curriculum" in defined, "failed to parse api.js definitions"

        used = set()
        for page in PAGES:
            used.update(re.findall(r"\bapi\.([A-Za-z_]\w*)\s*\(", page.read_text()))

        missing = used - defined
        assert not missing, (
            f"pages call api methods that do not exist: {sorted(missing)} "
            f"(defined: {sorted(defined)})"
        )

    def test_no_api_method_is_unused(self):
        """An unused method is usually a rename that was only half done."""
        source = (STATIC / "js" / "api.js").read_text()
        defined = set(re.findall(r"^\s{2}([A-Za-z_]\w*):", source, re.MULTILINE))
        used = set()
        for page in PAGES:
            used.update(re.findall(r"\bapi\.([A-Za-z_]\w*)\s*\(", page.read_text()))
        unused = defined - used
        assert not unused, f"api.js defines nothing calls: {sorted(unused)}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
class TestPageWiring:
    def test_page_has_nav_placeholder(self, page):
        assert "<header data-nav>" in page.read_text()

    def test_page_mounts_the_nav(self, page):
        assert "mountNav()" in page.read_text()

    def test_page_calls_the_api(self, page):
        assert "api." in page.read_text()

    def test_page_has_no_build_step_violations(self, page):
        """No CDN scripts, no bundler, no framework -- by design.

        Only `src`/`href` *attribute values* are checked. A bare search for
        "http://" would trip over the SVG namespace URI in the inline
        favicon, which is an identifier rather than a fetched asset.
        """
        text = page.read_text()
        remote = [
            url
            for url in re.findall(r'(?:href|src)="(https?://[^"]+)"', text)
            if not url.startswith("http://www.w3.org/")
        ]
        assert not remote, f"{page.name} loads remote assets: {remote}"
        for framework in ("react", "vue", "angular", "jquery", "bootstrap"):
            assert framework not in text.lower(), f"{page.name} references {framework}"

    def test_page_has_no_leftovers_from_another_project(self, page):
        """This app teaches Docker; a stray reference is a leftover."""
        text = page.read_text()
        for word in ("Python Trainer", "starter_code", "SQL Trainer", "λ"):
            assert word not in text, f"{page.name} still mentions {word!r}"

    def test_page_says_what_the_app_is(self, page):
        """The title should name the thing being taught."""
        assert "Docker Trainer" in page.read_text()


@pytest.mark.skipif(NODE is None, reason="node not installed")
class TestMarkdownRenderer:
    """The renderer has no framework behind it, so its behaviour is tested
    against the real lesson bodies rather than a fixture."""

    @staticmethod
    def _render(source: str, tmp_path: Path) -> str:
        target = tmp_path / "markdown.mjs"
        target.write_text((STATIC / "js" / "markdown.js").read_text())
        script = tmp_path / "run.mjs"
        script.write_text(
            "import { renderMarkdown } from './markdown.mjs';\n"
            "import { readFileSync } from 'node:fs';\n"
            "process.stdout.write(renderMarkdown(readFileSync(process.argv[2], 'utf8')));\n"
        )
        input_file = tmp_path / "input.md"
        input_file.write_text(source)
        result = subprocess.run(
            [NODE, str(script), str(input_file)], capture_output=True, text=True, cwd=tmp_path
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def test_headings_and_code(self, tmp_path):
        html = self._render("## Title\n\n```dockerfile\nFROM alpine\n```\n", tmp_path)
        assert "<h2>Title</h2>" in html
        assert '<pre><code class="language-dockerfile">FROM alpine</code></pre>' in html

    def test_inline_formatting(self, tmp_path):
        html = self._render("Use `FROM` and **bold** and *italic*.\n", tmp_path)
        assert "<code>FROM</code>" in html
        assert "<strong>bold</strong>" in html
        assert "<em>italic</em>" in html

    def test_asterisks_inside_code_span_are_literal(self, tmp_path):
        """Emphasis must not be applied inside a code span."""
        html = self._render("`a*b*c`\n", tmp_path)
        assert html == "<p><code>a*b*c</code></p>"

    def test_numbers_in_prose_are_not_code(self, tmp_path):
        """A code span is replaced by a placeholder, and the placeholder must
        not be mistaken for an ordinary number in the surrounding text."""
        html = self._render("There are 3 items and `x` here.\n", tmp_path)
        assert "<code>x</code>" in html
        assert "3 items" in html
        assert "<code>3</code>" not in html

    def test_lists(self, tmp_path):
        assert "<ul><li>one</li><li>two</li></ul>" in self._render("- one\n- two\n", tmp_path)
        assert "<ol><li>first</li><li>second</li></ol>" in self._render("1. first\n2. second\n", tmp_path)

    def test_tables(self, tmp_path):
        html = self._render("| A | B |\n| --- | --- |\n| 1 | 2 |\n", tmp_path)
        assert "<th>A</th>" in html and "<td>2</td>" in html

    def test_blockquote_folds_paragraphs(self, tmp_path):
        html = self._render("> **note**\n> more text\n", tmp_path)
        assert "<blockquote>" in html and "<strong>note</strong>" in html

    def test_html_in_content_is_escaped(self, tmp_path):
        html = self._render("Text <script>alert(1)</script>\n", tmp_path)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_real_lesson_bodies_render(self, tmp_path):
        """Every shipped lesson body must render without throwing."""
        from app.curriculum.loader import load_curriculum

        curriculum = load_curriculum()
        for lesson in curriculum.lessons.values():
            if not lesson.body.strip():
                continue
            html = self._render(lesson.body, tmp_path)
            assert html.strip(), f"{lesson.id} rendered to nothing"
            assert "<h1>" in html or "<h2>" in html, f"{lesson.id} has no headings"


@pytest.mark.skipif(NODE is None, reason="node not installed")
class TestFileEditor:
    """The multi-file editor has no framework behind it either."""

    @staticmethod
    def _run(tmp_path: Path, body: str) -> str:
        script = tmp_path / "run.mjs"
        script.write_text(
            "const el = (tag, attrs = {}, children = []) => {\n"
            "  const node = { tag, attrs, children, className: attrs.class || '',\n"
            "    textContent: attrs.text || '',\n"
            "    append: (...kids) => node.children.push(...kids),\n"
            "    replaceChildren: (...kids) => { node.children = kids; },\n"
            "    setAttribute: () => {}, addEventListener: () => {} };\n"
            "  return node;\n"
            "};\n"
            "const mount = (node, ...kids) => node;\n"
            "globalThis.el = el; globalThis.mount = mount;\n"
            f"{body}\n"
        )
        result = subprocess.run(
            [NODE, str(script)], capture_output=True, text=True, cwd=tmp_path
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def test_the_editor_lists_a_tab_per_file(self, tmp_path):
        source = (STATIC / "js" / "files.js").read_text()
        # The editor's tab list is built from the file map it is given.
        assert "contents.keys()" in source
        assert source.count("file-tab") >= 2
