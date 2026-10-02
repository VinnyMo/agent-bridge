> **CANCELLED / HISTORICAL:** The owner chose a public community board. Do not follow this authentication migration. See README.md.

# v2 verification — 2026-09-28

- `.mcp-venv/bin/python -m unittest discover -s tests -v`: **15 tests passed**.
- Python AST/compilation, context JSON, shell syntax and Auth0 Action JavaScript syntax checks passed.
- A real temporary nginx TLS proxy verified client-certificate checks and header overwrite behavior. Invalid chains, wrong SANs and absent certificates were rejected. All signing keys/certificates and appended test notes were disposable fixtures.
- The downloaded official OpenAI Connectors intermediate verified successfully against the official OpenAI root.
- Staged snapshot: `20260928T182603Z-2cd47f762036`.
- Production snapshot stayed `20260928T170546Z-074f5bd62e6a`; the public health endpoint confirmed that exact release after staging.
- No production DNS, nginx, systemd, identity provider or write mode was changed. No production test notes were created. A separate sanitized real task summary was recorded.

Tests cover provider-owner policy and bootstrap denial, JWT signature/issuer/audience/expiry/not-before/subject/client/scope rejection, OAuth challenges and metadata, public REST denial after cutover, MCP reads and legacy redactions, long and repeated authenticated/local appends, body ceilings and invalid input, local-route isolation, real TLS/header-spoof rejection, and publication/rollback guards.

The installed FastMCP dependency stack emits Authlib/httpx deprecation warnings; these did not cause test failures. No dependency upgrade beyond the existing FastMCP 2 environment was performed.

## Not yet verified externally

Auth0 tenant creation/configuration, real S256 authorization-code flow, CIMD identification, owner account linking, real OpenAI client-certificate presentation and production nginx/systemd activation. These require the remaining steps in `V2_DEPLOYMENT.md`. Unit tests are not evidence that those external steps are complete.
