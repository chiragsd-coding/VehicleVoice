"""
Keyless evaluation harness for the VehicleVoice pipeline.

The fixture describes transcripts and expected structured outcomes. Cases execute
against ``backend.main.run_pipeline`` directly, so no web server, microphone,
LLM, STT, TTS, or external API key is needed. The harness checks NLU slots,
conversation carry-over, result filters/ranking, response text, and the
no-hallucination response invariant.

Usage from the repository root::

    python3 evaluation/evaluate.py
    python3 evaluation/evaluate.py --tests evaluation/tests.json --json

Exit status is 0 only when every case passes. A failed assertion returns 1;
invalid fixture or pipeline setup errors return 2.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Allow ``python evaluation/evaluate.py`` from the repository root (and from any
# cwd) without requiring an installed package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import main as pipeline  # noqa: E402
from services.response import format_km, format_price_inr  # noqa: E402

DEFAULT_TESTS = Path(__file__).with_name("tests.json")
_PRICE_RE = re.compile(r"₹[\d.,]+\s+lakh", re.I)
_KM_RE = re.compile(r"([\d,]+) km\b", re.I)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_RAW_LARGE_INT_RE = re.compile(r"\b\d{5,}\b")


class FixtureError(ValueError):
    """The evaluation fixture is malformed or cannot be loaded."""


def load_fixture(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"cannot load fixture {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise FixtureError("fixture must be an object with a cases array")
    for index, case in enumerate(data["cases"], start=1):
        if not isinstance(case, dict) or not case.get("id") or not isinstance(case.get("turns"), list):
            raise FixtureError(f"case {index} must have an id and turns array")
        for turn_index, turn in enumerate(case["turns"], start=1):
            if not isinstance(turn, dict) or not isinstance(turn.get("transcript"), str):
                raise FixtureError(f"case {case.get('id')} turn {turn_index} needs a transcript")
            if not isinstance(turn.get("expect"), dict):
                raise FixtureError(f"case {case.get('id')} turn {turn_index} needs expect")
    return data


def _metric() -> dict[str, int]:
    return {"passed": 0, "total": 0}


def _record_check(metrics: dict[str, dict[str, int]], name: str, passed: bool) -> None:
    metrics[name]["total"] += 1
    if passed:
        metrics[name]["passed"] += 1


def _condition(value: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if "exact" in expected and value != expected["exact"]:
            return False
        if "min" in expected and value < expected["min"]:
            return False
        if "max" in expected and value > expected["max"]:
            return False
        return True
    return value == expected


def _result_constraint_ok(vehicle: dict[str, Any], key: str, expected: Any) -> bool:
    if key == "max_price":
        return vehicle.get("price", 0) <= expected
    if key == "min_price":
        return vehicle.get("price", 0) >= expected
    if key == "verified":
        return bool(vehicle.get("verified")) is bool(expected)
    return vehicle.get(key) == expected


def _grounding_errors(spoken: str, results: list[dict[str, Any]], slots: dict[str, Any]) -> list[str]:
    """Check that response facts are traceable to returned records or query context."""
    errors: list[str] = []
    allowed_prices = {format_price_inr(v["price"]) for v in results}
    if slots.get("budget") is not None:
        # The budget is user-provided context, not a catalog fact; response.py
        # may repeat it in the intro or no-results explanation.
        allowed_prices.add(format_price_inr(slots["budget"]))
    for token in _PRICE_RE.findall(spoken):
        if token not in allowed_prices:
            errors.append(f"price {token!r} is not a result or requested budget")

    allowed_km = {format_km(v["km"]) for v in results}
    for token in _KM_RE.findall(spoken):
        if token not in allowed_km:
            errors.append(f"km {token!r} is not present in returned records")

    allowed_years = {str(v["year"]) for v in results}
    for token in _YEAR_RE.findall(spoken):
        if token not in allowed_years:
            errors.append(f"year {token!r} is not present in returned records")

    # Prices and kilometres are formatted before interpolation. This catches a
    # future template accidentally exposing raw INR or odometer integers.
    if _RAW_LARGE_INT_RE.search(spoken.replace("1st", "").replace("2nd", "").replace("3rd", "")):
        errors.append("raw large integer leaked into spoken response")

    # The current composer includes these ground-truth fields for every option.
    # Requiring them here makes the invariant useful even if a value is changed
    # to another valid-looking number by a future template.
    for vehicle in results:
        name = f"{vehicle['make']} {vehicle['model']}"
        for label, token in (
            ("vehicle name", name),
            ("year", str(vehicle["year"])),
            ("price", format_price_inr(vehicle["price"])),
            ("km", f"{format_km(vehicle['km'])} km"),
            ("fuel", str(vehicle["fuel"])),
        ):
            if token not in spoken:
                errors.append(f"returned {label} {token!r} missing from response")
    return errors


def evaluate_turn(output: dict[str, Any], expected: dict[str, Any], metrics: dict[str, dict[str, int]]) -> list[str]:
    errors: list[str] = []

    for key, wanted in expected.get("slots", {}).items():
        actual = output.get("slots", {}).get(key)
        passed = actual == wanted
        _record_check(metrics, "slot_accuracy", passed)
        if not passed:
            errors.append(f"slot {key}: expected {wanted!r}, got {actual!r}")

    if "matched_count" in expected:
        wanted = expected["matched_count"]
        actual = output.get("matched_count")
        passed = _condition(actual, wanted)
        _record_check(metrics, "result_accuracy", passed)
        if not passed:
            errors.append(f"matched_count: expected {wanted!r}, got {actual!r}")

    if "result_ids" in expected:
        wanted = expected["result_ids"]
        actual = [v.get("id") for v in output.get("results", [])]
        passed = actual == wanted
        _record_check(metrics, "result_accuracy", passed)
        if not passed:
            errors.append(f"result_ids: expected {wanted!r}, got {actual!r}")

    results = output.get("results", [])
    for key, wanted in expected.get("result_constraints", {}).items():
        failed = [v.get("id") for v in results if not _result_constraint_ok(v, key, wanted)]
        passed = not failed
        _record_check(metrics, "result_accuracy", passed)
        if not passed:
            errors.append(f"result constraint {key}={wanted!r} failed for ids {failed}")

    if "selected_vehicle" in expected:
        wanted = expected["selected_vehicle"]
        actual = output.get("selected_vehicle")
        passed = actual == wanted
        _record_check(metrics, "selection_accuracy", passed)
        if not passed:
            errors.append(f"selected_vehicle: expected {wanted!r}, got {actual!r}")

    spoken_expect = expected.get("spoken", {})
    spoken = str(output.get("spoken", ""))
    for text in spoken_expect.get("contains", []):
        passed = str(text).lower() in spoken.lower()
        _record_check(metrics, "response_accuracy", passed)
        if not passed:
            errors.append(f"spoken response missing {text!r}")
    for text in spoken_expect.get("not_contains", []):
        passed = str(text).lower() not in spoken.lower()
        _record_check(metrics, "response_accuracy", passed)
        if not passed:
            errors.append(f"spoken response unexpectedly contains {text!r}")
    if spoken_expect.get("grounded", False):
        grounding = _grounding_errors(spoken, results, output.get("slots", {}))
        passed = not grounding
        _record_check(metrics, "grounding_accuracy", passed)
        errors.extend(f"grounding: {error}" for error in grounding)

    return errors


def run_evaluation(fixture_path: Path = DEFAULT_TESTS) -> dict[str, Any]:
    """Run all fixture cases and return a JSON-serializable report."""
    fixture = load_fixture(fixture_path)
    metrics = {
        "slot_accuracy": _metric(),
        "result_accuracy": _metric(),
        "selection_accuracy": _metric(),
        "response_accuracy": _metric(),
        "grounding_accuracy": _metric(),
    }
    case_reports: list[dict[str, Any]] = []
    turn_total = 0
    turn_passed = 0

    for case in fixture["cases"]:
        session_id = f"evaluation-{case['id']}"
        pipeline._STORE.reset(session_id)
        turn_reports: list[dict[str, Any]] = []
        case_errors: list[str] = []
        try:
            for turn_number, turn in enumerate(case["turns"], start=1):
                turn_total += 1
                output = pipeline.run_pipeline(session_id, turn["transcript"])
                errors = evaluate_turn(output, turn["expect"], metrics)
                passed = not errors
                if passed:
                    turn_passed += 1
                else:
                    case_errors.extend(f"turn {turn_number}: {error}" for error in errors)
                turn_reports.append({
                    "turn": turn_number,
                    "transcript": turn["transcript"],
                    "passed": passed,
                    "matched_count": output.get("matched_count"),
                    "result_ids": [v.get("id") for v in output.get("results", [])],
                    "errors": errors,
                })
        except Exception as exc:  # report a case failure, rather than hiding it
            case_errors.append(f"pipeline error: {exc!r}")
            turn_reports.append({"passed": False, "errors": [f"pipeline error: {exc!r}"]})
        finally:
            pipeline._STORE.reset(session_id)
        case_reports.append({
            "id": case["id"],
            "passed": not case_errors,
            "turns": turn_reports,
            "errors": case_errors,
        })

    cases_passed = sum(1 for case in case_reports if case["passed"])
    return {
        "fixture": str(fixture_path),
        "catalog_assumptions": fixture.get("catalog_assumptions", {}),
        "cases": case_reports,
        "metrics": {
            "cases": {"passed": cases_passed, "total": len(case_reports)},
            "turns": {"passed": turn_passed, "total": turn_total},
            **metrics,
        },
        "passed": cases_passed == len(case_reports),
    }


def _percentage(metric: dict[str, int]) -> str:
    if not metric["total"]:
        return "n/a"
    return f"{metric['passed']}/{metric['total']} ({metric['passed'] / metric['total'] * 100:.1f}%)"


def print_report(report: dict[str, Any]) -> None:
    print("VehicleVoice evaluation")
    print(f"Fixture: {report['fixture']}")
    assumptions = report.get("catalog_assumptions", {})
    if assumptions:
        print("Catalog: " + ", ".join(f"{key}={value}" for key, value in assumptions.items()))
    print()
    for case in report["cases"]:
        status = "PASS" if case["passed"] else "FAIL"
        print(f"{status} {case['id']} ({len(case['turns'])} turn{'s' if len(case['turns']) != 1 else ''})")
        for turn in case["turns"]:
            turn_status = "PASS" if turn["passed"] else "FAIL"
            ids = ",".join(str(value) for value in turn.get("result_ids", [])) or "-"
            print(f"  {turn_status} turn {turn['turn']}: matched={turn.get('matched_count')}, top_ids=[{ids}]")
            for error in turn.get("errors", []):
                print(f"    - {error}")
    print("\nAggregate")
    metrics = report["metrics"]
    print(f"  cases:              {_percentage(metrics['cases'])}")
    print(f"  turns:              {_percentage(metrics['turns'])}")
    print(f"  slot accuracy:      {_percentage(metrics['slot_accuracy'])}")
    print(f"  result accuracy:    {_percentage(metrics['result_accuracy'])}")
    print(f"  selection accuracy: {_percentage(metrics['selection_accuracy'])}")
    print(f"  response accuracy:  {_percentage(metrics['response_accuracy'])}")
    print(f"  grounding accuracy: {_percentage(metrics['grounding_accuracy'])}")
    print(f"\nOVERALL: {'PASS' if report['passed'] else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", type=Path, default=DEFAULT_TESTS, help="JSON fixture path")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    try:
        report = run_evaluation(args.tests)
    except FixtureError as exc:
        print(f"Evaluation configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Evaluation setup error: {exc!r}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_report(report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
