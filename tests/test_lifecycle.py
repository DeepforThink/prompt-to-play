import copy
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from prompt_to_play import contracts, lifecycle


def evaluation(
    *,
    source_marker: str,
    iteration: int,
    hard_passed: bool,
    weighted_score: float,
    run_id: str = "run_001",
    world_id: str = "generated_world",
    total_ms: int = 1000,
    total_tokens: int = 100,
):
    return {
        "schema": "prompt-to-play/evaluation@1",
        "run_id": run_id,
        "world_id": world_id,
        "world_sha256": source_marker * 64,
        "iteration": iteration,
        "checks": [
            {
                "id": "scene_loads",
                "kind": "hard",
                "passed": hard_passed,
            },
            {
                "id": "visual_review",
                "kind": "soft",
                "passed": weighted_score >= 0.5,
            },
        ],
        "result": {"weighted_score": weighted_score},
        # Lifecycle selection records neither field and never treats them as a
        # pass/fail gate.  They are deliberately extreme in one test below.
        "timing_ms": {"total": total_ms},
        "tokens": {"total": total_tokens},
    }


class RequestLifecycleTests(unittest.TestCase):
    def test_same_inputs_are_byte_reproducible_and_seed_matches_world_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "refs" / "front.png"
            reference.parent.mkdir()
            reference.write_bytes(b"same image bytes")

            first = lifecycle.build_request(
                "A quiet forest ruin", ["refs/front.png"], base_dir=root
            )
            second = lifecycle.build_request(
                "A quiet forest ruin", ["refs/front.png"], base_dir=root
            )

            self.assertEqual(first, second)
            self.assertEqual(
                contracts.canonical_json_bytes(first),
                contracts.canonical_json_bytes(second),
            )
            self.assertEqual(
                first["seed"],
                contracts.derive_world_seed(
                    first["prompt"],
                    [{"sha256": first["references"][0]["sha256"]}],
                ),
            )
            semantic = {
                "prompt": first["prompt"],
                "reference_sha256": [first["references"][0]["sha256"]],
            }
            self.assertEqual(first["request_hash"], contracts.document_sha256(semantic))

    def test_different_prompt_changes_hash_and_seed(self):
        first = lifecycle.build_request("sunlit desert")
        second = lifecycle.build_request("moonlit tundra")
        self.assertNotEqual(first["request_hash"], second["request_hash"])
        self.assertNotEqual(first["seed"], second["seed"])

    def test_reference_content_changes_hash_and_seed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "view.png"
            reference.write_bytes(b"first pixels")
            first = lifecycle.build_request("ruins", [reference], base_dir=root)
            reference.write_bytes(b"different pixels")
            second = lifecycle.build_request("ruins", [reference], base_dir=root)
            self.assertNotEqual(first["request_hash"], second["request_hash"])
            self.assertNotEqual(first["seed"], second["seed"])

    def test_same_content_under_different_names_keeps_semantic_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "angle-a.png"
            second_path = root / "renamed-angle.png"
            first_path.write_bytes(b"identical pixels")
            second_path.write_bytes(b"identical pixels")
            first = lifecycle.build_request("ruins", [first_path], base_dir=root)
            second = lifecycle.build_request("ruins", [second_path], base_dir=root)

            self.assertNotEqual(first["references"][0]["name"], second["references"][0]["name"])
            self.assertEqual(first["request_hash"], second["request_hash"])
            self.assertEqual(first["seed"], second["seed"])

    def test_reference_order_changes_hash_and_seed_in_lockstep(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a = root / "a.png"
            b = root / "b.png"
            a.write_bytes(b"angle a")
            b.write_bytes(b"angle b")
            forward = lifecycle.build_request("two views", [a, b], base_dir=root)
            reversed_order = lifecycle.build_request("two views", [b, a], base_dir=root)

            self.assertNotEqual(forward["request_hash"], reversed_order["request_hash"])
            self.assertNotEqual(forward["seed"], reversed_order["seed"])

    def test_cli_writes_canonical_json_and_exposes_no_seed_argument(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "view.png"
            output = root / "request.json"
            reference.write_bytes(b"pixels")
            with redirect_stdout(StringIO()):
                result = lifecycle.main(
                    [
                        "create-request",
                        "--prompt",
                        "coastal observatory",
                        "--reference",
                        str(reference),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(result, 0)
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                output.read_bytes(), contracts.canonical_json_bytes(document) + b"\n"
            )
            forbidden_options = (
                "--seed",
                "--time-budget",
                "--token-budget",
                "--weights",
                "--threshold",
            )
            for option in forbidden_options:
                with self.subTest(option=option), redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit):
                        lifecycle.main(
                            [
                                "create-request",
                                "--prompt",
                                "coastal observatory",
                                option,
                                "7",
                                "--output",
                                str(output),
                            ]
                        )

    def test_invalid_request_inputs_are_rejected(self):
        with self.assertRaisesRegex(lifecycle.LifecycleError, "must not be empty"):
            lifecycle.build_request("   ")
        with self.assertRaisesRegex(lifecycle.LifecycleError, "does not exist"):
            lifecycle.build_request("forest", ["missing-reference.png"])


class RevisionSelectionTests(unittest.TestCase):
    def test_hard_checks_take_priority_over_weighted_score(self):
        failed_high_score = evaluation(
            source_marker="a", iteration=0, hard_passed=False, weighted_score=1.0
        )
        passed_low_score = evaluation(
            source_marker="b", iteration=1, hard_passed=True, weighted_score=0.4
        )
        selection = lifecycle.select_best_revision(
            [("failed.json", failed_high_score), ("passed.json", passed_low_score)]
        )
        self.assertEqual(selection["selected"]["source"], "passed.json")

    def test_score_decides_after_equal_hard_status_and_time_tokens_are_not_gates(self):
        fast_cheap = evaluation(
            source_marker="a",
            iteration=0,
            hard_passed=True,
            weighted_score=0.7,
            total_ms=1,
            total_tokens=1,
        )
        slow_expensive = evaluation(
            source_marker="b",
            iteration=1,
            hard_passed=True,
            weighted_score=0.9,
            total_ms=10**12,
            total_tokens=10**12,
        )
        selection = lifecycle.select_best_revision(
            [("fast.json", fast_cheap), ("slow.json", slow_expensive)]
        )
        self.assertEqual(selection["selected"]["source"], "slow.json")
        self.assertNotIn("timing_ms", selection["selected"])
        self.assertNotIn("tokens", selection["selected"])

    def test_delivery_safe_revision_outranks_structural_failure(self):
        structurally_failed = evaluation(
            source_marker="a", iteration=0, hard_passed=False, weighted_score=0.99
        )
        visual_failure = evaluation(
            source_marker="b", iteration=1, hard_passed=True, weighted_score=0.4
        )
        visual_failure["checks"].append(
            {
                "id": lifecycle.VISUAL_CHECK_ID,
                "kind": "hard",
                "passed": False,
            }
        )
        selection = lifecycle.select_best_revision(
            [
                ("structural.json", structurally_failed),
                ("visual.json", visual_failure),
            ]
        )
        self.assertEqual(selection["selected"]["source"], "visual.json")
        self.assertTrue(selection["selected"]["all_delivery_checks_passed"])

    def test_fully_passing_revision_outranks_best_effort_score(self):
        best_effort = evaluation(
            source_marker="a", iteration=0, hard_passed=True, weighted_score=0.99
        )
        passed = evaluation(
            source_marker="b", iteration=1, hard_passed=True, weighted_score=0.7
        )
        passed["result"]["passed"] = True
        selection = lifecycle.select_best_revision(
            [("best-effort.json", best_effort), ("passed.json", passed)]
        )
        self.assertEqual(selection["selected"]["source"], "passed.json")
        self.assertTrue(selection["selected"]["evaluation_passed"])

    def test_tie_break_is_stable_and_independent_of_argument_order(self):
        iteration_one = evaluation(
            source_marker="a", iteration=1, hard_passed=True, weighted_score=0.8
        )
        iteration_two = evaluation(
            source_marker="b", iteration=2, hard_passed=True, weighted_score=0.8
        )
        forward = lifecycle.select_best_revision(
            [("one.json", iteration_one), ("two.json", iteration_two)]
        )
        reverse = lifecycle.select_best_revision(
            [("two.json", iteration_two), ("one.json", iteration_one)]
        )
        self.assertEqual(forward, reverse)
        self.assertEqual(forward["selected"]["source"], "one.json")

        # Equal hard status, score, and iteration falls back to canonical digest.
        other_iteration_one = copy.deepcopy(iteration_one)
        other_iteration_one["world_sha256"] = "c" * 64
        tied = lifecycle.select_best_revision(
            [("z.json", iteration_one), ("a.json", other_iteration_one)]
        )
        expected = min(
            (
                (contracts.document_sha256(iteration_one), "z.json"),
                (contracts.document_sha256(other_iteration_one), "a.json"),
            )
        )[1]
        self.assertEqual(tied["selected"]["source"], expected)

    def test_cli_selects_and_writes_a_selection_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "revision-0.json"
            second = root / "revision-1.json"
            output = root / "selection.json"
            first.write_text(
                json.dumps(
                    evaluation(
                        source_marker="a",
                        iteration=0,
                        hard_passed=False,
                        weighted_score=0.95,
                    )
                ),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps(
                    evaluation(
                        source_marker="b",
                        iteration=1,
                        hard_passed=True,
                        weighted_score=0.75,
                    )
                ),
                encoding="utf-8",
            )
            with redirect_stdout(StringIO()):
                result = lifecycle.main(
                    ["select-best", str(first), str(second), "--output", str(output)]
                )
            self.assertEqual(result, 0)
            selection = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(selection["schema"], "prompt-to-play/selection@1")
            self.assertEqual(selection["selected"]["source"], "revision-1.json")

    def test_invalid_evaluations_are_rejected(self):
        malformed = evaluation(
            source_marker="a", iteration=0, hard_passed=True, weighted_score=0.8
        )
        malformed["result"]["weighted_score"] = float("nan")
        with self.assertRaisesRegex(lifecycle.LifecycleError, "finite number"):
            lifecycle.select_best_revision([("bad.json", malformed)])

        no_hard_checks = evaluation(
            source_marker="a", iteration=0, hard_passed=True, weighted_score=0.8
        )
        no_hard_checks["checks"][0]["kind"] = "soft"
        with self.assertRaisesRegex(lifecycle.LifecycleError, "at least one hard"):
            lifecycle.select_best_revision([("bad.json", no_hard_checks)])

        other_run = evaluation(
            source_marker="b",
            iteration=1,
            hard_passed=True,
            weighted_score=0.9,
            run_id="run_002",
        )
        valid = evaluation(
            source_marker="a", iteration=0, hard_passed=True, weighted_score=0.8
        )
        with self.assertRaisesRegex(lifecycle.LifecycleError, "same run_id and world_id"):
            lifecycle.select_best_revision(
                [("valid.json", valid), ("other.json", other_run)]
            )


if __name__ == "__main__":
    unittest.main()
