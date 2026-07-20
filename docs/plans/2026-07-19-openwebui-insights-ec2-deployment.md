# OpenWebUI Insights EC2 Deployment Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an isolated OpenWebUI v0.10.2-slim AgentCore test service at `https://insights.bot-alex.com` without changing the behavior or data of the existing v0.6.15 service.

**Architecture:** Extend the existing `/home/ubuntu/app/docker-compose.yml` with one additive `open-webui-insights` service. It uses a separate PostgreSQL role/database, Docker volume, S3 bucket/prefix, ALB target group, Route 53 hostname, and AgentCore proxy namespace, while sharing only the existing PostgreSQL server and private VPC connectivity.

**Tech Stack:** Docker Compose v2, OpenWebUI v0.10.2-slim, PostgreSQL 15, AWS S3/IAM/EC2/ALB/Route 53, FastAPI, boto3, unittest, AWS Systems Manager.

---

## Decisions

- Keep the existing OpenWebUI v0.6.15, LiteLLM, PostgreSQL database, files, users, target group, and `claude.bot-alex.com` route unchanged.
- Pin `ghcr.io/open-webui/open-webui:v0.10.2-slim` by OCI digest.
- Disable local embedding/retrieval and Ollama for the new service.
- Use the POC plain-header trust model only over the private VPC path.
- Put new users in the `pending` role.
- Expire new S3 files after seven days.
- Keep the deployment in the existing Compose file for one-command management.
- Protect the existing host with a strict memory limit on the new service and low-swappiness swap.

## Task 1: Add proxy support for the Insights deployment

**Files:**
- Modify: `proxy/server.py`
- Modify: `proxy/k8s/deployment.yaml`
- Test: `tests/test_proxy_openwebui_context.py`

1. Write failing tests proving `/insights/v1` maps the same Harness to:
   - `actorId=openwebui-insights:<user-id>`
   - `runtimeSessionId=owui-insights-<user-id>-<chat-id>`
   - the dedicated Insights S3 bucket/prefix
2. Verify the current tests fail.
3. Add an explicit OpenWebUI source profile rather than widening the existing local POC allowlist.
4. Keep `/harness/v1`, Dify routes, and all current identity mappings unchanged.
5. Run the focused and complete test suites.

## Task 2: Prepare versioned EC2 deployment artifacts

**Files:**
- Create: `openwebui-insights/README.md`
- Create: `openwebui-insights/compose-service.yml`
- Create: `openwebui-insights/functions/agentcore_file_context.py`
- Create: `openwebui-insights/install_filter.py`
- Create: `openwebui-insights/deploy_compose.py`
- Create: `openwebui-insights/verify.sh`

1. Reuse the validated v0.10.2 filter and make its installer PostgreSQL-safe.
2. Define only the additive Compose service and volume.
3. Configure:
   - v0.10.2-slim pinned digest
   - `BYPASS_EMBEDDING_AND_RETRIEVAL=true`
   - no Ollama
   - pending default user role
   - forwarded OpenWebUI identity headers
   - dedicated PostgreSQL URL, secret key, volume, and S3 settings
   - memory/CPU limits and health check
4. Make the Compose patcher back up the original file, create a candidate, run `docker compose config --quiet`, and replace the file only after validation.
5. Add rollback instructions that remove only the Insights service definition.

## Task 3: Bootstrap S3 and IAM

**Files:**
- Create: `infra/openwebui_insights_bootstrap.py`

1. Write tests or dry-run assertions for resource names and IAM scope.
2. Create a dedicated private bucket with encryption, versioning, public-access block, and seven-day expiry.
3. Create or update a least-privilege EC2 instance role/profile with S3 access and SSM core permissions.
4. Attach the profile to `i-06f7b81355b8c5346` without rebooting.
5. Grant the proxy metadata validation and Code Interpreter read access only to the Insights prefix.
6. Verify effective permissions with harmless put/tag/list/delete probes.

## Task 4: Create isolated PostgreSQL resources

1. Back up the existing PostgreSQL server before DDL.
2. Generate a unique database password and WebUI secret on EC2 without printing either.
3. Create role `openwebui_insights` and database `openwebui_insights`.
4. Write `/home/ubuntu/app/insights/.env` with mode `0600`.
5. Confirm the existing `openwebui` database and connections are unchanged.

## Task 5: Protect host capacity

1. Capture baseline `docker stats`, memory, disk, and current health.
2. Add a small swap file with low swappiness if none exists.
3. Pull the slim image before changing Compose.
4. Verify direct private connectivity from a disposable container to the AgentCore proxy.
5. Enforce a memory limit so an Insights failure cannot consume the existing services.

## Task 6: Add and start only the new Compose service

1. Copy the versioned deployment artifacts to `/home/ubuntu/app/insights`.
2. Patch and validate the existing Compose file.
3. Run `docker compose up -d --no-deps open-webui-insights`.
4. Do not run `docker compose down` or recreate existing services.
5. Wait for the new health check and inspect its startup migrations.
6. Install and activate the AgentCore filter only in the new database.

## Task 7: Publish through ALB and Route 53

1. Add EC2 security-group ingress for the new host port from the existing ALB security group only.
2. Create a dedicated target group and register the existing instance on the new port.
3. Wait for target health.
4. Add a host-header listener rule for `insights.bot-alex.com`; leave the default action unchanged.
5. Create an alias in authoritative zone `Z069491219YFUFHMLLV7E`.
6. Verify the existing wildcard ACM certificate serves the new hostname.

## Task 8: End-to-end isolation and regression checks

1. Bootstrap the first admin before allowing general signup.
2. Confirm subsequent signups receive `pending`.
3. Test text chat and streaming through AgentCore.
4. Test S3 upload, ownership tags, seven-day lifecycle, and Code Interpreter processing.
5. Test two users, two chats, cross-owner file rejection, background-task isolation, and same-session serialization.
6. Confirm existing v0.6.15, LiteLLM, PostgreSQL, Caddy, target health, and `https://claude.bot-alex.com` remain healthy.
7. Record exact resource IDs, image digest, health evidence, and rollback commands.
