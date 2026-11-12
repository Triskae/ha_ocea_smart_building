"""Tests for the per-module Ocea coverage threshold."""

from __future__ import annotations

import json

from scripts.check_ocea_coverage import main


def test_coverage_gate_requires_more_than_95_percent(tmp_path, monkeypatch, capsys):
    """Exactly 95% and missing modules fail, while 96% passes."""
    package = tmp_path / "ocea"
    package.mkdir()
    for name in ("pass.py", "equal.py", "missing.py"):
        (package / name).touch()
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({
        "files": {
            (package / "pass.py").as_posix(): {
                "summary": {"covered_lines": 96, "num_statements": 100}
            },
            (package / "equal.py").as_posix(): {
                "summary": {"covered_lines": 95, "num_statements": 100}
            },
        }
    }))
    monkeypatch.setattr("scripts.check_ocea_coverage.PACKAGE", package)

    assert main(report) == 1
    output = capsys.readouterr().out
    assert "96/100 (pass)" in output
    assert "95/100 (fail)" in output
    assert "missing from coverage report" in output


def test_coverage_gate_passes_when_all_modules_exceed_threshold(tmp_path, monkeypatch):
    package = tmp_path / "ocea"
    package.mkdir()
    (package / "client.py").touch()
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({
        "files": {
            (package / "client.py").as_posix(): {
                "summary": {"covered_lines": 20, "num_statements": 20}
            }
        }
    }))
    monkeypatch.setattr("scripts.check_ocea_coverage.PACKAGE", package)
    assert main(report) == 0


def test_coverage_gate_rejects_an_empty_package(tmp_path, monkeypatch):
    package = tmp_path / "empty"
    package.mkdir()
    report = tmp_path / "coverage.json"
    report.write_text('{"files": {}}')
    monkeypatch.setattr("scripts.check_ocea_coverage.PACKAGE", package)
    assert main(report) == 1
