# Deployment Guide

## Overview

Three deployable components:

| Component | Where | Purpose |
|---|---|---|
| `agentcore-poc` container | AWS AgentCore Runtime (ap-southeast-1) | Agent logic: Claude + SQL tool |
| `agentcore-proxy` container | EKS Fargate (agentcore namespace) | OpenAI-compatible proxy for DIFY/Open WebUI |
| OpenAI wrapper Lambda | us-east-1 | External access with SigV4 auth |

---

## Prerequisites

```bash
aws --version           # AWS CLI v2
docker --version        # Docker
kubectl version         # kubectl (configured for ai-project cluster)
python3 --version       # Python 3.10+
pip3 install boto3      # for infra scripts
```

Configure kubectl:
```bash
aws eks update-kubeconfig --region ap-southeast-1 --name ai-project
```

---

## Part 1 — Agent Container (AgentCore Runtime)

### Step 1 — Configure environment

```bash
cp .env.example .env
```

Edit `.env`:
```dotenv
AWS_DEFAULT_REGION=ap-southeast-1
CLAUDE_CODE_USE_BEDROCK=1
RDS_SECRET_ARN=arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J
RDS_DB_NAME=nuhsConversation
```

### Step 2 — Test locally

```bash
pip3 install -r requirements.txt
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --port 8080 --reload
```

Test:
```bash
curl -X POST http://localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"list all tables"}]}'
```

> **Local note:** Local runs use your Mac's AWS credentials. The subprocess still needs `CLAUDE_CODE_USE_BEDROCK=1` and the inference profile ARN. The RDS timeout is expected if running locally (RDS is in a private VPC).

### Step 3 — Build and push to ECR

```bash
# AgentCore Runtime requires linux/arm64
bash infra/build_and_push.sh
```

> **Critical:** AgentCore Runtime only supports `arm64` images. Building `amd64` will get a `ValidationException: Architecture incompatible` error.

### Step 4 — Deploy to AgentCore

```bash
export ECR_IMAGE_URI=964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-poc:latest
export RDS_SECRET_ARN=arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J
export RDS_DB_NAME=nuhsConversation
python3 infra/deploy.py
```

**What `deploy.py` creates/updates:**

| Resource | Name | Region |
|---|---|---|
| IAM role (runtime) | `agentcore-poc-runtime-role` | global |
| IAM role (wrapper) | `agentcore-poc-wrapper-role` | global |
| AgentCore Runtime | `agentcore_poc` | ap-southeast-1 |
| AgentCore Endpoint | `agentcore_poc_endpoint` | ap-southeast-1 |
| Lambda (wrapper) | `agentcore-poc-openai-wrapper` | us-east-1 |
| Lambda Function URL | — | us-east-1 |
| API Gateway resource | `POST /v1/chat/completions` on `eoqmjqt5p1` | us-east-1 |

**Common deploy failures and fixes:**

| Error | Fix |
|---|---|
| `agentRuntimeName` validation — hyphens not allowed | Use underscores: `agentcore_poc` not `agentcore-poc` |
| `Architecture incompatible` | Build image as `--platform linux/arm64` |
| ECR `AccessDenied` while validating URI | Add ECR permissions to runtime role; wait 15s for IAM propagation |
| `UPDATE_FAILED: EC2 credentials error` | Add `ec2:CreateNetworkInterface` etc. to runtime role |
| IAM propagation causing repeated failures | `deploy.py` now waits 15s after every policy change |

### Step 5 — Verify AgentCore is running

```bash
# AgentCore playground: AWS Console → Bedrock → AgentCore → agentcore_poc → Test
# Input: {"messages":[{"role":"user","content":"list all tables"}]}

# Or via py_sdk.py:
python3 py_sdk.py "list all tables"
```

---

## Part 2 — EKS Proxy (for DIFY / Open WebUI)

### Step 1 — Grant IAM permissions

```bash
python3 infra/grant_eks_access.py
```

> **Note:** This script targets `AmazonEKSFargatePodExecutionRole` — the wrong role for pod-level credentials. The actual credential mechanism used is IRSA (see Step 3). This script is kept for reference but the IRSA role `agentcore-proxy-irsa` is what actually matters.

### Step 2 — Build and push proxy image

```bash
# Fargate nodes are amd64 (NOT arm64)
bash proxy/build_and_push.sh
```

> **Critical:** EKS Fargate in this cluster runs `amd64`. Build `linux/amd64` for the proxy, `linux/arm64` for the AgentCore container. They are different.

### Step 3 — Create IRSA role (one-time)

Already created as `agentcore-proxy-irsa`. If recreating:

```bash
ACCOUNT=964340114883
OIDC_ID=62A4B3D5B9330B4CE46ADB4CC753DFB3

aws iam create-role \
  --role-name agentcore-proxy-irsa \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::'$ACCOUNT':oidc-provider/oidc.eks.ap-southeast-1.amazonaws.com/id/'$OIDC_ID'"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "oidc.eks.ap-southeast-1.amazonaws.com/id/'$OIDC_ID':sub": "system:serviceaccount:agentcore:agentcore-proxy",
          "oidc.eks.ap-southeast-1.amazonaws.com/id/'$OIDC_ID':aud": "sts.amazonaws.com"
        }
      }
    }]
  }'

aws iam put-role-policy \
  --role-name agentcore-proxy-irsa \
  --policy-name agentcore-invoke \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["bedrock-agentcore:InvokeAgentRuntime","bedrock-agentcore:InvokeAgentRuntimeForUser"],
      "Resource": "*"
    }]
  }'
```

> **Important:** Use `Resource: "*"` — scoping to the runtime ARN fails because the actual resource in the IAM check is the endpoint ARN (`runtime/*/runtime-endpoint/DEFAULT`), not the runtime ARN.

### Step 4 — Tag subnets for internal NLB

One-time, already done:
```bash
aws ec2 create-tags \
  --region ap-southeast-1 \
  --resources subnet-061205c705e0f41d4 subnet-0466b6e1fbb8a49f3 \
  --tags Key=kubernetes.io/role/internal-elb,Value=1 \
         Key=kubernetes.io/cluster/ai-project,Value=shared
```

Without these tags: `Failed build model due to unable to resolve at least one subnet`.

### Step 5 — Deploy to EKS

```bash
kubectl apply -f proxy/k8s/namespace.yaml
kubectl apply -f proxy/k8s/serviceaccount.yaml
kubectl apply -f proxy/k8s/deployment.yaml
kubectl apply -f proxy/k8s/service.yaml
```

### Step 6 — Verify

```bash
kubectl get pods -n agentcore
kubectl get svc -n agentcore

# Test from within cluster:
kubectl run test --rm -i --restart=Never --image=curlimages/curl -n dify \
  -- curl -s http://agentcore-proxy.agentcore.svc.cluster.local/v1/models
```

### Step 7 — Configure frontends

**DIFY** (any namespace in ai-project cluster):
- Base URL: `http://agentcore-proxy.agentcore.svc.cluster.local`
- API Key: any value
- Model: `agentcore`

**Open WebUI** (EC2 in default VPC, reaches via VPC peering):
- Base URL: `http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com`
- API Key: any value

---

## Part 3 — Data Ingestion into RDS

RDS is in private subnets — not reachable from a developer Mac.

```bash
# 1. Upload dump to S3
aws s3 cp data/employees.sql.gz s3://agentcore-tmp-964340114883/employees.sql.gz

# 2. Run one-shot ECS task (uses agentcore-poc image, runs pg_restore via apk)
# Task definition: agentcore-pg-restore (family, in ap-southeast-1)
# Role: agentcore-restore-task-role
aws ecs run-task \
  --region ap-southeast-1 \
  --cluster embedded-web-app \
  --task-definition agentcore-pg-restore:6 \
  --launch-type FARGATE \
  --network-configuration '{"awsvpcConfiguration":{"subnets":["subnet-061205c705e0f41d4"],"securityGroups":["sg-07258677b7e691e48"],"assignPublicIp":"DISABLED"}}'

# 3. Watch logs
aws logs tail /ecs/agentcore-pg-restore --region ap-southeast-1 --follow
```

> **Gotcha:** The pg_dump file was custom format (not gzip despite `.gz` extension). Must use `pg_restore`, not `psql`. The ECS task installs `postgresql-client` via `apk` at runtime and runs as `root` (container runs as non-root by default — `user: root` override needed in task definition).

---

## Updating After Code Changes

### Agent code change (app/)
```bash
bash infra/build_and_push.sh      # rebuilds arm64, pushes to ECR
python3 infra/deploy.py           # updates AgentCore Runtime
```

### Proxy code change (proxy/)
```bash
bash proxy/build_and_push.sh      # rebuilds amd64, pushes to ECR
kubectl rollout restart deployment/agentcore-proxy -n agentcore
```

Both deploy scripts are idempotent — safe to re-run.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| AgentCore container health check fails on `GET /ping` | Container returns 404 for `/ping` | Add `@app.get("/ping")` to `app/main.py` |
| AgentCore 500: POST /invocations 404 | Container has `/invoke` not `/invocations` | AgentCore uses `/invocations`, not `/invoke` |
| `Not logged in · Please run /login` | `claude` subprocess using Anthropic API, not Bedrock | Set `CLAUDE_CODE_USE_BEDROCK=1` and pass via `ClaudeAgentOptions(env=...)` |
| `400 Invocation of model ... not supported` | Using bare model ID without inference profile | Use `application-inference-profile` ARN as model |
| `403` on Lambda Function URL | Org SCP blocks `AuthType: NONE` | Set `AuthType: AWS_IAM`; use SigV4 or EKS proxy |
| `Unable to locate credentials` in EKS pod | Fargate pod execution role ≠ container credentials | Use IRSA: create IAM role with OIDC trust, annotate ServiceAccount |
| `AccessDeniedException: no identity-based policy allows InvokeAgentRuntime` | Policy scoped to runtime ARN but check uses endpoint ARN | Use `Resource: "*"` in policy |
| Internal NLB stuck pending | Subnets missing `kubernetes.io/role/internal-elb` tag | Tag both private subnets |
| EKS `ImagePullBackOff: no match for platform` | Proxy image built as multi-arch or wrong arch | Build `--platform linux/amd64` for EKS (Fargate is amd64 here) |
| `Cannot connect to RDS` from dev Mac | RDS in private subnet, no IGW route | Use ECS task or bastion for data ingestion |
| AgentCore `UPDATE_FAILED: EC2 credentials` | Runtime role missing VPC permissions | Add `ec2:CreateNetworkInterface` etc. to `agentcore-poc-runtime-role` |
| DB schema returns "No tables found in public schema" | Data loaded into non-public schema (e.g. `employees`) | Add `search_path=employees,public` to psycopg2 connect, or update schema query |

---

## AWS Resource Reference

| Resource | ID / ARN |
|---|---|
| AgentCore Runtime ARN | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/agentcore_poc-iumXW8638m` |
| Inference profile | `arn:aws:bedrock:us-east-1:964340114883:application-inference-profile/ji5jakx5lho3` |
| RDS endpoint | `jinxin-postgres.cf7in3efovlt.ap-southeast-1.rds.amazonaws.com` |
| Secrets Manager ARN | `arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J` |
| Lambda Function URL | `https://hx66okhncgcepkwmpk2ljzu5qe0wbtzz.lambda-url.us-east-1.on.aws/` |
| API Gateway | `eoqmjqt5p1` (us-east-1) |
| ECR (agent) | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-poc` |
| ECR (proxy) | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-proxy` |
| IRSA role | `arn:aws:iam::964340114883:role/agentcore-proxy-irsa` |
| EKS OIDC | `oidc.eks.ap-southeast-1.amazonaws.com/id/62A4B3D5B9330B4CE46ADB4CC753DFB3` |
| Internal NLB | `k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com` |
