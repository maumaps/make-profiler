import json

from make_profiler import report_export


def _export_single_report(tmp_path, monkeypatch, record: dict[str, object]) -> dict:
    monkeypatch.chdir(tmp_path)
    report_export.status.clear()
    report_export.status_list.clear()

    report_export.export_report(
        {"build": record},
        {"build": "Build target"},
        {"build"},
    )

    return json.loads((tmp_path / "report.json").read_text())


def test_export_report_normalizes_missing_log_values(tmp_path, monkeypatch) -> None:
    """Keep report consumers seeing string paths when a log key is null."""
    report = _export_single_report(
        tmp_path,
        monkeypatch,
        {
            "running": False,
            "failed": False,
            "done": True,
            "finish_prev": 1,
            "log": None,
        },
    )

    assert (
        report["status"][0]["targetLog"] == ""
    ), "targetLog should stay a string when the performance record stores log=None"


def test_export_report_handles_missing_log_key(tmp_path, monkeypatch) -> None:
    """Keep targetLog empty when the performance record omits the log key."""
    report = _export_single_report(
        tmp_path,
        monkeypatch,
        {
            "running": False,
            "failed": False,
            "done": True,
            "finish_prev": 1,
        },
    )

    assert (
        report["status"][0]["targetLog"] == ""
    ), "targetLog should stay a string when the performance record omits log"
