#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"
load_config
assume_openwebui_role

AWS_REGION="${AWS_REGION}" aws s3api list-objects-v2 \
  --bucket "${S3_BUCKET_NAME}" \
  --prefix "${S3_KEY_PREFIX}/" \
  --query 'Contents[].{Key:Key,Size:Size,LastModified:LastModified}' \
  --output table

LATEST_KEY="$(
  AWS_REGION="${AWS_REGION}" aws s3api list-objects-v2 \
    --bucket "${S3_BUCKET_NAME}" \
    --prefix "${S3_KEY_PREFIX}/" \
    --query 'sort_by(Contents,&LastModified)[-1].Key' \
    --output text
)"

if [[ -z "${LATEST_KEY}" || "${LATEST_KEY}" == "None" ]]; then
  echo "No uploaded objects found."
  exit 1
fi

echo
echo "Latest object metadata:"
AWS_REGION="${AWS_REGION}" aws s3api head-object \
  --bucket "${S3_BUCKET_NAME}" \
  --key "${LATEST_KEY}" \
  --query '{ContentLength:ContentLength,ContentType:ContentType,Encryption:ServerSideEncryption,VersionId:VersionId}' \
  --output table

echo
echo "Latest object ownership tags:"
AWS_REGION="${AWS_REGION}" aws s3api get-object-tagging \
  --bucket "${S3_BUCKET_NAME}" \
  --key "${LATEST_KEY}" \
  --query 'TagSet' \
  --output table
