import os
import subprocess

import pytest

from make_profiler import __main__ as make_profiler_main
from make_profiler.make_args import make_environment, makefile_arguments, scan_make_options
from make_profiler.parser import Tokens, included_makefiles, parse_makefile
from make_profiler.timing import parse_timing_db


def no_report(monkeypatch):
    monkeypatch.setattr(make_profiler_main, "render_dot", lambda *_: None)
    monkeypatch.setattr(make_profiler_main, "export_report", lambda *_: None)


@pytest.mark.parametrize(
    ("make_args", "goals", "dry_run", "jobs"),
    (
        (["-j", "4"], [], False, True),
        (["-j", "all"], ["all"], False, True),
        (["-j1", "all"], ["all"], False, False),
        (["--jobs", "4", "all"], ["all"], False, True),
        (["-l", "2.5", "all"], ["all"], False, False),
        (["-o", "old-output"], [], False, False),
        (["-W", "changed.c", "all"], ["all"], False, False),
        (["--old-file", "x", "--what-if=y"], [], False, False),
        (["-kC", "dir", "all"], ["all"], False, False),
        (["-n", "all"], ["all"], True, False),
        (["-kn", "all"], ["all"], True, False),
        (["--just-print"], [], True, False),
        (["CC=gcc", "-s"], [], False, False),
        (["--", "-j", "4"], ["-j", "4"], False, False),
    ),
)
def test_scan_make_options(make_args, goals, dry_run, jobs):
    options = scan_make_options(make_args)
    assert options == {"goals": goals, "dry_run": dry_run, "jobs": jobs}, (
        f"GNU Make arguments {make_args!r} must be classified as goals={goals!r}, "
        f"dry_run={dry_run!r}, jobs={jobs!r}; got {options!r}"
    )


@pytest.mark.parametrize(
    "make_args",
    (["--file", "Alt.mk"], ["--makefile", "Alt.mk"], ["--file=Alt.mk"], ["--makefile=Alt.mk"], ["-fAlt.mk"], ["-f", "Alt.mk"]),
)
def test_makefile_arguments_cover_every_gnu_make_spelling(make_args):
    assert makefile_arguments(make_args) == ["Alt.mk"], (
        f"Every spelling of the Makefile option must resolve to Alt.mk; arguments were {make_args!r}"
    )


def test_recipeless_rule_resolves_docs_from_long_file_option(tmp_path, monkeypatch):
    (tmp_path / "Alt.mk").write_text("all: dep ## Everything\ndep:\n\t@true\n")
    monkeypatch.chdir(tmp_path)

    ast = parse_makefile("Makefile", ["--file", "Alt.mk"])

    docs = {data["target"]: data["docs"] for token, data in ast if token == Tokens.target}
    assert docs.get("all") == "Everything", (
        f"A recipe-less rule must take its description from the --file Makefile; got {docs!r}"
    )


def test_grouped_target_is_one_entry_with_its_recipe(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text("a b &: c | d ## Group\n\ttouch a b\nc d:\n\t@touch $@\n")
    monkeypatch.chdir(tmp_path)

    ast = parse_makefile("Makefile")

    groups = [data for token, data in ast if token == Tokens.target and data["grouped"]]
    assert len(groups) == 1, f"A grouped rule must appear once in the AST; got {groups!r}"
    assert groups[0]["all_targets"] == ["a", "b"], f"Group members were lost: {groups[0]!r}"
    assert groups[0]["deps"] == [["c"], ["d"]], f"Order-only dependencies must be kept: {groups[0]!r}"
    assert groups[0]["body"], f"The group's recipe must survive merging member records: {groups[0]!r}"
    assert groups[0]["docs"] == "Group", f"The group's description must survive: {groups[0]!r}"


def test_parsing_is_independent_of_the_users_locale(tmp_path, monkeypatch):
    (tmp_path / "included.mk").write_text("target:\n\t@true\n")
    (tmp_path / "Makefile").write_text("include included.mk\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    monkeypatch.setenv("LANGUAGE", "ru")

    assert make_environment()["LC_ALL"] == "C", "The database probe must run Make with untranslated messages."
    environment = make_environment(keep_recipe_locale=True)
    assert environment["LC_MESSAGES"] == "C" and "LANGUAGE" not in environment and "LC_ALL" not in environment, (
        f"A profiled build must only switch Make's messages to C; got {environment!r}"
    )
    targets = [d["target"] for t, d in parse_makefile("Makefile") if t == Tokens.target]
    assert targets == ["target"], f"Included rules must be found under a Russian locale; got {targets!r}"


def test_makefile_list_is_split_without_probing_every_prefix(tmp_path, monkeypatch):
    names = [f"m{index}.mk" for index in range(50)] + ["with space.mk"]
    for name in names:
        (tmp_path / name).write_text("")
    probes = []
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda path: probes.append(path) or real_isfile(path))
    value = " ".join(name.replace(" ", "\\ ") for name in names)

    found = included_makefiles(f"MAKEFILE_LIST := {value}\n", str(tmp_path))

    assert [os.path.basename(path) for path in found] == names, f"Entries were mis-split: {found!r}"
    assert len(probes) == len(names), (
        f"MAKEFILE_LIST must need one filesystem probe per entry, not one per prefix; probes: {len(probes)}"
    )


def test_failed_build_keeps_logs_failure_marker_and_make_status(tmp_path, monkeypatch, capfd):
    (tmp_path / "Makefile").write_text("ok:\n\t@echo fine\nbad: ok\n\t@echo breaking; false\n")
    no_report(monkeypatch)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        make_profiler_main.cli(["bad"])

    assert exit_info.value.code == 2, "profile_make must exit with GNU Make's own failure status (2)."
    records = [line.split() for line in (tmp_path / "make_profile.db").read_text().splitlines()]
    events = [(record[2], record[3]) for record in records]
    assert events == [("start", "ok"), ("finish", "ok"), ("start", "bad"), ("finish", "bad")], (
        f"Each target needs start/finish, including the failed one; got {events!r}"
    )
    run = records[0][1]
    assert "fine" in (tmp_path / "logs" / run / "ok" / "log.txt").read_text(), "Per-target log.txt is missing."
    assert (tmp_path / "logs" / run / "bad" / "failed.touch").exists(), "The failed target needs failed.touch."
    assert not (tmp_path / "logs" / run / "ok" / "failed.touch").exists(), "A successful target must not be marked failed."
    timing = parse_timing_db(str(tmp_path / "make_profile.db"), directory=str(tmp_path))
    assert timing["bad"]["failed"] and not timing["bad"]["running"], f"Report state was {timing['bad']!r}"
    assert timing["ok"]["log"] == f"logs/{run}/ok/log.txt", f"Report log link was {timing['ok']!r}"


def test_phony_target_without_update_prefix_is_recorded(tmp_path):
    match = make_profiler_main.TRACE_TARGET.match("Makefile:3: target 'all' does not exist\n")
    assert match and match.group(1) == "all", "GNU Make 4.3's file-less target trace form must start a record."
    match = make_profiler_main.TRACE_TARGET.match("Makefile:9: update target 'z' due to: p\n")
    assert match and match.group(1) == "z", "GNU Make 4.4's trace form must start a record."


def test_dry_run_does_not_write_timing_records(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text("target:\n\t@echo hello\n")
    no_report(monkeypatch)
    monkeypatch.chdir(tmp_path)

    make_profiler_main.main(["-n", "target"])

    assert not (tmp_path / "make_profile.db").exists(), "A dry run executes nothing and must not be recorded."
    assert not (tmp_path / "logs").exists(), "A dry run must not create logs."


def test_parallel_build_warns_and_closes_targets_at_exit(tmp_path, monkeypatch, caplog):
    (tmp_path / "Makefile").write_text("all: a b\na:\n\t@sleep 0.2\nb:\n\t@sleep 0.2\n")
    no_report(monkeypatch)
    monkeypatch.chdir(tmp_path)

    make_profiler_main.main(["-j", "2", "all"])

    assert "approximate" in caplog.text, "A -j build must warn that trace timings are approximate."
    events = [line.split()[2:] for line in (tmp_path / "make_profile.db").read_text().splitlines()]
    for target in ("a", "b"):
        assert ["start", target] in events and ["finish", target] in events, (
            f"Every concurrent target needs its own start and finish; got {events!r}"
        )
    first_finish = next(i for i, e in enumerate(events) if e[0] == "finish")
    assert all(e[0] == "start" for e in events[:first_finish]) and first_finish >= 2, (
        f"A target must not be finished just because another one started; got {events!r}"
    )


def test_report_resolves_targets_relative_to_dash_c(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "Makefile").write_text("artifact:\n\t@touch artifact\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "stray").write_text("")
    no_report(monkeypatch)
    monkeypatch.chdir(elsewhere)

    make_profiler_main.main(["-C", str(project), "artifact"])

    timing = parse_timing_db(str(project / "make_profile.db"), directory=str(project))
    assert timing["artifact"]["done"], f"A file built under -C must be reported as existing; got {timing!r}"
    unrelated = parse_timing_db(str(project / "make_profile.db"), directory=str(elsewhere))
    assert not unrelated["artifact"]["done"], "The caller's directory must not be consulted for -C builds."
