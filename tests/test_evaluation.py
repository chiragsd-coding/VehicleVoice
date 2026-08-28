"""Tests for the keyless evaluation harness and its CI failure contract."""
import json
from pathlib import Path

from evaluation.evaluate import DEFAULT_TESTS, main, run_evaluation


def test_evaluation_fixture_passes_against_current_pipeline():
    report = run_evaluation(DEFAULT_TESTS)

    assert report["passed"] is True
    assert report["metrics"]["cases"] == {"passed": 7, "total": 7}
    assert report["metrics"]["turns"] == {"passed": 8, "total": 8}
    for name in (
        "slot_accuracy",
        "result_accuracy",
        "selection_accuracy",
        "response_accuracy",
        "grounding_accuracy",
    ):
        assert report["metrics"][name]["passed"] == report["metrics"][name]["total"]


def test_evaluation_cli_returns_one_for_failed_assertion(tmp_path, capsys):
    fixture = json.loads(DEFAULT_TESTS.read_text(encoding="utf-8"))
    fixture["cases"][0]["turns"][0]["expect"]["matched_count"] = {"exact": 999}
    bad_fixture = Path(tmp_path) / "bad-tests.json"
    bad_fixture.write_text(json.dumps(fixture), encoding="utf-8")

    assert main(["--tests", str(bad_fixture), "--json"]) == 1
    assert '"passed": false' in capsys.readouterr().out
