#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REGION="${AWS_DEFAULT_REGION:-ap-southeast-1}"
TAG="${TAG:-v0.0.10}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
IMAGE="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/agentcore-dify-proxy:${TAG}"

export TAG

echo "==> Building and pushing ${IMAGE}"
bash "${SCRIPT_DIR}/build_and_push.sh"

echo "==> Applying Dify proxy Kubernetes resources"
kubectl apply -f "${SCRIPT_DIR}/k8s/"

echo "==> Deploying ${IMAGE}"
kubectl set image deployment/agentcore-dify-proxy \
    dify-proxy="${IMAGE}" \
    --namespace agentcore

echo "==> Restarting Deployment to pull the rebuilt tag"
kubectl rollout restart deployment/agentcore-dify-proxy \
    --namespace agentcore

echo "==> Waiting for rollout"
kubectl rollout status deployment/agentcore-dify-proxy \
    --namespace agentcore

echo "==> Services"
kubectl get services \
    agentcore-dify-proxy \
    agentcore-dify-proxy-internal \
    --namespace agentcore

echo
echo "In-cluster Dify base URL:"
echo "http://agentcore-dify-proxy.agentcore.svc.cluster.local/dify/v1"
