#!/usr/bin/env bash
set -euo pipefail

REPO=agentcore-proxy
REGION=ap-southeast-1
TAG="${TAG:-v0.0.1}"
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

echo "==> Building and pushing image (linux/amd64 for Fargate)..."
# Docker Desktop exposes Buildx through the portable `docker buildx` subcommand.
docker buildx build \
    --platform linux/amd64 \
    --no-cache \
    --push \
    -t "${ECR_URI}:${TAG}" \
    "$(dirname "$0")"

echo ""
echo "Image pushed: ${ECR_URI}:${TAG}"
