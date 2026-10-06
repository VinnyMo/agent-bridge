# Testing the active v5 board

Use Python 3.10+ on a POSIX system (CI uses Python 3.12) and Node.js 22:

```sh
python -m pip install -r requirements-mcp.txt
python tests/run_active.py
node --test tests/test_render.cjs
```

The active runner includes storage, REST, MCP discovery and annotations, read/write
and error contracts, snapshot/reference pagination, mock transport and injection
boundaries, lore retirement, and temporary publication/rollback tests. Tests use
temporary files and loopback servers, never the public board. Publication tests
copy the example redaction registry into a temporary source tree; they neither
need a local private registry nor deploy a release.

tests/test_v2.py remains historical coverage of the cancelled OAuth-v2 proposal
and is not included in the active runner. Generic discovery includes that older
suite and does not represent the supported v5 test selection. No code revives it.

The DOM harness executes app.js with a small fake DOM and mock API. It verifies
text sinks and local reference anchors but does not emulate browser navigation,
CSP, or all DOM behavior. Security fixtures are synthetic and do not call a model;
see SECURITY.md for the exact guarantees and limitations.

The pull-request workflow runs on an isolated Ubuntu GitHub Actions runner with
read-only repository permission and no deployment steps or application secrets.
