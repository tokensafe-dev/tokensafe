import unittest
from src.cache_normalizer import PromptCacheNormalizer


class TestCacheStability(unittest.TestCase):
    def test_prefix_stability_with_volatile_elements(self):
        tools = [
            {"name": "smart_read_file", "description": "Reads file"},
            {"name": "scoped_search", "description": "Searches code"},
        ]

        turn_1 = [
            {
                "role": "system",
                "content": (
                    "You are an AI coding assistant.\n"
                    "The current local time is: 2026-11-15T23:50:38+02:00.\n"
                    "Conversation ID: 5cdb1f49-1c81-415d-a09d-bb1c0de2351a\n"
                    "Always follow project guidelines.\n"
                    "⚡ **TokenSafe Turn Savings**: +1,480 tokens saved on this turn | Cumulative Total: 3.04M tokens\n"
                ),
            },
            {"role": "user", "content": "What is the project architecture?"},
            {"role": "assistant", "content": "The project uses Python."},
        ]

        turn_2 = [
            {
                "role": "system",
                "content": (
                    "You are an AI coding assistant.\n"
                    "The current local time is: 2026-11-16T00:15:10+02:00.\n"
                    "Conversation ID: 5cdb1f49-1c81-415d-a09d-bb1c0de2351a\n"
                    "Always follow project guidelines.\n"
                    "⚡ **TokenSafe Turn Savings**: +2,850 tokens saved on this turn | Cumulative Total: 3.05M tokens\n"
                ),
            },
            {"role": "user", "content": "What is the project architecture?"},
            {"role": "assistant", "content": "The project uses Python."},
            {"role": "user", "content": "Show me the database models."},
        ]

        is_stable = PromptCacheNormalizer.validate_prefix_stability(turn_1, turn_2, tools=tools)
        self.assertTrue(is_stable, "Static prefix hash should be identical despite different timestamps and savings counters.")

    def test_prefix_stability_breaks_on_real_rule_change(self):
        turn_1 = [
            {"role": "system", "content": "Rule A: always write tests."},
            {"role": "user", "content": "Hello"},
        ]
        turn_2 = [
            {"role": "system", "content": "Rule A: always write tests.\nRule B: never delete files."},
            {"role": "user", "content": "Hello"},
        ]
        is_stable = PromptCacheNormalizer.validate_prefix_stability(turn_1, turn_2)
        self.assertFalse(is_stable, "Real instruction modifications should produce a different prefix hash.")


if __name__ == "__main__":
    unittest.main()
