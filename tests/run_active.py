"""Run current board tests on POSIX; the cancelled OAuth-v2 suite is historical."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
MODULES = (
    'test_board', 'test_board_http', 'test_board_mcp', 'test_mcp_boundaries',
    'test_board_security', 'test_board_lore', 'test_publication',
    'test_injection_advisory', 'test_advisory_integration', 'test_reference_graph',
)
suite = unittest.defaultTestLoader.loadTestsFromNames(MODULES)
if __name__ == '__main__':
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
