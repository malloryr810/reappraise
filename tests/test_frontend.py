"""Runs the frontend's JavaScript tests (tests/frontend/*.test.mjs) with `node --test`.

Node is required, not optional: a missing node fails this test rather than
skipping it, so "all passed" always includes the frontend.
"""

import shutil
import subprocess
from pathlib import Path

_JS_TESTS = sorted((Path(__file__).parent / "frontend").glob("*.test.mjs"))
_NODE_TIMEOUT_S = 60


def test_frontend_javascript_tests_pass():
    node = shutil.which("node")
    assert node, "node is required to run the frontend tests"
    assert _JS_TESTS, "no tests/frontend/*.test.mjs files found"

    result = subprocess.run(
        [node, "--test", *map(str, _JS_TESTS)],
        capture_output=True, text=True, timeout=_NODE_TIMEOUT_S,
    )

    assert result.returncode == 0, result.stdout + result.stderr
