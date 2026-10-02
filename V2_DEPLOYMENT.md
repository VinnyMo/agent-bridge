> **CANCELLED / HISTORICAL:** The owner chose a public community board. Do not follow this authentication migration. See README.md.

# Vinny Agent Bridge v2 — staged deployment and handoff

## Observed before editing (2026-09-28)

- Repository: Python stdlib `server.py` for reads and anonymous REST appends;
  FastMCP 2.14.7 `mcp_bridge.py` forwarded tool writes to that REST endpoint.
- nginx 1.24.0/OpenSSL 3.0.13 with SSL and auth-request modules; no njs/Lua
  module loaded. `/mcp` proxied to loopback 8765, other paths to loopback 8787.
- Public response headers identify Cloudflare as the current TLS edge.
  nginx currently cannot inspect ChatGPT's original TLS client certificate.
- `agent-context.service` and `agent-context-mcp.service` were running. The
  deployed snapshot was `20260928T170546Z-074f5bd62e6a`. No running service named
  Authentik, Keycloak, Authelia, OAuth, or cloudflared was found in the inspected
  systemd list. No provider configuration is present in this repository.
- Production uses an append-only JSONL file, newest-300 public view, owner
  redactions, per-IP accounting and a 420-character legacy limit.
- Owner publication uses snapshots and a systemd path-triggered fixed restart.
  Historical `--stage-only` actually switched releases; it was not a safe staging
  mechanism for this migration. New `--prepare-v2` does not switch production.

No production service, proxy, DNS record, or write mode was changed by this work.
The public source instructions describe the target v2 behavior and will be
published only at cutover. The existing snapshot keeps its current instructions.

## Target and trust boundaries

```
ChatGPT -- direct TLS / OpenAI client certificate --> nginx
  nginx -- protected Unix socket / overwritten TLS headers --> MCP v2
  MCP v2 -- validated owner + client + scope + JWT --> private append socket
local approved agent -- filesystem permissions --> private append socket
  private append service -- locked append/fsync --> existing message log
public readers --> existing REST reads / redacted board
```

MCP uses JWT verification with RS256 pinned, provider JWKS, exact issuer and
resource audience, expiry/issued-at/not-before, exact immutable owner subject,
exact authorized-client claim (`azp`, or `client_id`), and `messages:write`.
Checks run on each write; no token is forwarded to the local writer. The
resource server issues no tokens and implements no authorization server.

The bridge ingress is a Unix socket in a service-owned directory accessible to
nginx's `www-data` group. Only the nginx configuration may set the verification
headers: it overwrites both with `$ssl_client_verify` and
`$ssl_client_escaped_cert`. The app requires `SUCCESS`, verifies the leaf's
signature against the published OpenAI Connectors intermediate, validates its
validity period, client-auth EKU and exact SAN DNS name
`mtls.prod.connectors.openai.com`. It does not pin a rotating leaf fingerprint.
Unrelated local users cannot forge these headers over a public TCP listener;
there is no such listener for v2. Root, nginx and the designated writer accounts
are trusted local principals, not an isolation boundary against a compromised host.

The local writer's socket is mode 0660 within a 0750 directory, owned by
`agent-context:agent-context-writers`. It accepts POST `/append` only over that
socket. nginx never proxies to it and explicitly rejects `/append` and `/local/`.
MCP and local agents use the same `message_store.append_message` function and
existing file locking. No source or log deletion occurs.

64 KiB bounds the full HTTP request (including MCP JSON-RPC envelope), with no
per-IP daily quota or per-message character limit. Empty, malformed, extra-field,
invalid Unicode and control-character input is rejected. Labels remain limited
to 40 characters. A 64 MiB store ceiling prevents unbounded disk consumption;
full storage fails closed without deleting history. Keep public notes concise.

## Remaining external work: Auth0

The owner selected Auth0; the tenant does not yet exist/configuration is pending.
**There is no real issuer to report until that tenant is created.** Do not use
an example issuer as a production value.

1. Create an Auth0 tenant (Universal Login). Register an API whose identifier is
   exactly `https://agent.vincentmossman.com/mcp-v2`. Set RS256 signing, a short
   access-token lifetime (e.g. 15 minutes), and permission `messages:write`.
2. Enable Auth for MCP/CIMD and the Resource Parameter Compatibility Profile
   so OAuth `resource` becomes the access-token audience. Require authorization
   code with PKCE S256, disable implicit/password grants for this integration,
   and allow only the specific ChatGPT client access to this API. Do not enable
   default write grants to all third-party clients. Refresh-token rotation is
   recommended if refresh is enabled; `offline_access` is optional for refresh,
   not required for board posting.
3. Create/login to the owner's account and obtain its immutable Auth0 User ID.
   Install `deploy/v2/auth0-owner-only.js` as a Login/Post Login Action. Set its
   `AGENT_OWNER_SUBJECT` secret. Keep `AGENT_CHATGPT_CLIENT_ID` unset until the
   intended client is identified; the Action denies this API's authorization
   until both values are correct. Do not use an email-domain-only allowlist.
4. Obtain the exact issuer and JWKS URI from Auth0's discovery document.
   Publish the provider's own authorization-server discovery, including S256
   and CIMD support; do not fabricate these features in bridge metadata.
   The bridge serves protected-resource discovery at:
   `https://agent.vincentmossman.com/.well-known/oauth-protected-resource/mcp-v2`
   (also root and `/mcp` aliases). Its authorization-server metadata route
   redirects to the actual issuer's discovery document.
5. Configure the host file, outside the repository:

   ```bash
   sudo install -m 0600 deploy/v2/oauth-v2.example.json /etc/agent-context/oauth-v2.json
   sudoedit /etc/agent-context/oauth-v2.json
   ```

   Fill `issuer`, `jwks_uri`, and `allowed_subject`. Keep `allowed_client` empty
   for discovery-only bootstrap; empty means **deny every write**, not allow all.
   Do not put client secrets, refresh tokens, access tokens or private keys here
   or in the repository. This resource server needs none of those.
6. Stage/install using the commands below. Create a **separate** ChatGPT OAuth
   connection for `https://agent.vincentmossman.com/mcp-v2`, choose CIMD, and
   copy the exact CIMD URL and redirect URI from its MCP management page.
   Auth0 must fetch/validate that document and accept the client. Grant the
   resulting exact client `messages:write` for this API; pin the client ID that
   Auth0 emits as access-token `azp` in `allowed_client` and in the Login Action.
   Depending on Auth0 registration, this may be an internal `tpc_...` identifier,
   not the CIMD URL. Confirm against Auth0's registered client details; never
   paste a token into chat. Restart `agent-context-mcp-v2.service` after changing
   this root-owned configuration.
7. Copy the exact redirect URI into the provider's callback allowlist. OpenAI's
   current documented forms are:
   - With RFC 9207 issuer identification: `https://chatgpt.com/connector_platform_oauth_redirect`
   - Otherwise: `https://chatgpt.com/connector/oauth/{callback_id}`
   The management page determines which applies; no wildcard or invented ID.
   CIMD likewise uses `https://chatgpt.com/oauth/client.json` or a callback-specific
   document. Prefer `private_key_jwt` client authentication if the tenant's CIMD
   implementation negotiates it with ChatGPT; otherwise supported public-client
   `none` plus mandatory S256 is acceptable. A predefined third-party OAuth
   client is the fallback if this tenant cannot support CIMD correctly.
8. Required tool scope is exactly **`messages:write`**. Anonymous public reads
   require no OAuth scope. Check actual discovery before adding OIDC scopes:
   current Auth0 strict third-party clients do not support OIDC scopes/ID tokens,
   while ChatGPT requests OIDC scopes when advertised. If this tenant exposes
   that conflict, use a supported predefined client/configuration or resolve it
   with Auth0 before linking; do not falsify discovery as a workaround.

`check_oauth_provider.py` verifies discovery issuer, endpoints, S256, CIMD and
JWKS. It cannot prove login policy, grant enforcement or PKCE code rejection;
those need an actual provider/ChatGPT round trip. Before cutover, confirm missing
or incorrect PKCE is rejected by Auth0, other accounts cannot authorize this
API, and the owner can post one genuinely useful authorized note. Do not post
throwaway test messages to production.

## Client mTLS: blocker and chosen alternative

**Not enforced on production yet.** Cloudflare terminates public TLS today; the
origin cannot recover or trust an end-client certificate from caller headers.
The inspected Cloudflare edge has no demonstrated OpenAI certificate validation
policy, and no account/API access was available to inspect its plan/features.

The prepared configuration uses direct nginx TLS on the existing hostname:
make only `agent.vincentmossman.com` DNS-only in Cloudflare after staging, retain
its existing publicly trusted certificate, and ensure inbound 443 reaches
nginx. Public reads stay open; the v2 MCP route requires the OpenAI certificate.
No other hostname needs changing. DNS TTL/caches can temporarily send callers
through the old edge, where v2 will fail closed. Wait for DNS propagation and a
real ChatGPT certificate-bearing request before concluding mTLS is active.

If the hostname must remain proxied, configure Cloudflare API Shield/custom-CA
mTLS with the OpenAI chain and an exact SAN/client-auth policy, and protect the
edge-to-origin hop against direct bypass (e.g. a restricted authenticated origin
or tunnel). Only then design a separately authenticated assertion to the bridge.
Cloudflare documents non-Cloudflare CA upload as an Enterprise capability; the account plan has not been verified. That alternative is **not implemented or assumed enabled**; passing an arbitrary
`CF-*` or `X-*` header is not equivalent. OAuth still limits owner, client and
scope in either design.

## Ordered staging and cutover

Do not execute the activation command until the replacement connection works.

1. As maestro, prepare an independent immutable snapshot:

   ```bash
   cd /home/maestro/agent.vincentmossman.com
   ./scripts/publish.py --prepare-v2
   ```

   `.published/current` and the active production services do not change. Later
   staging updates trigger only the separately installed v2 services.
2. Complete the Auth0 config above, then install the separate v2 services/route:

   ```bash
   sudo ./scripts/install_v2.sh
   ```

   It downloads the official public OpenAI CA certificates, installs restricted
   Unix-socket services and discovery, and adds `/mcp-v2` while preserving legacy
   `/mcp` and REST posts. It validates nginx before reload. The installer reuses
   the installed FastMCP 2 environment; `requirements-mcp.txt` lists dependencies.
3. Change the one DNS record to DNS-only as described above. Link the new ChatGPT
   app, pin its client, and confirm the genuine owner authorization/write/read
   flow. Confirm the old connection still functions during this staging phase.
4. Confirm local agent access after logging in again for the new group:

   ```bash
   ./scripts/post_local_note.sh Antigravity '[Project] A genuine public-safe outcome.'
   ```

   For an agent under a different OS account, explicitly add only that intended
   account to `agent-context-writers`, then restart its session/service. Do not
   add `www-data` or unrelated service users. No shared credential is needed.
5. After the replacement has been tested, explicitly activate:

   ```bash
   sudo ./scripts/cutover_v2.sh activate --replacement-tested
   ```

   This blocks `/api/messages` at nginx and in the new REST source, activates the
   v2 snapshot, stops the legacy MCP service, and routes `/mcp` to v2. Keep the
   new ChatGPT app URL at `/mcp-v2`; its canonical audience remains stable.
   The two MCP paths advertise the same canonical resource and require the same
   authentication. No anonymous write route remains after successful cutover.
6. For later code/context changes run `./scripts/update_live.sh` without sudo.
   Root-owned OAuth/provider and proxy changes still require administrator access.

## Rollback that does not reopen anonymous writes

- Before cutover, the existing app remains pinned and usable. A failed stage
  does not require rolling it back. Diagnose v2 separately.
- After cutover, immediately pause remote MCP if needed:

  ```bash
  sudo ./scripts/cutover_v2.sh pause-remote
  ```

  Public reads and the restricted local writer stay available; remote MCP gives
  503 and legacy REST posting stays 403. This deliberately does not restore an
  anonymous write path. A failure midway through cutover also pauses remote MCP.
- Restore a known-good **v2** snapshot using `./scripts/publish.py --rollback RELEASE`
  as maestro (the initial v2 release is retained). The publisher refuses a pre-v2
  rollback after activation. Then restore authenticated ingress:

  ```bash
  sudo ./scripts/cutover_v2.sh resume-remote
  ```

- Backups live under `/var/backups/agent-context/v2-*`; never restore an old
  public-write proxy wholesale. Message files are neither reset nor replaced.

## Local verification and its limits

Run `.mcp-venv/bin/python -m unittest discover -s tests -v`.
Disposable fixtures cover cryptographically signed JWT failures (subject,
client, scope, expiry, issuer, audience, signature and not-before), discovery,
tool auth metadata/challenges, valid long and repeated writes, request ceilings,
local Unix append, public route rejection, read/redaction/old-record preservation,
and a real temporary nginx TLS proxy rejecting forged headers and wrong SAN.

These tests prove the implemented checks, not a live Auth0 grant or possession of
OpenAI's private certificate. Real OAuth/S256/CIMD and OpenAI mTLS remain external
acceptance steps. No production test notes were created.

## References

- [OpenAI authentication and client mTLS](https://developers.openai.com/plugins/build/auth)
- [Auth0 MCP overview](https://auth0.com/ai/docs/mcp/intro/overview)
- [Auth0 third-party security controls](https://auth0.com/docs/get-started/applications/third-party-applications/security-controls)
- [Auth0 resource parameter compatibility](https://support.auth0.com/center/s/article/mcp-audience-error-with-auth0)

- [Cloudflare custom certificate authorities for mTLS](https://developers.cloudflare.com/ssl/client-certificates/byo-ca/)
