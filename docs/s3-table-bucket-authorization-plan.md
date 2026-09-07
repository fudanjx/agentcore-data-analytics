# Per-user S3 Table bucket authorization

## Summary

Use `nuhs.public.data_insight_user_access` as the authorization source, extended with a physical `table_bucket_arn`. Access is enforced at the AgentCore Gateway before any Athena/Lambda operation.

The Dify/OIDC identity supplies the user; the database supplies bucket grants. Blank datasets grant no access. Administrators are identified through `authorized_user.account_role` and can access every discoverable S3 Tables bucket.

AgentCore Gateway request interceptors support custom authorization and can inspect validated JWT claims when request headers are passed through. See the AWS documentation for [Gateway interceptors](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors.html) and [fine-grained access control](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-fine-grained-access-control.html).

## Implementation changes

### Database and grant service

- Add nullable `table_bucket_arn TEXT` to `data_insight_user_access`.
- Add a partial index on `(user_email, table_bucket_arn)` for active grants.
- Preserve `dataset` (`ah`, `nuh`) for existing consumers; it is no longer the physical authorization key.
- Backfill known grants to the corresponding bucket ARNs.
- Treat active rows with blank dataset and null bucket as no S3 Table access.
- Normalize roles and deny unknown roles.
- Support multiple active bucket rows per user.
- Add idempotent grant upsert for `(user_email, table_bucket_arn, role)`.
- Use least-privilege database credentials:
  - Gateway interceptor: `SELECT`.
  - Uploader service: `SELECT` plus grant `INSERT/UPDATE`.
- Never return raw email addresses or database credentials to the browser or agent.

### Identity propagation

- Validate the existing OIDC/JWT identity at the Dify proxy.
- Use a stable subject and verified email claim for grant lookup.
- Remove production reliance on the browser/body-supplied user ID.
- Propagate the validated request identity through Runtime to the Gateway.
- Configure the Gateway with JWT validation and a request interceptor.
- The interceptor extracts the validated identity, loads active grants, and rejects unauthorized requests before target execution.
- Log only request ID, hashed user identifier, bucket ARN, decision, and role.

### Gateway tools and dynamic discovery

- Add `list_accessible_buckets`.
- Require a bucket identifier for table listing, table description, and SQL execution.
- Preserve `ah`/`nuh` as compatibility aliases, but resolve them to physical bucket ARNs before execution.
- Discover available S3 Table buckets dynamically through AWS APIs.
- Return only the intersection of:
  - AWS-discoverable buckets, and
  - the user’s database grants.
- Administrators receive all discoverable buckets.
- Namespace remains an internal routing dimension, not an authorization dimension.
- The interceptor must reject bucket spoofing and source/bucket mismatches.
- The Lambda must execute only against the interceptor-approved canonical bucket.

### Uploader creator grant

- After a new table’s Glue create operation succeeds, insert an idempotent `owner` grant for the creator and bucket ARN.
- Do not grant access for failed, cancelled, or abandoned uploads.
- If grant persistence fails after table creation, keep the table intact, report a warning, and provide retry/reconciliation logging.
- Existing uploader authorization, history, rollback, and skill permissions remain unchanged.

## Test plan

- Database migration and backfill tests.
- Multiple bucket grants for one user.
- Deleted grants and blank-dataset rows deny access.
- Admin access to every discovered bucket.
- Unknown roles deny access.
- Invalid, expired, missing, and mismatched JWT claims deny access.
- Interceptor rejects unauthorized bucket, table, and SQL requests.
- Dynamic bucket discovery returns only authorized buckets.
- `ah`/`nuh` compatibility aliases resolve correctly.
- Creator grant is written only after successful create and is idempotent.
- Failed Glue creation creates no grant.
- Regression tests for existing Gateway and uploader behavior.
- Integration test with a database reachable from the interceptor and uploader service.

## Assumptions

- The OIDC provider exposes a stable subject and verified email.
- Gateway and uploader services can reach the private PostgreSQL service in EKS.
- `authorized_user.account_role = admin` is the authoritative administrator signal.
- All current S3 Tables Gateway tools are read-only.
- Bucket-level grants are sufficient for the current phase; namespace-level grants can be added later.
- Future bucket access is provisioned by inserting an active grant row with its physical bucket ARN.
- The blank-dataset active row grants no S3 Table access and is flagged for cleanup.
- Creator access is granted only after successful new-table creation.
