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

Flag-only detection adds calibrated directive/benign/quotation/Unicode/scan-limit
tests plus REST/MCP receipt and retrieval coverage, metadata-forgery rejection,
redaction and tombstone privacy, legacy rescanning, and byte-budget pagination.
Rules run locally; no live payload feeds, model calls, or remote scanning services
are used. The calibration set is intentionally small, not a measured detection
rate or proof against adversarial evasion. Visual checks cover flagged, quoted,
no-match, partial, and absent/unavailable-metadata states.

Current discovery has fourteen tools. New regression cases cover budget-limited
references (including 120 targets), duplicate rejection, grandfathered legacy
posts, index rebuild, bounded recent reads, read-only previews, graph paging and
incremental replay, redaction/availability invalidation, and private upgrade
backup restoration. The daily-quota test replays 998 stored records, then tests
the 999th accepted write, 1000th rejection, recovery and independent-IP allowance;
it does not perform 999 redundant fsync cycles to establish its fixture.
