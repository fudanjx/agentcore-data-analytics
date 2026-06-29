#!/usr/bin/env bash
set -euo pipefail

REPO=agentcore-proxy
REGION=ap-southeast-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}"

echo "==> Creating ECR repository (idempotent)..."
aws ecr create-repository \
    --repository-name "${REPO}" \
    --region "${REGION}" \
    --image-scanning-configuration scanOnPush=true \
    2>/dev/null || echo "    (repository already exists)"

echo "==> Authenticating Docker with ECR..."
aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${ECR_URI}"

echo "==> Building image (linux/amd64 for Fargate)..."
docker build --platform linux/amd64 \
    -t "${REPO}" \
    "$(dirname "$0")"

echo "==> Tagging and pushing..."
docker tag "${REPO}:latest" "${ECR_URI}:latest"
docker push "${ECR_URI}:latest"

echo ""
echo "Image pushed: ${ECR_URI}:latest"
