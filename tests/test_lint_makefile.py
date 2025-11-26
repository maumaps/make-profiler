import io
from make_profiler import parser, lint_makefile


def run_validation(
    mk: str,
    root_dir: str | None = None,
) -> tuple[bool, list[lint_makefile.LintError]]:
    lines = mk.splitlines()
    ast = parser.parse(io.StringIO(mk))
    targets, deps, dep_map = lint_makefile.parse_targets(ast, lines)
    errors: list[lint_makefile.LintError] = []
    valid = lint_makefile.validate(
        lines,
        targets,
        deps,
        dep_map,
        root_dir=root_dir,
        errors=errors,
    )
    return valid, errors


def test_missing_rule() -> None:
    mk = "all: foo\n"
    valid, errors = run_validation(mk)
    assert not valid
    assert any(err.error_type == "missing rule" for err in errors)


def test_spaces_after_multiline_continuation() -> None:
    mk = (
        "all: foo bar \\\n"
        "    baz ## [FINAL] deploy\n"
        "foo: ## first dep\n"
        "\t@echo foo\n"
        "bar: ## second dep\n"
        "\t@echo bar\n"
        "baz: ## third dep\n"
        "\t@echo baz\n"
    )
    valid, errors = run_validation(mk)
    assert valid, errors
def test_trailing_spaces_after_continuation() -> None:
    mk = "all: foo \\  \n\t@echo foo\n"
    valid, errors = run_validation(mk)
    assert not valid
    assert any(err.error_type == "trailing spaces" for err in errors)


def test_trailing_spaces() -> None:
    mk = (
        "all: foo ## [FINAL] doc  \n"
        "\t@echo foo\n"
        "foo: ## doc\n"
        "\t@echo bar\n"
    )
    valid, errors = run_validation(mk)
    assert not valid
    assert any(err.error_type == "trailing spaces" for err in errors)


def test_space_instead_of_tab() -> None:
    mk = (
        "all: ## [FINAL] doc\n"
        "  @echo foo\n"
    )
    valid, errors = run_validation(mk)
    assert not valid
    assert any(err.error_type == "space instead of tab" for err in errors)


def test_error_includes_line_info() -> None:
    mk = (
        "all: foo ## [FINAL] doc  \n"
        "\t@echo foo\n"
    )
    valid, errors = run_validation(mk)
    assert not valid
    assert any(err.error_type == "trailing spaces" for err in errors)
    trailing = next(err for err in errors if err.error_type == "trailing spaces")
    assert (
        trailing.line_number is not None
    ), "line_number should be present for trailing space errors"
    assert mk.splitlines()[trailing.line_number].endswith("  ")
    assert trailing.line_text.endswith("  ")


def test_missing_rule_line_info() -> None:
    mk = "all: foo\n"
    valid, errors = run_validation(mk)
    assert not valid
    assert any(e.error_type == "missing rule" for e in errors)
    err = next(e for e in errors if e.error_type == "missing rule")
    expected = next(i for i, line in enumerate(mk.splitlines()) if "all:" in line)
    assert err.line_number == expected
    assert err.line_text.startswith("all:")


def test_orphan_and_no_docs_line_info() -> None:
    mk = "foo:\n\t@echo foo\n"
    valid, errors = run_validation(mk)
    assert not valid
    expected = next(i for i, line in enumerate(mk.splitlines()) if line.startswith("foo"))
    assert any(e.error_type == "orphan target" for e in errors)
    assert any(e.error_type == "target without comments" for e in errors)
    orphan = next(e for e in errors if e.error_type == "orphan target")
    nodoc = next(e for e in errors if e.error_type == "target without comments")
    assert orphan.line_number == expected
    assert nodoc.line_number == expected


def test_orphan_and_no_docs() -> None:
    mk = "foo:\n\t@echo foo\n"
    valid, errors = run_validation(mk)
    assert not valid
    types = {e.error_type for e in errors}
    assert "orphan target" in types
    assert "target without comments" in types


def test_main_reports_summary(tmp_path, monkeypatch, capsys) -> None:
    mk = "all: foo\n"
    mfile = tmp_path / "Makefile"
    mfile.write_text(mk)
    monkeypatch.setattr("sys.argv", ["profile_make_lint", "--in_filename", str(mfile)])
    ret = lint_makefile.main()
    captured = capsys.readouterr()
    assert ret == 1
    assert "validation failed" in captured.err.lower()
    assert "missing rule: 1" in captured.err.lower()


def test_summary_counts_similar_errors() -> None:
    errors = [
        lint_makefile.LintError(error_type="space instead of tab", message=""),
        lint_makefile.LintError(error_type="space instead of tab", message=""),
        lint_makefile.LintError(error_type="space instead of tab", message=""),
        lint_makefile.LintError(error_type="missing rule", message=""),
    ]
    summary = lint_makefile.summarize_errors(errors)
    assert "space instead of tab: 3" in summary
    assert "missing rule: 1" in summary


def test_multiple_targets_with_colon_warns() -> None:
    """Warn when multiple targets share a rule without grouping."""
    mk = (
        "foo bar: dep\n"
        "\t@echo hi\n"
    )
    valid, errors = run_validation(mk)
    assert not valid
    assert any(e.error_type == "multiple targets with colon" for e in errors)


def test_multiple_targets_grouped_is_ok() -> None:
    """Grouped targets using '&:' should pass validation."""
    mk = (
        "foo bar &: dep ## [FINAL] doc\n"
        "\t@echo hi\n"
        "dep: ## used\n"
        "\t@echo dep\n"
    )
    valid, errors = run_validation(mk)
    assert valid, errors


def test_directory_dependency_requires_order_only(tmp_path) -> None:
    workdir = tmp_path / "mk"
    workdir.mkdir()
    monitored_dir = workdir / "artifacts"
    monitored_dir.mkdir()
    mk = (
        "build: artifacts ## [FINAL] build app\n"
        "\t@echo hi\n"
    )
    valid, errors = run_validation(mk, root_dir=str(workdir))
    assert not valid
    assert any(
        e.error_type == "directory dependency not order-only" for e in errors
    ), errors


def test_directory_dependency_order_only_is_ok(tmp_path) -> None:
    workdir = tmp_path / "mk"
    workdir.mkdir()
    logs_dir = workdir / "logs"
    logs_dir.mkdir()
    mk = (
        "sync: | logs ## [FINAL] sync logs\n"
        "\t@echo hi\n"
    )
    valid, errors = run_validation(mk, root_dir=str(workdir))
    assert valid, errors
