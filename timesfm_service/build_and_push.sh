#!/usr/bin/env bash
set -euo pipefail

REPO=timesfm-service
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

echo "==> Building and pushing image (linux/amd64 for Fargate)..."
# NOTE: This build downloads ~800MB of model weights — expect 10-20 minutes
~/.docker/cli-plugins/docker-buildx build \
    --platform linux/amd64 \
    --push \
    -t "${ECR_URI}:latest" \
    "$(dirname "$0")"

echo ""
echo "Image pushed: ${ECR_URI}:latest"
