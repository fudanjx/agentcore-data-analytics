"""
Grant the EKS Fargate pod execution role permission to invoke the AgentCore Runtime.

Run once before deploying the proxy:
    python infra/grant_eks_access.py
"""

import json
import boto3

REGION = "ap-southeast-1"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/agentcore_poc-iumXW8638m"
FARGATE_ROLE_NAME = "AmazonEKSFargatePodExecutionRole"
POLICY_NAME = "agentcore-invoke"

iam = boto3.client("iam")


def main():
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:InvokeAgentRuntime",
                ],
                "Resource": RUNTIME_ARN,
            }
        ],
    }

    print(f"Attaching inline policy '{POLICY_NAME}' to role '{FARGATE_ROLE_NAME}'...")
    iam.put_role_policy(
        RoleName=FARGATE_ROLE_NAME,
        PolicyName=POLICY_NAME,
        PolicyDocument=json.dumps(policy),
    )
    print("Done. EKS Fargate pods can now call invoke_agent_runtime.")
    print(f"\nRuntime ARN: {RUNTIME_ARN}")


if __name__ == "__main__":
    main()
