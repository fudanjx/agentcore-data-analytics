"""Render the reviewed S3 Uploader v2 Fargate CloudFormation template.

The module is intentionally parameterised: rendering it never changes AWS,
Route 53, the current EC2 uploader, or an existing ALB listener.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DOMAIN = "s3-uploader-v2.bot-alex.com"


def render_template() -> dict[str, Any]:
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "S3 Uploader v2: API Fargate service and isolated worker tasks",
        "Parameters": {
            "ClusterArn": {"Type": "String"}, "VpcId": {"Type": "AWS::EC2::VPC::Id"},
            "PrivateSubnets": {"Type": "List<AWS::EC2::Subnet::Id>"}, "AlbListenerArn": {"Type": "String"},
            "HostedZoneId": {"Type": "String"}, "CertificateArn": {"Type": "String"},
            "AlbDnsName": {"Type": "String"}, "AlbCanonicalHostedZoneId": {"Type": "String"},
            "ApiImageUri": {"Type": "String"}, "WorkerImageUri": {"Type": "String"},
            "LoginPasswordSecretArn": {"Type": "String"}, "LoginSigningSecretArn": {"Type": "String"},
            "EncryptionSecretArn": {"Type": "String"},
            "ExistingAlbSecurityGroupId": {"Type": "AWS::EC2::SecurityGroup::Id"},
        },
        "Resources": {
            "LandingBucket": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "VersioningConfiguration": {"Status": "Enabled"},
                    "BucketEncryption": {"ServerSideEncryptionConfiguration": [{"ServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]},
                    "CorsConfiguration": {"CorsRules": [{"AllowedOrigins": ["https://s3-uploader-v2.bot-alex.com"], "AllowedMethods": ["PUT"], "AllowedHeaders": ["*"], "ExposedHeaders": ["ETag"], "MaxAge": 900}]},
                    "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True},
                    "LifecycleConfiguration": {"Rules": [
                        {"Id": "expire-raw", "Status": "Enabled", "Prefix": "s3-uploader-v2/uploads/", "ExpirationInDays": 1},
                        {"Id": "expire-prepared", "Status": "Enabled", "Prefix": "s3-uploader-v2/jobs/", "ExpirationInDays": 30},
                    ]},
                },
            },
            "DeadLetterQueue": {"Type": "AWS::SQS::Queue", "Properties": {"FifoQueue": True, "QueueName": "s3-uploader-v2-dlq.fifo", "MessageRetentionPeriod": 1209600}},
            "JobQueue": {"Type": "AWS::SQS::Queue", "Properties": {"FifoQueue": True, "ContentBasedDeduplication": False, "QueueName": "s3-uploader-v2-jobs.fifo", "VisibilityTimeout": 3600, "RedrivePolicy": {"deadLetterTargetArn": {"Fn::GetAtt": ["DeadLetterQueue", "Arn"]}, "maxReceiveCount": 2}}},
            "ApiSecurityGroup": {"Type": "AWS::EC2::SecurityGroup", "Properties": {"GroupDescription": "S3 uploader v2 API", "VpcId": {"Ref": "VpcId"}, "SecurityGroupIngress": [{"IpProtocol": "tcp", "FromPort": 8090, "ToPort": 8090, "SourceSecurityGroupId": {"Ref": "ExistingAlbSecurityGroupId"}}]}},
            "ApiTaskRole": {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}, "Policies": [{"PolicyName": "api-job-control", "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListBucketMultipartUploads", "s3:ListMultipartUploadParts"], "Resource": [{"Fn::GetAtt": ["LandingBucket", "Arn"]}, {"Fn::Sub": "${LandingBucket.Arn}/*"}]}, {"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": {"Fn::GetAtt": ["JobQueue", "Arn"]}}, {"Effect": "Allow", "Action": "glue:GetJobRun", "Resource": {"Fn::Sub": "arn:${AWS::Partition}:glue:${AWS::Region}:${AWS::AccountId}:job/ah-soc-delta-pilot-web-ingest"}}, {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": [{"Ref": "LoginPasswordSecretArn"}, {"Ref": "LoginSigningSecretArn"}]}]}}]}},
            "WorkerTaskRole": {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}, "Policies": [{"PolicyName": "worker-data", "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], "Resource": [{"Fn::GetAtt": ["LandingBucket", "Arn"]}, {"Fn::Sub": "${LandingBucket.Arn}/*"}]}, {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": {"Fn::GetAtt": ["LandingBucket", "Arn"]}, "Condition": {"StringLike": {"s3:prefix": ["jobs/*", "sessions/*"]}}}, {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::ah-data-analytics/temp_s3_update/web_ingest/table_contracts/*"}, {"Effect": "Allow", "Action": "glue:StartJobRun", "Resource": {"Fn::Sub": "arn:${AWS::Partition}:glue:${AWS::Region}:${AWS::AccountId}:job/ah-soc-delta-pilot-web-ingest"}}, {"Effect": "Allow", "Action":"secretsmanager:GetSecretValue", "Resource": {"Ref": "EncryptionSecretArn"}}]}}]}},
            "PipeRole": {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "pipes.amazonaws.com"}, "Action": "sts:AssumeRole"}]}, "Policies": [{"PolicyName": "dispatch-worker", "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], "Resource": {"Fn::GetAtt": ["JobQueue", "Arn"]}}, {"Effect": "Allow", "Action": "ecs:RunTask", "Resource": {"Fn::Sub": "arn:${AWS::Partition}:ecs:${AWS::Region}:${AWS::AccountId}:task-definition/s3-uploader-v2-worker:*"}}, {"Effect": "Allow", "Action": "iam:PassRole", "Resource": [{"Fn::GetAtt": ["ExecutionRole", "Arn"]}, {"Fn::GetAtt": ["WorkerTaskRole", "Arn"]}]}]}}]}},
            "ExecutionRole": {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}, "ManagedPolicyArns": ["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"], "Policies": [{"PolicyName": "read-task-secrets", "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": [{"Ref": "LoginPasswordSecretArn"}, {"Ref": "LoginSigningSecretArn"}]}]}}]}},
            "ApiTaskDefinition": {"Type": "AWS::ECS::TaskDefinition", "Properties": {"Family": "s3-uploader-v2-api", "RequiresCompatibilities": ["FARGATE"], "NetworkMode": "awsvpc", "Cpu": "1024", "Memory": "2048", "ExecutionRoleArn": {"Fn::GetAtt": ["ExecutionRole", "Arn"]}, "TaskRoleArn": {"Fn::GetAtt": ["ApiTaskRole", "Arn"]}, "ContainerDefinitions": [{"Name": "api", "Image": {"Ref": "ApiImageUri"}, "Essential": True, "PortMappings": [{"ContainerPort": 8090}], "Environment": [{"Name": "AWS_REGION", "Value": {"Ref": "AWS::Region"}}, {"Name": "S3_UPLOADER_V2_LANDING_BUCKET", "Value": {"Ref": "LandingBucket"}}, {"Name": "S3_UPLOADER_V2_QUEUE_URL", "Value": {"Ref": "JobQueue"}}, {"Name": "S3_UPLOADER_V2_API_BASE_URL", "Value": "https://s3-uploader-v2.bot-alex.com"}, {"Name": "S3_UPLOADER_V2_GLUE_JOB_NAME", "Value": "ah-soc-delta-pilot-web-ingest"}], "Secrets": [{"Name": "S3_UPLOADER_V2_LOGIN_PASSWORD", "ValueFrom": {"Ref": "LoginPasswordSecretArn"}}, {"Name": "S3_UPLOADER_V2_LOGIN_SECRET", "ValueFrom": {"Ref": "LoginSigningSecretArn"}}], "LogConfiguration": {"LogDriver": "awslogs", "Options": {"awslogs-group": {"Ref": "ApiLogGroup"}, "awslogs-region": {"Ref": "AWS::Region"}, "awslogs-stream-prefix": "api"}}}]}},
            "WorkerTaskDefinition": {"Type": "AWS::ECS::TaskDefinition", "Properties": {"Family": "s3-uploader-v2-worker", "RequiresCompatibilities": ["FARGATE"], "NetworkMode": "awsvpc", "Cpu": "8192", "Memory": "32768", "EphemeralStorage": {"SizeInGiB": 100}, "ExecutionRoleArn": {"Fn::GetAtt": ["ExecutionRole", "Arn"]}, "TaskRoleArn": {"Fn::GetAtt": ["WorkerTaskRole", "Arn"]}, "ContainerDefinitions": [{"Name": "worker", "Image": {"Ref": "WorkerImageUri"}, "Essential": True, "Environment": [{"Name": "AWS_REGION", "Value": {"Ref": "AWS::Region"}}, {"Name": "S3_UPLOADER_V2_LANDING_BUCKET", "Value": {"Ref": "LandingBucket"}}, {"Name": "S3_UPLOADER_V2_GLUE_JOB_NAME", "Value": "ah-soc-delta-pilot-web-ingest"}, {"Name": "S3_UPLOADER_V2_ENCRYPTION_SECRET_ARN", "Value": {"Ref": "EncryptionSecretArn"}}], "LogConfiguration": {"LogDriver": "awslogs", "Options": {"awslogs-group": {"Ref": "WorkerLogGroup"}, "awslogs-region": {"Ref": "AWS::Region"}, "awslogs-stream-prefix": "worker"}}}]}},
            "ApiLogGroup": {"Type": "AWS::Logs::LogGroup", "Properties": {"RetentionInDays": 30}}, "WorkerLogGroup": {"Type": "AWS::Logs::LogGroup", "Properties": {"RetentionInDays": 30}},
            "ApiTargetGroup": {"Type": "AWS::ElasticLoadBalancingV2::TargetGroup", "Properties": {"TargetType": "ip", "Port": 8090, "Protocol": "HTTP", "VpcId": {"Ref": "VpcId"}, "HealthCheckPath": "/healthz"}},
            "ApiService": {"Type": "AWS::ECS::Service", "Properties": {"Cluster": {"Ref": "ClusterArn"}, "DesiredCount": 1, "LaunchType": "FARGATE", "TaskDefinition": {"Ref": "ApiTaskDefinition"}, "NetworkConfiguration": {"AwsvpcConfiguration": {"AssignPublicIp": "ENABLED", "SecurityGroups": [{"Ref": "ApiSecurityGroup"}], "Subnets": {"Ref": "PrivateSubnets"}}}, "LoadBalancers": [{"ContainerName": "api", "ContainerPort": 8090, "TargetGroupArn": {"Ref": "ApiTargetGroup"}}]}},
            "HttpsCertificate": {"Type": "AWS::ElasticLoadBalancingV2::ListenerCertificate", "Properties": {"ListenerArn": {"Ref": "AlbListenerArn"}, "Certificates": [{"CertificateArn": {"Ref": "CertificateArn"}}]}},
            "HostRule": {"Type": "AWS::ElasticLoadBalancingV2::ListenerRule", "Properties": {"ListenerArn": {"Ref": "AlbListenerArn"}, "Priority": 49000, "Conditions": [{"Field": "host-header", "HostHeaderConfig": {"Values": [DOMAIN]}}], "Actions": [{"Type": "forward", "TargetGroupArn": {"Ref": "ApiTargetGroup"}}]}},
            "WorkerPipe": {"Type": "AWS::Pipes::Pipe", "Properties": {"RoleArn": {"Fn::GetAtt": ["PipeRole", "Arn"]}, "Source": {"Fn::GetAtt": ["JobQueue", "Arn"]}, "Target": {"Ref": "ClusterArn"}, "SourceParameters": {"SqsQueueParameters": {"BatchSize": 1}}, "TargetParameters": {"EcsTaskParameters": {"TaskDefinitionArn": {"Ref": "WorkerTaskDefinition"}, "TaskCount": 1, "LaunchType": "FARGATE", "Overrides": {"ContainerOverrides": [{"Name": "worker", "Environment": [{"Name": "S3_UPLOADER_V2_JOB_ID", "Value": "$.body"}]}]}, "NetworkConfiguration": {"AwsvpcConfiguration": {"AssignPublicIp": "ENABLED", "SecurityGroups": [{"Ref": "ApiSecurityGroup"}], "Subnets": {"Ref": "PrivateSubnets"}}}}}}},
            "DnsRecord": {"Type": "AWS::Route53::RecordSet", "Properties": {"HostedZoneId": {"Ref": "HostedZoneId"}, "Name": DOMAIN + ".", "Type": "A", "AliasTarget": {"DNSName": {"Ref": "AlbDnsName"}, "HostedZoneId": {"Ref": "AlbCanonicalHostedZoneId"}, "EvaluateTargetHealth": True}}},
        },
    }


def main() -> None:
    print(json.dumps(render_template(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
