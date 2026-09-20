import unittest
from pathlib import Path

from src.context import load_context


class ContextTest(unittest.TestCase):
    def test_fixture_matches_domain(self):
        data = load_context(Path("fixtures/context.json"))
        self.assertEqual(data["domain"], "canal-patrol-closure")
        self.assertGreaterEqual(len(data["facts"]), 1)


if __name__ == "__main__":
    unittest.main()
