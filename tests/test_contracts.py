import copy
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from prompt_to_play import contracts


ROOT = Path(__file__).resolve().parents[1]
PROMPT_TO_PLAY = ROOT / "prompt_to_play"
EXAMPLES = PROMPT_TO_PLAY / "examples"


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_example(name: str):
    return read_json(EXAMPLES / name)


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = read_example("world.json")
        cls.forest = read_example("forest_world.json")
        cls.patch = read_example("patch.json")
        cls.policy = read_json(PROMPT_TO_PLAY / "evaluation_policy.json")
        cls.resolved = contracts.apply_patch(cls.world, cls.patch)
        cls.evaluation = read_example("evaluation.json")

    def test_examples_have_synchronized_hashes(self):
        self.assertEqual(self.patch["base_world_sha256"], contracts.document_sha256(self.world))
        self.assertEqual(
            self.evaluation["world_sha256"],
            contracts.document_sha256(self.resolved),
        )

    def test_world_seed_is_system_derived_and_stable(self):
        contracts.validate_world(self.world)
        brief = self.world["brief"]
        self.assertEqual(
            self.world["seed"],
            contracts.derive_world_seed(brief["text"], brief["references"]),
        )
        self.assertEqual(
            contracts.derive_seed(self.world["seed"], "core_a"),
            contracts.derive_seed(self.world["seed"], "core_a"),
        )
        self.assertNotEqual(
            contracts.derive_seed(self.world["seed"], "core_a"),
            contracts.derive_seed(self.world["seed"], "core_b"),
        )

    def test_forest_world_proves_schema_is_not_mechanical_city_specific(self):
        contracts.validate_world(self.forest)
        self.assertEqual(self.forest["buildings"], [])
        self.assertEqual(
            {item["action"] for item in self.forest["interactions"]["interactables"]},
            {"inspect", "repair"},
        )
        self.assertEqual(len(self.forest["interactions"]["interactables"]), 2)
        ids = {item["id"] for item in self.forest["interactions"]["interactables"]}
        self.assertFalse(any("core" in item_id for item_id in ids))

    def test_world_rejects_user_budget_or_evaluation_policy(self):
        for forbidden_key in ("budget", "evaluation"):
            with self.subTest(forbidden_key=forbidden_key):
                world = copy.deepcopy(self.world)
                world[forbidden_key] = {}
                with self.assertRaisesRegex(contracts.ContractError, "unknown keys"):
                    contracts.validate_world(world)

    def test_world_rejects_non_derived_seed(self):
        world = copy.deepcopy(self.world)
        world["seed"] += 1
        with self.assertRaisesRegex(contracts.ContractError, "system-derived seed"):
            contracts.validate_world(world)

    def test_world_rejects_unsafe_asset_path(self):
        world = copy.deepcopy(self.world)
        world["props"][0]["prefab"] = "../secret.tscn"
        with self.assertRaisesRegex(contracts.ContractError, "must not escape"):
            contracts.validate_world(world)

    def test_world_rejects_dangling_road_region(self):
        world = copy.deepcopy(self.world)
        world["roads"][0]["to"] = "missing"
        with self.assertRaisesRegex(contracts.ContractError, "unknown region"):
            contracts.validate_world(world)

    def test_objectives_and_exit_have_checked_cross_references(self):
        world = copy.deepcopy(self.world)
        world["interactions"]["objectives"][0]["targets"] = ["missing"]
        with self.assertRaisesRegex(contracts.ContractError, "unknown interactable"):
            contracts.validate_world(world)
        world = copy.deepcopy(self.world)
        world["interactions"]["exit"]["requires"] = ["core_a"]
        with self.assertRaisesRegex(contracts.ContractError, "unknown objective"):
            contracts.validate_world(world)

    def test_style_palette_requires_exact_uppercase_colors(self):
        world = copy.deepcopy(self.world)
        world["style"]["palette"]["sky"] = "#9db7c9"
        with self.assertRaisesRegex(contracts.ContractError, "#RRGGBB"):
            contracts.validate_world(world)

    def test_policy_requires_exact_course_metrics(self):
        policy = copy.deepcopy(self.policy)
        policy["weights"]["visual"] = policy["weights"].pop("scene_similarity")
        with self.assertRaisesRegex(contracts.ContractError, "scene_similarity"):
            contracts.validate_evaluation_policy(policy)

    def test_patch_applies_by_stable_id_without_mutating_source(self):
        source = copy.deepcopy(self.world)
        resolved = contracts.apply_patch(source, self.patch)
        target = next(
            item
            for item in resolved["interactions"]["interactables"]
            if item["id"] == "core_c"
        )
        camera = next(item for item in resolved["cameras"] if item["id"] == "overview")
        self.assertEqual(target["position"], [65, 3, 7])
        self.assertEqual(camera["position"], [25, 62, 82])
        self.assertEqual(source, self.world)

    def test_patch_rejects_stale_hash(self):
        patch = copy.deepcopy(self.patch)
        patch["base_world_sha256"] = "0" * 64
        with self.assertRaisesRegex(contracts.ContractError, "stale patch"):
            contracts.validate_patch(patch, self.world)

    def test_patch_rejects_non_whitelisted_field(self):
        patch = copy.deepcopy(self.patch)
        patch["operations"][0]["changes"] = {"seed": 7}
        with self.assertRaisesRegex(contracts.ContractError, "not patchable"):
            contracts.validate_patch(patch, self.world)

    def test_evaluation_recomputes_score_and_gates_from_policy(self):
        contracts.validate_evaluation(self.evaluation, self.resolved, self.policy)
        report = copy.deepcopy(self.evaluation)
        report["result"]["weighted_score"] = 0.93
        with self.assertRaisesRegex(contracts.ContractError, "recomputed score"):
            contracts.validate_evaluation(report, self.resolved, self.policy)

    def test_evaluation_enforces_hard_gate(self):
        report = copy.deepcopy(self.evaluation)
        report["checks"][2]["passed"] = False
        with self.assertRaisesRegex(contracts.ContractError, "hard check failed"):
            contracts.validate_evaluation(report, self.resolved, self.policy)

    def test_slow_generation_is_scored_not_a_hard_budget_failure(self):
        report = copy.deepcopy(self.evaluation)
        report["timing_ms"]["plan"] += 69300
        report["timing_ms"]["total"] = 120000
        report["metrics"]["generation_speed"] = 0.5
        report["result"]["weighted_score"] = 0.89
        contracts.validate_evaluation(report, self.resolved, self.policy)
        self.assertTrue(report["result"]["passed"])

    def test_high_token_use_is_scored_not_a_hard_budget_failure(self):
        report = copy.deepcopy(self.evaluation)
        report["tokens"].update({"input": 11000, "output": 1000, "total": 12000})
        report["metrics"]["token_efficiency"] = 0.5
        report["result"]["weighted_score"] = 0.89
        contracts.validate_evaluation(report, self.resolved, self.policy)
        self.assertTrue(report["result"]["passed"])

    def test_evaluation_enforces_correction_limit(self):
        report = copy.deepcopy(self.evaluation)
        report["iteration"] = 3
        with self.assertRaisesRegex(contracts.ContractError, "max_correction_iterations"):
            contracts.validate_evaluation(report, self.resolved, self.policy)

    def test_cli_apply_and_validate(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            world_path = temp / "world.json"
            patch_path = temp / "patch.json"
            policy_path = temp / "policy.json"
            evaluation_path = temp / "evaluation.json"
            output_path = temp / "resolved.json"
            documents = (
                (world_path, self.world),
                (patch_path, self.patch),
                (policy_path, self.policy),
                (evaluation_path, self.evaluation),
            )
            for path, document in documents:
                path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    contracts.main(["apply", str(world_path), str(patch_path), str(output_path)]),
                    0,
                )
                self.assertEqual(contracts.main(["validate-world", str(output_path)]), 0)
                self.assertEqual(
                    contracts.main(
                        [
                            "validate-eval",
                            str(evaluation_path),
                            "--world",
                            str(output_path),
                            "--policy",
                            str(policy_path),
                        ]
                    ),
                    0,
                )
            self.assertTrue(output_path.is_file())


if __name__ == "__main__":
    unittest.main()
