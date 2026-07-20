# Local OpenWebUI + S3 + AgentCore test stack

This stack runs OpenWebUI `v0.10.2` in Docker Desktop, stores its uploaded
files in a dedicated private S3 bucket, and connects its OpenAI-compatible
model endpoint to the private AgentCore proxy through a Caddy relay on EC2.

No long-lived AWS access key is written to disk. `start.sh` assumes the
bucket-scoped IAM role and passes one-hour credentials only to the container.

The model path is private end to end:

```text
OpenWebUI container
  -> 100.79.116.60:18080 over Tailscale
  -> internal AgentCore NLB over VPC peering
  -> AgentCore proxy
```

Caddy is bound only to the EC2 Tailscale address. No public EC2 listener or
security-group rule is used for this relay.

## 1. Bootstrap AWS storage

```bash
./openwebui-local/bootstrap_aws.sh
```

Creates:

- `s3://agentcore-openwebui-test-964340114883/openwebui-test/`
- S3 block-public-access, SSE-S3 encryption and versioning
- seven-day test-object expiry
- IAM role `agentcore-openwebui-local-test`
- read-only access for `agentcore-code-interpreter-role` on the test prefix

## 2. Verify the private AgentCore route

On macOS, Shadowrocket may install a competing route for Tailscale's
`100.64.0.0/10` address range. The start script detects this and, only when
needed, asks for administrator access to install a more-specific route for the
EC2 relay:

```bash
./openwebui-local/ensure_tailscale_route.sh
```

This changes routing only for `100.79.116.60`. The route may need to be
re-added after a reboot or VPN interface change; `start.sh` performs the same
check automatically.

## 3. Start OpenWebUI

```bash
./openwebui-local/start.sh
```

Open <http://localhost:3000>. The first account created is the local admin.
The AgentCore model is exposed with the `agentcore` prefix.

This test configuration bypasses OpenWebUI's embedding and retrieval layer.
The `agentcore_file_context` global filter is installed automatically by
`start.sh`. For the AgentCore model only, it resolves every file still attached
to the current chat, verifies that OpenWebUI says the current user owns it, and
forwards an S3 metadata manifest to the proxy. It does not download or copy file
contents.

OpenWebUI forwards `X-OpenWebUI-User-Id` and `X-OpenWebUI-Chat-Id`. The proxy
maps them to:

- AgentCore `actorId`: `openwebui:<user-id>`
- AgentCore `runtimeSessionId`: `owui-<user-id>-<chat-id>`

Both headers are required for `/harness` chat requests. This gives one memory
namespace per OpenWebUI user and one runtime session per chat.

The filter marks OpenWebUI title, follow-up and other background-generation
requests as tasks. The proxy runs each task in a fresh `owui-bg-*` session
under `actorId=openwebui-task:<user-id>`, without chat files, so generated
prompts cannot enter the user chat's AgentCore history. For foreground chat,
the proxy sends only the newest user turn plus system context, because the
Harness already persists prior turns. Requests for the same foreground session
are serialized by the single proxy replica.

The compose file is authoritative for service settings
(`ENABLE_PERSISTENT_CONFIG=false`), so Admin Panel setting changes do not
survive a container restart. Accounts and chats remain in the Docker volume.
Sign-up is disabled after initial setup; use the admin account already created
at first launch.

## 4. Verify an S3 upload

Attach a small CSV or text file in OpenWebUI, then run:

```bash
./openwebui-local/verify_s3.sh
```

The result must show an object below `openwebui-test/`. With
`S3_ENABLE_TAGGING=true`, OpenWebUI also writes ownership tags including its
user ID.

OpenWebUI's native S3 provider is server-mediated: the browser uploads to
OpenWebUI, which writes a temporary local file and then copies it into S3.
The filter forwards only file id, name, MIME type, size and S3 URI. Before
AgentCore sees the URI, the proxy verifies the allowlisted bucket/prefix,
extension, object size, and the S3 tags `OpenWebUI-User-Id` and
`OpenWebUI-File-Id`. It obtains authoritative size/existence from a
prefix-restricted S3 listing. A mismatch rejects the whole request.

Files remain available on later turns in the same chat while they remain
attached. They are not carried into another chat. A shared-chat viewer cannot
process the original owner's files and must upload their own copy.

Current limits are 50 MiB per file, 10 files per chat, and 200 MiB combined.
Supported extensions are `csv`, `xlsx`, `xls`, `pdf`, `docx`, `pptx`, `txt`,
`md`, and `json`.

## POC trust boundary

This local POC trusts identity headers emitted by OpenWebUI because both
OpenWebUI and the relay are inside the private test path. Before exposing the
proxy to untrusted clients, replace the plain headers with a signed short-lived
JWT and derive `actorId` from verified claims. Also narrow Code Interpreter S3
access to object-scoped authorization. Dify compatibility is a separate next
phase.

## Stop

```bash
./openwebui-local/stop.sh
```

The named Docker volume is retained. To remove it after testing:

```bash
docker volume rm agentcore-openwebui-test-data
```

The S3 bucket and IAM role are intentionally retained for repeatable tests.

## Port-forward fallback

If the EC2 relay is under maintenance, `proxy_tunnel.sh` remains available as
a manual diagnostic fallback. Change `AGENTCORE_BASE_URL` back to
`http://host.docker.internal:18080/harness/v1` before using it.
