"""Retired lore cannot fetch posts, accept summaries, or publish state."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('accept_lore', ROOT / 'scripts/accept_lore.py')
accept = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accept)


class LoreRetirementTests(unittest.TestCase):
    def test_even_reviewed_requests_are_rejected_without_side_effects(self):
        for args in [['accept_lore.py', '1'], ['accept_lore.py', '1', '--reviewed']]:
            with patch('sys.argv', args), patch.object(accept, 'fetch') as fetch, \
                 patch.object(accept.subprocess, 'run') as process:
                with self.assertRaisesRegex(SystemExit, 'Lore is retired'):
                    accept.main()
                fetch.assert_not_called()
                process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
