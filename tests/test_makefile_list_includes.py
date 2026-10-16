import os
import shutil
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from make_profiler import __main__ as make_profiler_main
from make_profiler.parser import Tokens, parse
from make_profiler.preprocess import generate_makefile


def write_issue_fixture(tmp_path):
    makefile = tmp_path / "Makefile"
    included_dir = tmp_path / "dir"
    included_dir.mkdir()
    included_makefile = included_dir / "Makefile2"

    makefile.write_text(
        "dir_self := $(dir $(lastword $(MAKEFILE_LIST)))\n"
        "include $(dir_self)/dir/Makefile2\n"
    )
    included_makefile.write_text("target:\n\techo Making target\n")

    return makefile


def disable_report_generation(monkeypatch):
    monkeypatch.setattr(make_profiler_main, "render_dot", lambda *_: None)
    monkeypatch.setattr(make_profiler_main, "export_report", lambda *_: None)


def test_profiles_target_from_makefile_list_include(tmp_path, monkeypatch, capfd):
    makefile = write_issue_fixture(tmp_path)
    database = tmp_path / "make_profile.db"
    disable_report_generation(monkeypatch)
    monkeypatch.chdir(tmp_path)

    make_profiler_main.main(["-f", str(makefile), "-db", str(database), "target"])

    stdout, _ = capfd.readouterr()
    assert "Making target" in stdout, (
        "The issue #7 fixture must execute the included target recipe while profiling. "
        f"Captured stdout was: {stdout!r}"
    )
    assert database.exists(), "Profiling the included target must create its timing database."
    assert "start target" in database.read_text(), (
        "The target must be instrumented, not merely delegated to the original Makefile."
    )


def test_missing_include_fails_loudly(tmp_path, monkeypatch):
    makefile = tmp_path / "Makefile"
    makefile.write_text("include missing/Makefile\n")
    disable_report_generation(monkeypatch)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(subprocess.CalledProcessError) as error:
        make_profiler_main.main(["-f", str(makefile), "target"])

    assert "missing/Makefile" in error.value.output, (
        "A required missing include must retain GNU Make's filename diagnostic; "
        f"output was: {error.value.output!r}"
    )


@pytest.mark.parametrize("absolute", (False, True))
def test_parser_and_preprocessor_support_ordinary_includes(tmp_path, monkeypatch, absolute):
    included_dir = tmp_path / "dir"
    included_dir.mkdir()
    included_makefile = included_dir / "Makefile2"
    included_makefile.write_text("target:\n\techo ordinary include\n")
    include_path = str(included_makefile) if absolute else "dir/Makefile2"
    makefile = tmp_path / "Makefile"
    makefile.write_text(f"include {include_path}\n")
    monkeypatch.chdir(tmp_path)

    with makefile.open() as source:
        ast = parse(source)
    generated = StringIO()
    generate_makefile(ast, generated, str(tmp_path / "make_profile.db"))

    targets = [data["target"] for token, data in ast if token == Tokens.target]
    assert targets == ["target"], (
        "The parser must retain the included target for "
        f"{'absolute' if absolute else 'relative'} includes; got {targets!r}."
    )
    assert "target:" in generated.getvalue(), (
        "The preprocessor must emit the included target after GNU Make resolves "
        f"the {'absolute' if absolute else 'relative'} include."
    )


def test_profiles_makefile_without_makefile_list(tmp_path, monkeypatch, capfd):
    makefile = tmp_path / "Makefile"
    makefile.write_text("target:\n\techo plain target\n")
    disable_report_generation(monkeypatch)
    monkeypatch.chdir(tmp_path)

    make_profiler_main.main(["target"])

    stdout, _ = capfd.readouterr()
    assert "plain target" in stdout, (
        "A Makefile without include directives or MAKEFILE_LIST must still profile "
        f"its target; captured stdout was: {stdout!r}"
    )


@pytest.mark.parametrize(
    ("goals", "expected_output", "expected_records"),
    (
        (("--dash-target",), "dash target", ("--dash-target",)),
        (("-db",), "dash profiler option", ("-db",)),
        (("-C",), "dash directory option", ("-C",)),
        (("-C", "next"), "next target", ("-C", "next")),
    ),
)
def test_profile_make_preserves_goals_after_separator(
    tmp_path,
    goals,
    expected_output,
    expected_records,
):
    if shutil.which("dot") is None or shutil.which("unflatten") is None:
        pytest.skip("Graphviz is required for the end-to-end profile_make run")
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "--dash-target:\n"
        "\t@echo dash target\n"
        "-db:\n"
        "\t@echo dash profiler option\n"
        "-C:\n"
        "\t@echo dash directory option\n"
        "next:\n"
        "\t@echo next target\n"
    )
    environment = os.environ.copy()
    project_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (project_root, environment.get("PYTHONPATH")))
    )

    plain_make = subprocess.run(
        ["make", "--", *goals],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    result = subprocess.run(
        [sys.executable, "-m", "make_profiler", "-p", str(tmp_path / "out.svg"), "--", *goals],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )

    output = result.stdout + result.stderr
    expected_output_from_make = plain_make.stdout + plain_make.stderr
    assert plain_make.returncode == 0, (
        "The GNU Make control command must accept the exact target sequence after --; "
        f"goals were {goals!r}, output was: {expected_output_from_make!r}"
    )
    assert result.returncode == 0, (
        "profile_make must pass a dash-leading target after -- through every Make pass; "
        f"goals were {goals!r}, output was: {output!r}"
    )
    plain_output_lines = plain_make.stdout.splitlines()
    assert all(line in result.stdout.splitlines() for line in plain_output_lines), (
        "profile_make must preserve GNU Make's target output after --; "
        f"goals were {goals!r}, profile output was {result.stdout!r}, "
        f"plain Make output was {plain_make.stdout!r}"
    )
    assert expected_output in output, (
        "The exact target after -- must execute instead of being consumed as a profiler option; "
        f"goals were {goals!r}, output was: {output!r}"
    )
    database = (tmp_path / "make_profile.db").read_text()
    for target in expected_records:
        assert f"start {target}" in database, (
            "Every target after -- must be profiled by GNU Make unchanged; "
            f"target was {target!r}, database was: {database!r}"
        )


def test_include_keeps_make_parse_order(tmp_path, monkeypatch, capfd):
    makefile = tmp_path / "Makefile"
    included_makefile = tmp_path / "included.mk"
    makefile.write_text(
        "VALUE := before\n"
        "include included.mk\n"
        "VALUE := after\n"
    )
    included_makefile.write_text("target:\n\techo $(VALUE)\n")
    disable_report_generation(monkeypatch)
    monkeypatch.chdir(tmp_path)

    make_profiler_main.main(["-f", str(makefile), "target"])

    stdout, _ = capfd.readouterr()
    assert "after" in stdout and "before" not in stdout, (
        "An included recipe must retain GNU Make's parse-time variable order; "
        f"captured stdout was: {stdout!r}"
    )

    with makefile.open() as source:
        ast = parse(source)
    generated = StringIO()
    generate_makefile(ast, generated, str(tmp_path / "make_profile.db"))
    assert "VALUE := after" in generated.getvalue(), (
        "The preprocessor must retain GNU Make's final variable state for included recipes."
    )


def test_remade_include_uses_restarted_makefile_state(tmp_path, monkeypatch, capfd):
    makefile = tmp_path / "Makefile"
    database = tmp_path / "make_profile.db"
    makefile.write_text(
        "include generated.mk\n"
        "generated.mk:\n"
        "\tprintf 'target:\\n\\techo rebuilt include\\n' > $@\n"
    )
    disable_report_generation(monkeypatch)
    monkeypatch.chdir(tmp_path)

    make_profiler_main.main(["-f", str(makefile), "-db", str(database), "target"])

    stdout, _ = capfd.readouterr()
    assert "rebuilt include" in stdout, (
        "A makefile rebuilt during include processing must be used after GNU Make restarts; "
        f"captured stdout was: {stdout!r}"
    )
    assert "start target" in database.read_text(), (
        "The restarted Makefile state must be profiled for the requested target."
    )


@pytest.mark.parametrize(
    ("makefile_name", "makefile_text", "arguments", "expected"),
    (
        (
            "Makefile",
            "include $(INC_DIR)/included.mk\n",
            ("INC_DIR=dir", "target"),
            "variable assignment include",
        ),
        (
            "Makefile",
            "include included.mk\n",
            ("-I", "dir", "target"),
            "include search path",
        ),
        (
            "Makefile",
            "include $(INC_DIR)/included.mk\n",
            ("--eval=INC_DIR:=dir", "target"),
            "evaluated variable include",
        ),
        (
            "project/Makefile",
            "include dir/included.mk\n",
            ("-C", "project", "-f", "Makefile", "target"),
            "changed directory include",
        ),
    ),
)
def test_profile_honors_make_arguments(
    tmp_path,
    monkeypatch,
    capfd,
    makefile_name,
    makefile_text,
    arguments,
    expected,
):
    makefile = tmp_path / makefile_name
    makefile.parent.mkdir(parents=True, exist_ok=True)
    included_dir = makefile.parent / "dir"
    included_dir.mkdir()
    (included_dir / "included.mk").write_text(f"target:\n\techo {expected}\n")
    makefile.write_text(makefile_text)
    captured_performance = {}
    disable_report_generation(monkeypatch)
    monkeypatch.setattr(
        make_profiler_main,
        "export_dot",
        lambda _, __, ___, ____, performance, _____, ______: captured_performance.update(performance),
    )
    monkeypatch.chdir(tmp_path)

    if "-f" in arguments:
        command = list(arguments)
    else:
        command = ["-f", str(makefile), *arguments]
    make_profiler_main.main(command)

    stdout, _ = capfd.readouterr()
    assert expected in stdout, (
        "GNU Make options and variable assignments that choose an include must be passed "
        f"to profiling unchanged; captured stdout was: {stdout!r}"
    )
    if "-C" in arguments:
        database = tmp_path / "project" / "make_profile.db"
        assert database.exists() and "start target" in database.read_text(), (
            "A relative timing database must follow GNU Make's -C working directory."
        )
        assert "target" in captured_performance, (
            "The post-run report must parse the timing database from GNU Make's -C directory; "
            f"captured performance was: {captured_performance!r}"
        )


def test_included_target_description_is_preserved_in_graph_data(tmp_path, monkeypatch):
    makefile = tmp_path / "Makefile"
    included_makefile = tmp_path / "included.mk"
    captured_docs = {}
    makefile.write_text("include included.mk\n")
    included_makefile.write_text("target: ## shown in report\n")
    disable_report_generation(monkeypatch)
    monkeypatch.setattr(
        make_profiler_main,
        "export_dot",
        lambda _, __, ___, ____, _____, ______, docs: captured_docs.update(docs),
    )
    monkeypatch.chdir(tmp_path)

    make_profiler_main.main(["-f", "Makefile"])

    assert captured_docs["target"] == "shown in report", (
        "A documented target from an included Makefile must retain its description in graph data; "
        f"captured docs were: {captured_docs!r}"
    )


def test_paths_with_spaces_are_profiled(tmp_path, monkeypatch, capfd):
    project = tmp_path / "project with spaces"
    included_dir = project / "dir with spaces"
    included_dir.mkdir(parents=True)
    makefile = project / "Makefile"
    included_makefile = included_dir / "included.mk"
    include_path = str(included_makefile).replace(" ", "\\ ")
    makefile.write_text(f"include {include_path}\n")
    included_makefile.write_text("target:\n\techo spaced path\n")
    disable_report_generation(monkeypatch)
    monkeypatch.chdir(project)

    make_profiler_main.main(["-f", str(makefile), "target"])

    stdout, _ = capfd.readouterr()
    assert "spaced path" in stdout, (
        "A Makefile and include directory with spaces must retain their literal paths; "
        f"captured stdout was: {stdout!r}"
    )


def test_recipe_text_starting_with_include_is_not_an_include_directive(tmp_path, monkeypatch, capfd):
    makefile = tmp_path / "Makefile"
    makefile.write_text("target:\n\tprintf '%s\\n' 'include inside recipe'\n")
    disable_report_generation(monkeypatch)
    monkeypatch.chdir(tmp_path)

    with makefile.open() as source:
        ast = parse(source)
    make_profiler_main.main(["-f", str(makefile), "target"])

    stdout, _ = capfd.readouterr()
    target = next(data for token, data in ast if token == Tokens.target)
    assert target["body"] == [(Tokens.command, "printf '%s\\n' 'include inside recipe'")], (
        "A tabbed recipe line containing include must stay recipe text, not trigger include parsing."
    )
    assert "include inside recipe" in stdout, (
        "The original tabbed recipe must still execute; captured stdout was: "
        f"{stdout!r}"
    )


def test_failed_target_propagates_make_failure(tmp_path, monkeypatch):
    makefile = tmp_path / "Makefile"
    makefile.write_text("target:\n\tfalse\n")
    disable_report_generation(monkeypatch)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(subprocess.CalledProcessError) as error:
        make_profiler_main.main(["-f", str(makefile), "target"])

    assert error.value.returncode != 0, (
        "A failing target must preserve its non-zero GNU Make exit status."
    )
