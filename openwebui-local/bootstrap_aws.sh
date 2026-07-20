#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a
source "${SCRIPT_DIR}/.env"
set +a

CALLER_ARN="$(AWS_REGION="${AWS_REGION}" aws sts get-caller-identity --query Arn --output text)"
ROLE_NAME="${AWS_ROLE_ARN##*/}"

echo "Creating or updating private test bucket: ${S3_BUCKET_NAME}"
if ! AWS_REGION="${AWS_REGION}" aws s3api head-bucket --bucket "${S3_BUCKET_NAME}" 2>/dev/null; then
  AWS_REGION="${AWS_REGION}" aws s3api create-bucket \
    --bucket "${S3_BUCKET_NAME}" \
    --region "${AWS_REGION}" \
    --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
fi

AWS_REGION="${AWS_REGION}" aws s3api put-public-access-block \
  --bucket "${S3_BUCKET_NAME}" \
  --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

AWS_REGION="${AWS_REGION}" aws s3api put-bucket-encryption \
  --bucket "${S3_BUCKET_NAME}" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

AWS_REGION="${AWS_REGION}" aws s3api put-bucket-versioning \
  --bucket "${S3_BUCKET_NAME}" \
  --versioning-configuration Status=Enabled

LIFECYCLE="$(jq -cn --arg prefix "${S3_KEY_PREFIX}/" '{
  Rules: [{
    ID: "expire-local-openwebui-test",
    Status: "Enabled",
    Filter: {Prefix: $prefix},
    Expiration: {Days: 7},
    NoncurrentVersionExpiration: {NoncurrentDays: 1},
    AbortIncompleteMultipartUpload: {DaysAfterInitiation: 1}
  }]
}')"
AWS_REGION="${AWS_REGION}" aws s3api put-bucket-lifecycle-configuration \
  --bucket "${S3_BUCKET_NAME}" \
  --lifecycle-configuration "${LIFECYCLE}"

BUCKET_POLICY="$(jq -cn --arg bucket "${S3_BUCKET_NAME}" '{
  Version: "2012-10-17",
  Statement: [{
    Sid: "DenyInsecureTransport",
    Effect: "Deny",
    Principal: "*",
    Action: "s3:*",
    Resource: [
      ("arn:aws:s3:::" + $bucket),
      ("arn:aws:s3:::" + $bucket + "/*")
    ],
    Condition: {Bool: {"aws:SecureTransport": "false"}}
  }]
}')"
AWS_REGION="${AWS_REGION}" aws s3api put-bucket-policy \
  --bucket "${S3_BUCKET_NAME}" \
  --policy "${BUCKET_POLICY}"

TRUST_POLICY="$(jq -cn --arg caller "${CALLER_ARN}" '{
  Version: "2012-10-17",
  Statement: [{
    Effect: "Allow",
    Principal: {AWS: $caller},
    Action: "sts:AssumeRole"
  }]
}')"

if AWS_REGION="${AWS_REGION}" aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  AWS_REGION="${AWS_REGION}" aws iam update-assume-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-document "${TRUST_POLICY}"
else
  AWS_REGION="${AWS_REGION}" aws iam create-role \
    --role-name "${ROLE_NAME}" \
    --description "Temporary local OpenWebUI access to its dedicated S3 test bucket" \
    --max-session-duration 43200 \
    --assume-role-policy-document "${TRUST_POLICY}" >/dev/null
fi

ROLE_POLICY="$(jq -cn \
  --arg bucket "${S3_BUCKET_NAME}" \
  --arg prefix "${S3_KEY_PREFIX}/" '{
    Version: "2012-10-17",
    Statement: [
      {
        Sid: "BucketMetadata",
        Effect: "Allow",
        Action: ["s3:GetBucketLocation", "s3:ListBucket"],
        Resource: ("arn:aws:s3:::" + $bucket)
      },
      {
        Sid: "OpenWebUIObjects",
        Effect: "Allow",
        Action: [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:GetObjectTagging",
          "s3:PutObjectTagging",
          "s3:AbortMultipartUpload",
          "s3:ListMultipartUploadParts"
        ],
        Resource: ("arn:aws:s3:::" + $bucket + "/" + $prefix + "*")
      }
    ]
  }')"
AWS_REGION="${AWS_REGION}" aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "OpenWebUILocalTestS3" \
  --policy-document "${ROLE_POLICY}"

CODE_INTERPRETER_POLICY="$(jq -cn \
  --arg bucket "${S3_BUCKET_NAME}" \
  --arg prefix "${S3_KEY_PREFIX}/" '{
    Version: "2012-10-17",
    Statement: [
      {
        Sid: "ListOpenWebUILocalTestPrefix",
        Effect: "Allow",
        Action: "s3:ListBucket",
        Resource: ("arn:aws:s3:::" + $bucket),
        Condition: {
          StringLike: {
            "s3:prefix": [
              $prefix,
              ($prefix + "*")
            ]
          }
        }
      },
      {
        Sid: "ReadOpenWebUILocalTestObjects",
        Effect: "Allow",
        Action: [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:GetObjectTagging"
        ],
        Resource: ("arn:aws:s3:::" + $bucket + "/" + $prefix + "*")
      }
    ]
  }')"
AWS_REGION="${AWS_REGION}" aws iam put-role-policy \
  --role-name "${CODE_INTERPRETER_ROLE_NAME}" \
  --policy-name "OpenWebUILocalTestS3Read" \
  --policy-document "${CODE_INTERPRETER_POLICY}"

echo "AWS test storage is ready."
echo "  Bucket: s3://${S3_BUCKET_NAME}/${S3_KEY_PREFIX}/"
echo "  Role:   ${AWS_ROLE_ARN}"
echo "  Reader: arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/${CODE_INTERPRETER_ROLE_NAME}"
