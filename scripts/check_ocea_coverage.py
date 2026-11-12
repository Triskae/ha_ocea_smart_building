"""Verify that each Ocea client module has strictly more than 95% coverage."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PACKAGE = Path("custom_components/ocea_smart_building/ocea")


def main(report_path: Path) -> int:
    """Report each module's exact line coverage and fail below the threshold."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = report["files"]
    paths = sorted(PACKAGE.glob("*.py"))
    if not paths:
        print(f"No Python modules found in {PACKAGE}")
        return 1
    failed = False
    for path in paths:
        name = path.as_posix()
        if name not in files:
            print(f"{name}: missing from coverage report")
            failed = True
            continue
        summary = files[name]["summary"]
        covered = summary["covered_lines"]
        total = summary["num_statements"]
        passed = total > 0 and covered * 20 > total * 19
        print(f"{name}: {covered}/{total} ({'pass' if passed else 'fail'})")
        failed |= not passed
    return int(failed)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: check_ocea_coverage.py COVERAGE_JSON")
    raise SystemExit(main(Path(sys.argv[1])))
