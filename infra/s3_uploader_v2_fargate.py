"""Render the reviewed S3 Uploader v2 Fargate CloudFormation template.

The module is intentionally parameterised: rendering it never changes AWS,
Route 53, the current EC2 uploader, or an existing ALB listener.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DOMAIN = "s3-uploader-v2.bot-alex.com"
V3_GLUE_JOB_NAME = "s3-uploader-v3-ingest"
V3_GLUE_SCRIPT_URI = "s3://ah-data-analytics/temp_s3_update/s3_uploader_v3/generic_glue_job.py"


def render_template() -> dict[str, Any]:
    template = {
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
            "EnableV3Leases": {"Type": "String", "AllowedValues": ["true", "false"], "Default": "false"},
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
            "BaseWorkerQueue": {"Type": "AWS::SQS::Queue", "Properties": {"FifoQueue": True, "ContentBasedDeduplication": False, "QueueName": "s3-uploader-v3-base.fifo", "VisibilityTimeout": 3600, "RedrivePolicy": {"deadLetterTargetArn": {"Fn::GetAtt": ["DeadLetterQueue", "Arn"]}, "maxReceiveCount": 2}}},
            "LargeWorkerQueue": {"Type": "AWS::SQS::Queue", "Properties": {"FifoQueue": True, "ContentBasedDeduplication": False, "QueueName": "s3-uploader-v3-large.fifo", "VisibilityTimeout": 3600, "RedrivePolicy": {"deadLetterTargetArn": {"Fn::GetAtt": ["DeadLetterQueue", "Arn"]}, "maxReceiveCount": 2}}},
            "ApiSecurityGroup": {"Type": "AWS::EC2::SecurityGroup", "Properties": {"GroupDescription": "S3 uploader v2 API", "VpcId": {"Ref": "VpcId"}, "SecurityGroupIngress": [{"IpProtocol": "tcp", "FromPort": 8090, "ToPort": 8090, "SourceSecurityGroupId": {"Ref": "ExistingAlbSecurityGroupId"}}]}},
            "ApiTaskRole": {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}, "Policies": [{"PolicyName": "api-job-control", "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListBucketMultipartUploads", "s3:ListMultipartUploadParts"], "Resource": [{"Fn::GetAtt": ["LandingBucket", "Arn"]}, {"Fn::Sub": "${LandingBucket.Arn}/*"}]}, {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": {"Fn::GetAtt": ["LandingBucket", "Arn"]}, "Condition": {"StringLike": {"s3:prefix": ["s3-uploader-v2/audit/*"]}}}, {"Effect": "Allow", "Action": "s3:GetObject", "Resource": ["arn:aws:s3:::ah-data-analytics/temp_s3_update/web_ingest/table_contracts/*", "arn:aws:s3:::ah-data-analytics/temp_s3_update/web_ingest/upload_history/*"]}, {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::ah-data-analytics", "Condition": {"StringLike": {"s3:prefix": ["temp_s3_update/web_ingest/upload_history/*"]}}}, {"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": [{"Fn::GetAtt": ["JobQueue", "Arn"]}, {"Fn::GetAtt": ["BaseWorkerQueue", "Arn"]}, {"Fn::GetAtt": ["LargeWorkerQueue", "Arn"]}]}, {"Effect": "Allow", "Action": "s3tables:ListTableBuckets", "Resource": "*"}, {"Effect": "Allow", "Action": ["s3tables:GetTableBucket", "s3tables:ListNamespaces", "s3tables:GetNamespace", "s3tables:CreateNamespace", "s3tables:ListTables", "s3tables:GetTable", "s3tables:DeleteTable"], "Resource": [{"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/ah-analytics"}, {"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/ah-analytics/*"}, {"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/ah-soc-delta-pilot"}, {"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/ah-soc-delta-pilot/*"}, {"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/nuh-analytics"}, {"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/nuh-analytics/*"}]}, {"Effect": "Allow", "Action": ["glue:GetJobRun", "glue:StartJobRun"], "Resource": {"Fn::Sub": "arn:${AWS::Partition}:glue:${AWS::Region}:${AWS::AccountId}:job/ah-soc-delta-pilot-web-ingest"}}, {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": [{"Ref": "LoginPasswordSecretArn"}, {"Ref": "LoginSigningSecretArn"}]}]}}]}},
            "WorkerTaskRole": {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}, "Policies": [{"PolicyName": "worker-data", "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject"], "Resource": [{"Fn::GetAtt": ["LandingBucket", "Arn"]}, {"Fn::Sub": "${LandingBucket.Arn}/*"}]}, {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": {"Fn::GetAtt": ["LandingBucket", "Arn"]}, "Condition": {"StringLike": {"s3:prefix": ["jobs/*", "sessions/*"]}}}, {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "arn:aws:s3:::ah-data-analytics/temp_s3_update/web_ingest/table_contracts/*"}, {"Effect": "Allow", "Action": "glue:StartJobRun", "Resource": {"Fn::Sub": "arn:${AWS::Partition}:glue:${AWS::Region}:${AWS::AccountId}:job/ah-soc-delta-pilot-web-ingest"}}, {"Effect": "Allow", "Action":"secretsmanager:GetSecretValue", "Resource": {"Ref": "EncryptionSecretArn"}}]}}]}},
            "PipeRole": {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "pipes.amazonaws.com"}, "Action": "sts:AssumeRole"}]}, "Policies": [{"PolicyName": "dispatch-worker", "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], "Resource": [{"Fn::GetAtt": ["JobQueue", "Arn"]}, {"Fn::GetAtt": ["BaseWorkerQueue", "Arn"]}, {"Fn::GetAtt": ["LargeWorkerQueue", "Arn"]}]}, {"Effect": "Allow", "Action": "ecs:RunTask", "Resource": {"Fn::Sub": "arn:${AWS::Partition}:ecs:${AWS::Region}:${AWS::AccountId}:task-definition/s3-uploader-v2-worker:*"}}, {"Effect": "Allow", "Action": "iam:PassRole", "Resource": [{"Fn::GetAtt": ["ExecutionRole", "Arn"]}, {"Fn::GetAtt": ["WorkerTaskRole", "Arn"]}]}]}}]}},
            "ExecutionRole": {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}, "ManagedPolicyArns": ["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"], "Policies": [{"PolicyName": "read-task-secrets", "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": [{"Ref": "LoginPasswordSecretArn"}, {"Ref": "LoginSigningSecretArn"}]}]}}]}},
            "ApiTaskDefinition": {"Type": "AWS::ECS::TaskDefinition", "Properties": {"Family": "s3-uploader-v2-api", "RequiresCompatibilities": ["FARGATE"], "NetworkMode": "awsvpc", "Cpu": "1024", "Memory": "2048", "ExecutionRoleArn": {"Fn::GetAtt": ["ExecutionRole", "Arn"]}, "TaskRoleArn": {"Fn::GetAtt": ["ApiTaskRole", "Arn"]}, "ContainerDefinitions": [{"Name": "api", "Image": {"Ref": "ApiImageUri"}, "Essential": True, "PortMappings": [{"ContainerPort": 8090}], "Environment": [{"Name": "AWS_REGION", "Value": {"Ref": "AWS::Region"}}, {"Name": "S3_UPLOADER_V2_LANDING_BUCKET", "Value": {"Ref": "LandingBucket"}}, {"Name": "S3_UPLOADER_V2_QUEUE_URL", "Value": {"Ref": "JobQueue"}}, {"Name": "S3_UPLOADER_V3_BASE_QUEUE_URL", "Value": {"Ref": "BaseWorkerQueue"}}, {"Name": "S3_UPLOADER_V3_LARGE_QUEUE_URL", "Value": {"Ref": "LargeWorkerQueue"}}, {"Name": "S3_UPLOADER_V3_LEASES_ENABLED", "Value": {"Ref": "EnableV3Leases"}}, {"Name": "S3_UPLOADER_V2_API_BASE_URL", "Value": "https://s3-uploader-v2.bot-alex.com"}, {"Name": "S3_UPLOADER_V2_GLUE_JOB_NAME", "Value": "ah-soc-delta-pilot-web-ingest"}], "Secrets": [{"Name": "S3_UPLOADER_V2_LOGIN_PASSWORD", "ValueFrom": {"Ref": "LoginPasswordSecretArn"}}, {"Name": "S3_UPLOADER_V2_LOGIN_SECRET", "ValueFrom": {"Ref": "LoginSigningSecretArn"}}], "LogConfiguration": {"LogDriver": "awslogs", "Options": {"awslogs-group": {"Ref": "ApiLogGroup"}, "awslogs-region": {"Ref": "AWS::Region"}, "awslogs-stream-prefix": "api"}}}]}},
            "WorkerTaskDefinition": {"Type": "AWS::ECS::TaskDefinition", "Properties": {"Family": "s3-uploader-v2-worker", "RequiresCompatibilities": ["FARGATE"], "NetworkMode": "awsvpc", "Cpu": "8192", "Memory": "32768", "EphemeralStorage": {"SizeInGiB": 100}, "ExecutionRoleArn": {"Fn::GetAtt": ["ExecutionRole", "Arn"]}, "TaskRoleArn": {"Fn::GetAtt": ["WorkerTaskRole", "Arn"]}, "ContainerDefinitions": [{"Name": "worker", "Image": {"Ref": "WorkerImageUri"}, "Essential": True, "Environment": [{"Name": "AWS_REGION", "Value": {"Ref": "AWS::Region"}}, {"Name": "S3_UPLOADER_V2_LANDING_BUCKET", "Value": {"Ref": "LandingBucket"}}, {"Name": "S3_UPLOADER_V2_GLUE_JOB_NAME", "Value": "ah-soc-delta-pilot-web-ingest"}, {"Name": "S3_UPLOADER_V2_ENCRYPTION_SECRET_ARN", "Value": {"Ref": "EncryptionSecretArn"}}], "LogConfiguration": {"LogDriver": "awslogs", "Options": {"awslogs-group": {"Ref": "WorkerLogGroup"}, "awslogs-region": {"Ref": "AWS::Region"}, "awslogs-stream-prefix": "worker"}}}]}},
            "BaseWorkerTaskDefinition": {"Type": "AWS::ECS::TaskDefinition", "Properties": {"Family": "s3-uploader-v2-worker", "RequiresCompatibilities": ["FARGATE"], "NetworkMode": "awsvpc", "Cpu": "4096", "Memory": "16384", "EphemeralStorage": {"SizeInGiB": 100}, "ExecutionRoleArn": {"Fn::GetAtt": ["ExecutionRole", "Arn"]}, "TaskRoleArn": {"Fn::GetAtt": ["WorkerTaskRole", "Arn"]}, "ContainerDefinitions": [{"Name": "worker", "Image": {"Ref": "WorkerImageUri"}, "Essential": True, "Environment": [{"Name": "AWS_REGION", "Value": {"Ref": "AWS::Region"}}, {"Name": "S3_UPLOADER_V2_LANDING_BUCKET", "Value": {"Ref": "LandingBucket"}}, {"Name": "S3_UPLOADER_V2_GLUE_JOB_NAME", "Value": "ah-soc-delta-pilot-web-ingest"}, {"Name": "S3_UPLOADER_V2_ENCRYPTION_SECRET_ARN", "Value": {"Ref": "EncryptionSecretArn"}}], "LogConfiguration": {"LogDriver": "awslogs", "Options": {"awslogs-group": {"Ref": "WorkerLogGroup"}, "awslogs-region": {"Ref": "AWS::Region"}, "awslogs-stream-prefix": "worker"}}}]}},
            "ApiLogGroup": {"Type": "AWS::Logs::LogGroup", "Properties": {"RetentionInDays": 30}}, "WorkerLogGroup": {"Type": "AWS::Logs::LogGroup", "Properties": {"RetentionInDays": 30}},
            "ApiTargetGroup": {"Type": "AWS::ElasticLoadBalancingV2::TargetGroup", "Properties": {"TargetType": "ip", "Port": 8090, "Protocol": "HTTP", "VpcId": {"Ref": "VpcId"}, "HealthCheckPath": "/healthz"}},
            "ApiService": {"Type": "AWS::ECS::Service", "Properties": {"Cluster": {"Ref": "ClusterArn"}, "DesiredCount": 1, "LaunchType": "FARGATE", "TaskDefinition": {"Ref": "ApiTaskDefinition"}, "NetworkConfiguration": {"AwsvpcConfiguration": {"AssignPublicIp": "ENABLED", "SecurityGroups": [{"Ref": "ApiSecurityGroup"}], "Subnets": {"Ref": "PrivateSubnets"}}}, "LoadBalancers": [{"ContainerName": "api", "ContainerPort": 8090, "TargetGroupArn": {"Ref": "ApiTargetGroup"}}]}},
            "HttpsCertificate": {"Type": "AWS::ElasticLoadBalancingV2::ListenerCertificate", "Properties": {"ListenerArn": {"Ref": "AlbListenerArn"}, "Certificates": [{"CertificateArn": {"Ref": "CertificateArn"}}]}},
            "HostRule": {"Type": "AWS::ElasticLoadBalancingV2::ListenerRule", "Properties": {"ListenerArn": {"Ref": "AlbListenerArn"}, "Priority": 49000, "Conditions": [{"Field": "host-header", "HostHeaderConfig": {"Values": [DOMAIN]}}], "Actions": [{"Type": "forward", "TargetGroupArn": {"Ref": "ApiTargetGroup"}}]}},
            "WorkerPipe": {"Type": "AWS::Pipes::Pipe", "Properties": {"RoleArn": {"Fn::GetAtt": ["PipeRole", "Arn"]}, "Source": {"Fn::GetAtt": ["JobQueue", "Arn"]}, "Target": {"Ref": "ClusterArn"}, "SourceParameters": {"SqsQueueParameters": {"BatchSize": 1}}, "TargetParameters": {"EcsTaskParameters": {"TaskDefinitionArn": {"Ref": "WorkerTaskDefinition"}, "TaskCount": 1, "LaunchType": "FARGATE", "Overrides": {"ContainerOverrides": [{"Name": "worker", "Environment": [{"Name": "S3_UPLOADER_V2_JOB_ID", "Value": "$.body"}]}]}, "NetworkConfiguration": {"AwsvpcConfiguration": {"AssignPublicIp": "ENABLED", "SecurityGroups": [{"Ref": "ApiSecurityGroup"}], "Subnets": {"Ref": "PrivateSubnets"}}}}}}},
            "BaseWorkerPipe": {"Type": "AWS::Pipes::Pipe", "Properties": {"RoleArn": {"Fn::GetAtt": ["PipeRole", "Arn"]}, "Source": {"Fn::GetAtt": ["BaseWorkerQueue", "Arn"]}, "Target": {"Ref": "ClusterArn"}, "SourceParameters": {"SqsQueueParameters": {"BatchSize": 1}}, "TargetParameters": {"EcsTaskParameters": {"TaskDefinitionArn": {"Ref": "BaseWorkerTaskDefinition"}, "TaskCount": 1, "LaunchType": "FARGATE", "Overrides": {"ContainerOverrides": [{"Name": "worker", "Environment": [{"Name": "S3_UPLOADER_V2_JOB_ID", "Value": "$.body"}]}]}, "NetworkConfiguration": {"AwsvpcConfiguration": {"AssignPublicIp": "ENABLED", "SecurityGroups": [{"Ref": "ApiSecurityGroup"}], "Subnets": {"Ref": "PrivateSubnets"}}}}}}},
            "LargeWorkerPipe": {"Type": "AWS::Pipes::Pipe", "Properties": {"RoleArn": {"Fn::GetAtt": ["PipeRole", "Arn"]}, "Source": {"Fn::GetAtt": ["LargeWorkerQueue", "Arn"]}, "Target": {"Ref": "ClusterArn"}, "SourceParameters": {"SqsQueueParameters": {"BatchSize": 1}}, "TargetParameters": {"EcsTaskParameters": {"TaskDefinitionArn": {"Ref": "WorkerTaskDefinition"}, "TaskCount": 1, "LaunchType": "FARGATE", "Overrides": {"ContainerOverrides": [{"Name": "worker", "Environment": [{"Name": "S3_UPLOADER_V2_JOB_ID", "Value": "$.body"}]}]}, "NetworkConfiguration": {"AwsvpcConfiguration": {"AssignPublicIp": "ENABLED", "SecurityGroups": [{"Ref": "ApiSecurityGroup"}], "Subnets": {"Ref": "PrivateSubnets"}}}}}}},
            "DnsRecord": {"Type": "AWS::Route53::RecordSet", "Properties": {"HostedZoneId": {"Ref": "HostedZoneId"}, "Name": DOMAIN + ".", "Type": "A", "AliasTarget": {"DNSName": {"Ref": "AlbDnsName"}, "HostedZoneId": {"Ref": "AlbCanonicalHostedZoneId"}, "EvaluateTargetHealth": True}}},
        },
    }

    # V1's current Glue job remains untouched. V3 gets a dedicated job so a
    # V1 asset refresh cannot silently overwrite V3's verified rollback code.
    resources = template["Resources"]
    v3_job_arn = {"Fn::Sub": "arn:${AWS::Partition}:glue:${AWS::Region}:${AWS::AccountId}:job/${V3GlueJob}"}
    resources["V3GlueJob"] = {
        "Type": "AWS::Glue::Job",
        "Properties": {
            "Name": V3_GLUE_JOB_NAME,
            "Role": {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/ah-soc-delta-pilot-glue-role"},
            "Command": {"Name": "glueetl", "ScriptLocation": V3_GLUE_SCRIPT_URI, "PythonVersion": "3"},
            "GlueVersion": "5.0", "WorkerType": "G.1X", "NumberOfWorkers": 4,
            "Timeout": 60, "MaxRetries": 0, "ExecutionProperty": {"MaxConcurrentRuns": 5},
            "DefaultArguments": {
                "--job-language": "python", "--datalake-formats": "iceberg", "--enable-metrics": "true",
                "--enable-continuous-cloudwatch-log": "true",
                "--conf": {"Fn::Sub": " ".join([
                    "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
                    "--conf spark.sql.legacy.timeParserPolicy=CORRECTED",
                    "--conf spark.sql.catalog.s3_rest_catalog=org.apache.iceberg.spark.SparkCatalog",
                    "--conf spark.sql.catalog.s3_rest_catalog.type=rest",
                    "--conf spark.sql.catalog.s3_rest_catalog.uri=https://s3tables.${AWS::Region}.amazonaws.com/iceberg",
                    "--conf spark.sql.catalog.s3_rest_catalog.warehouse=arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/ah-soc-delta-pilot",
                    "--conf spark.sql.catalog.s3_rest_catalog.rest.sigv4-enabled=true",
                    "--conf spark.sql.catalog.s3_rest_catalog.rest.signing-name=s3tables",
                    "--conf spark.sql.catalog.s3_rest_catalog.rest.signing-region=${AWS::Region}",
                    "--conf spark.sql.catalog.s3_rest_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO",
                ])},
            },
        },
    }
    for role_name in ("ApiTaskRole", "WorkerTaskRole"):
        statements = resources[role_name]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        for statement in statements:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            if "glue:StartJobRun" in actions:
                statement["Resource"] = v3_job_arn
    api_statements = resources["ApiTaskRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    # Administrators discover and manage all account-local S3 Table buckets.
    # Keep generic S3 access confined to managed Iceberg metadata; table data
    # remains reachable only through S3 Tables table-scoped permissions.
    table_bucket_resource = {"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/*"}
    table_resource = {"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/*/table/*"}
    control_statement = next(
        statement for statement in api_statements
        if "s3tables:GetTableBucket" in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    )
    control_statement["Action"] = [
        "s3tables:GetTableBucket", "s3tables:ListNamespaces", "s3tables:GetNamespace",
        "s3tables:CreateNamespace", "s3tables:ListTables",
    ]
    control_statement["Resource"] = [table_bucket_resource]
    api_statements.extend([
        {
            # A table bucket ARN does not exist until this call succeeds, so
            # AWS requires the create action to use the account-wide resource.
            "Effect": "Allow", "Action": "s3tables:CreateTableBucket", "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": ["s3tables:GetTable", "s3tables:DeleteTable", "s3tables:GetTableData", "s3tables:GetTableMetadataLocation"],
            "Resource": [table_resource],
        },
        {
            "Effect": "Allow", "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::*--table-s3/metadata/*",
        },
        {
            "Effect": "Allow", "Action": "s3:PutObject",
            "Resource": "arn:aws:s3:::ah-data-analytics/temp_s3_update/web_ingest/table_contracts/*",
        },
    ])
    for task_definition in ("ApiTaskDefinition", "WorkerTaskDefinition", "BaseWorkerTaskDefinition"):
        environment = resources[task_definition]["Properties"]["ContainerDefinitions"][0]["Environment"]
        for variable in environment:
            if variable["Name"] == "S3_UPLOADER_V2_GLUE_JOB_NAME":
                variable["Value"] = {"Ref": "V3GlueJob"}
                break
    return template


def main() -> None:
    print(json.dumps(render_template(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
