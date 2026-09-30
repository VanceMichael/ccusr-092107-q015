import unittest
from pathlib import Path

from src.context import load_context, load_canal, validate_canal


class ContextTest(unittest.TestCase):
    def test_fixture_matches_domain(self):
        data = load_context(Path("fixtures/context.json"))
        self.assertEqual(data["domain"], "canal-patrol-closure")
        self.assertGreaterEqual(len(data["facts"]), 1)

    def test_canal_fixture_references_are_consistent(self):
        canal = load_canal(Path("fixtures/canal.json"))
        self.assertGreaterEqual(len(canal["segments"]), 5)
        self.assertTrue(canal["devices"])
        self.assertTrue(canal["registry"])
        self.assertTrue(canal["boats"])
        self.assertTrue(canal["anchorages"])

    def test_canal_validation_rejects_broken_reference(self):
        canal = load_canal(Path("fixtures/canal.json"))
        canal["devices"][0]["segment_id"] = "SEG-X"
        with self.assertRaises(ValueError):
            validate_canal(canal)

    def test_events_fixture_is_valid_scenario(self):
        from src.replay import Replay
        result = Replay.from_files(
            Path("fixtures/canal.json"), Path("fixtures/events.json")).run()
        self.assertTrue(result["passed"], msg="样例剧本终态校验应全部通过")


if __name__ == "__main__":
    unittest.main()
