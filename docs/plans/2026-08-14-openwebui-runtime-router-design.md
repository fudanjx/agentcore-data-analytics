# OpenWebUI Runtime Router Design

## Objective

Refocus `agentcore-proxy` exclusively on OpenWebUI and route requests to AgentCore runtimes by a deployment-configured slug. Remove Dify behavior from this proxy without changing the independent `dify-proxy/` component.

## Routing model

The proxy loads a JSON backend registry from `AGENTCORE_RUNTIME_ROUTES_JSON`. Each entry supplies an OpenWebUI-facing slug, display name, and exactly one AgentCore Runtime or Harness ARN. Invalid configuration fails during application startup. Changing a backend therefore requires a ConfigMap update and pod rollout, not a new image.

Canonical routes are:

- `/strands/v1` -> `Strands_runtime`
- `/insights-office/v1` -> `harness_insights_office`
- `/gmio-pcr-dev/v1` -> `gmio_pcr_dev`

The root `/v1` routes and the temporary `/insights/v1` alias resolve to `strands`. The compatibility alias is served but hidden from model discovery in the v0.10.2 OpenWebUI configuration.

Strands and GMIO are standalone runtimes invoked with `invoke_agent_runtime`.
Insights Office is harness-managed and must be invoked through its Harness ARN
with `invoke_harness`; AWS rejects direct invocation of its managed Runtime ARN.

## OpenWebUI contract

Every chat request requires `X-OpenWebUI-User-Id` and `X-OpenWebUI-Chat-Id`. The proxy derives the isolated actor and session identifiers from those trusted private-network headers. The existing owner/chat-scoped S3 manifest validation applies uniformly to every configured slug.

The common upload route remains `POST /v1/files`. Every configured agent supports `POST /{slug}/v1/artifacts/register`. Artifact registration validates the bucket, requesting user and chat key prefix, ownership tags, object metadata, extension, and size before the OpenWebUI filter creates a user-owned native File record.

Downloadable generated formats are CSV, DOCX, XLSX, PPTX, PDF, and HTML. HTML is always linked through OpenWebUI's authenticated file endpoint with `attachment=true`; it is not rendered under the application origin.

## Streaming

Structured tool lifecycle events are forwarded individually, immediately, and in their original order. The proxy does not aggregate active tools and does not synthesize generic progress such as `Preparing final answer`. Only safe tool names, lifecycle state, and safe runtime summaries are exposed; tool arguments, raw results, credentials, and internal reasoning are excluded.

Each completed or failed tool closes its own status, and the response terminator closes any remaining transient UI state. A runtime that emits no structured tool events receives no fabricated progress display.

## OpenWebUI v0.10.2

The Insights deployment exposes the three canonical models with display names `Strands Runtime`, `Insights Office`, and `GMIO PCR Dev`. Its global filter recognizes all three model IDs, resolves the artifact-registration endpoint from the selected model slug, and continues to enforce chat ownership. Existing chats using the hidden `insights` alias remain functional for the compatibility release.

The legacy OpenWebUI v0.6.15 deployment is out of scope.

## Dify removal boundary

Remove Dify routes, request/response translation, Dify uploads, Dify artifact URL handling, environment variables, and main-proxy tests/documentation from `agentcore-proxy`. Preserve the separately deployable `dify-proxy/` directory unchanged.

## Operational safety

- Preserve `/health`, root `/v1`, `/v1/files`, `/insights/v1`, and `/insights-office/v1` compatibility.
- Fail startup on malformed configuration, invalid ARNs, or entries that select
  both/neither invocation modes.
- Return `404` for unknown or disabled slugs.
- Add regression coverage for routing, mandatory identity, file isolation, HTML registration, direct tool status ordering, and absence of Dify routes.
- Build deployment images for `linux/amd64` when a release is requested.
