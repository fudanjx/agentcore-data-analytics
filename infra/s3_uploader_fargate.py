"""Render the production S3 uploader Fargate CloudFormation template.

This stack is blue-green safe: it references the existing landing bucket and
keeps the historical hostname's ALB rule disabled until smoke tests pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DOMAIN = "s3-uploader-v2.bot-alex.com"  # Retained public hostname.
GLUE_JOB_NAME = "s3-uploader-ingest"
GLUE_SCRIPT_URI = "s3://ah-data-analytics/temp_s3_update/s3_uploader/generic_glue_job.py"
LANDING_PREFIX = "s3-uploader"
HISTORICAL_PREFIX = "s3-uploader-v2"
CONTRACT_BUCKET = "ah-data-analytics"
CONTRACT_PREFIX = "temp_s3_update/web_ingest/table_contracts"
HISTORY_PREFIX = "temp_s3_update/web_ingest/upload_history"


def _role(assumed_by: str, statements: list[dict[str, Any]], *, managed: list[str] | None = None) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": assumed_by}, "Action": "sts:AssumeRole"}]},
        "Policies": [{"PolicyName": "s3-uploader-production", "PolicyDocument": {"Version": "2012-10-17", "Statement": statements}}],
    }
    if managed:
        properties["ManagedPolicyArns"] = managed
    return {"Type": "AWS::IAM::Role", "Properties": properties}


def _environment(*items: tuple[str, Any]) -> list[dict[str, Any]]:
    return [{"Name": name, "Value": value} for name, value in items]


def _network() -> dict[str, Any]:
    return {"AwsvpcConfiguration": {"AssignPublicIp": "ENABLED", "SecurityGroups": [{"Ref": "ApiSecurityGroup"}], "Subnets": {"Ref": "PrivateSubnets"}}}


def _log(group: str, prefix: str) -> dict[str, Any]:
    return {"LogDriver": "awslogs", "Options": {"awslogs-group": {"Ref": group}, "awslogs-region": {"Ref": "AWS::Region"}, "awslogs-stream-prefix": prefix}}


def _worker_definition(*, cpu: str, memory: str) -> dict[str, Any]:
    return {
        "Type": "AWS::ECS::TaskDefinition",
        "Properties": {
            "Family": "s3-uploader-worker", "RequiresCompatibilities": ["FARGATE"], "NetworkMode": "awsvpc", "Cpu": cpu, "Memory": memory, "EphemeralStorage": {"SizeInGiB": 100},
            "ExecutionRoleArn": {"Fn::GetAtt": ["ExecutionRole", "Arn"]}, "TaskRoleArn": {"Fn::GetAtt": ["WorkerTaskRole", "Arn"]},
            "ContainerDefinitions": [{
                "Name": "worker", "Image": {"Ref": "WorkerImageUri"}, "Essential": True,
                "Environment": _environment(
                    ("AWS_REGION", {"Ref": "AWS::Region"}), ("S3_UPLOADER_LANDING_BUCKET", {"Ref": "LandingBucketName"}),
                    ("S3_UPLOADER_LANDING_PREFIX", LANDING_PREFIX), ("S3_UPLOADER_GLUE_JOB_NAME", {"Ref": "GlueJob"}),
                    ("S3_UPLOADER_CONTRACT_BUCKET", CONTRACT_BUCKET), ("S3_UPLOADER_CONTRACT_PREFIX", CONTRACT_PREFIX),
                    ("S3_UPLOADER_MUTATION_QUEUE_URL", {"Ref": "MutationQueue"}), ("S3_UPLOADER_ENCRYPTION_SECRET_ARN", {"Ref": "EncryptionSecretArn"}),
                ), "LogConfiguration": _log("WorkerLogGroup", "worker"),
            }],
        },
    }


def _worker_pipe(task_definition: str, source_queue: str) -> dict[str, Any]:
    return {
        "Type": "AWS::Pipes::Pipe",
        "Properties": {
            "RoleArn": {"Fn::GetAtt": ["WorkerPipeRole", "Arn"]}, "Source": {"Fn::GetAtt": [source_queue, "Arn"]}, "Target": {"Ref": "ClusterArn"},
            "SourceParameters": {"SqsQueueParameters": {"BatchSize": 1}},
            "TargetParameters": {"EcsTaskParameters": {
                "TaskDefinitionArn": {"Ref": task_definition}, "TaskCount": 1, "LaunchType": "FARGATE",
                "Overrides": {"ContainerOverrides": [{"Name": "worker", "Environment": [{"Name": "S3_UPLOADER_JOB_ID", "Value": "$.body"}]}]},
                "NetworkConfiguration": _network(),
            }},
        },
    }


def render_template() -> dict[str, Any]:
    landing_objects = {"Fn::Sub": "${LandingBucketArn}/s3-uploader/*"}
    historical_objects = {"Fn::Sub": f"${{LandingBucketArn}}/{HISTORICAL_PREFIX}/*"}
    landing_bucket = {"Ref": "LandingBucketArn"}
    glue_job_arn = {"Fn::Sub": "arn:${AWS::Partition}:glue:${AWS::Region}:${AWS::AccountId}:job/${GlueJob}"}
    table_bucket = {"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/*"}
    table = {"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/*/table/*"}
    landing_list = {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": landing_bucket, "Condition": {"StringLike": {"s3:prefix": [f"{LANDING_PREFIX}/*", f"{HISTORICAL_PREFIX}/*"]}}}
    landing_read = {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"], "Resource": [landing_objects, historical_objects]}
    api_landing_write = {"Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"], "Resource": [landing_objects]}
    worker_landing_write = {"Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject"], "Resource": [landing_objects]}

    resources: dict[str, Any] = {
        "WorkerLaunchDlq": {"Type": "AWS::SQS::Queue", "Properties": {"FifoQueue": True, "QueueName": "s3-uploader-worker-launch-dlq.fifo", "MessageRetentionPeriod": 1209600, "SqsManagedSseEnabled": True}},
        "MutationDlq": {"Type": "AWS::SQS::Queue", "Properties": {"FifoQueue": True, "QueueName": "s3-uploader-mutations-dlq.fifo", "MessageRetentionPeriod": 1209600, "SqsManagedSseEnabled": True}},
        "BaseWorkerQueue": {"Type": "AWS::SQS::Queue", "Properties": {"FifoQueue": True, "ContentBasedDeduplication": False, "QueueName": "s3-uploader-base.fifo", "VisibilityTimeout": 3600, "SqsManagedSseEnabled": True, "RedrivePolicy": {"deadLetterTargetArn": {"Fn::GetAtt": ["WorkerLaunchDlq", "Arn"]}, "maxReceiveCount": 2}}},
        "LargeWorkerQueue": {"Type": "AWS::SQS::Queue", "Properties": {"FifoQueue": True, "ContentBasedDeduplication": False, "QueueName": "s3-uploader-large.fifo", "VisibilityTimeout": 3600, "SqsManagedSseEnabled": True, "RedrivePolicy": {"deadLetterTargetArn": {"Fn::GetAtt": ["WorkerLaunchDlq", "Arn"]}, "maxReceiveCount": 2}}},
        "MutationQueue": {"Type": "AWS::SQS::Queue", "Properties": {"FifoQueue": True, "ContentBasedDeduplication": False, "QueueName": "s3-uploader-mutations.fifo", "VisibilityTimeout": 120, "SqsManagedSseEnabled": True, "RedrivePolicy": {"deadLetterTargetArn": {"Fn::GetAtt": ["MutationDlq", "Arn"]}, "maxReceiveCount": 5}}},
        "ApiSecurityGroup": {"Type": "AWS::EC2::SecurityGroup", "Properties": {"GroupDescription": "S3 uploader production API", "VpcId": {"Ref": "VpcId"}, "SecurityGroupIngress": [{"IpProtocol": "tcp", "FromPort": 8090, "ToPort": 8090, "SourceSecurityGroupId": {"Ref": "ExistingAlbSecurityGroupId"}}]}},
        "ApiLogGroup": {"Type": "AWS::Logs::LogGroup", "Properties": {"LogGroupName": "/ecs/s3-uploader/api", "RetentionInDays": 30}},
        "WorkerLogGroup": {"Type": "AWS::Logs::LogGroup", "Properties": {"LogGroupName": "/ecs/s3-uploader/worker", "RetentionInDays": 30}},
    }
    resources["ExecutionRole"] = _role("ecs-tasks.amazonaws.com", [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": [{"Ref": "LoginPasswordSecretArn"}, {"Ref": "LoginSigningSecretArn"}]}], managed=["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"])
    resources["ApiTaskRole"] = _role("ecs-tasks.amazonaws.com", [
        landing_read, api_landing_write, landing_list,
        {"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": [{"Fn::GetAtt": ["BaseWorkerQueue", "Arn"]}, {"Fn::GetAtt": ["LargeWorkerQueue", "Arn"]}, {"Fn::GetAtt": ["MutationQueue", "Arn"]}]},
        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": [{"Ref": "LoginPasswordSecretArn"}, {"Ref": "LoginSigningSecretArn"}]},
        {"Effect": "Allow", "Action": "s3tables:ListTableBuckets", "Resource": "*"},
        {"Effect": "Allow", "Action": ["s3tables:GetTableBucket", "s3tables:ListNamespaces", "s3tables:GetNamespace", "s3tables:CreateNamespace", "s3tables:ListTables"], "Resource": [table_bucket]},
        {"Effect": "Allow", "Action": "s3tables:CreateTableBucket", "Resource": "*"},
        {"Effect": "Allow", "Action": ["s3tables:GetTable", "s3tables:DeleteTable", "s3tables:GetTableData", "s3tables:GetTableMetadataLocation"], "Resource": [table]},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"arn:aws:s3:::{CONTRACT_BUCKET}/{CONTRACT_PREFIX}/*"},
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{CONTRACT_BUCKET}/{HISTORY_PREFIX}/*"},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{CONTRACT_BUCKET}", "Condition": {"StringLike": {"s3:prefix": [f"{HISTORY_PREFIX}/*"]}}},
        {"Effect": "Allow", "Action": ["glue:GetJobRun", "glue:StartJobRun"], "Resource": glue_job_arn},
    ])
    resources["WorkerTaskRole"] = _role("ecs-tasks.amazonaws.com", [
        landing_read, worker_landing_write, landing_list,
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"arn:aws:s3:::{CONTRACT_BUCKET}/{CONTRACT_PREFIX}/*"},
        {"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": {"Fn::GetAtt": ["MutationQueue", "Arn"]}},
        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": {"Ref": "EncryptionSecretArn"}},
    ])
    resources["DispatcherTaskRole"] = _role("ecs-tasks.amazonaws.com", [
        landing_read, worker_landing_write, landing_list,
        {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], "Resource": {"Fn::GetAtt": ["MutationQueue", "Arn"]}},
        {"Effect": "Allow", "Action": ["glue:GetJobRun", "glue:GetJobRuns", "glue:StartJobRun"], "Resource": glue_job_arn},
    ])
    resources["WorkerPipeRole"] = _role("pipes.amazonaws.com", [
        {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], "Resource": [{"Fn::GetAtt": ["BaseWorkerQueue", "Arn"]}, {"Fn::GetAtt": ["LargeWorkerQueue", "Arn"]}]},
        {"Effect": "Allow", "Action": "ecs:RunTask", "Resource": {"Fn::Sub": "arn:${AWS::Partition}:ecs:${AWS::Region}:${AWS::AccountId}:task-definition/s3-uploader-worker:*"}},
        {"Effect": "Allow", "Action": "iam:PassRole", "Resource": [{"Fn::GetAtt": ["ExecutionRole", "Arn"]}, {"Fn::GetAtt": ["WorkerTaskRole", "Arn"]}]},
    ])
    resources["GlueExecutionRole"] = _role("glue.amazonaws.com", [
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": {"Fn::Sub": "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:*"}},
        {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*", "Condition": {"StringEquals": {"cloudwatch:namespace": "Glue"}}},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": landing_objects},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"arn:aws:s3:::{CONTRACT_BUCKET}/temp_s3_update/*"},
        {"Effect": "Allow", "Action": ["s3tables:GetTableBucket", "s3tables:GetNamespace", "s3tables:ListNamespaces", "s3tables:CreateNamespace", "s3tables:GetTable", "s3tables:ListTables", "s3tables:CreateTable", "s3tables:GetTableMetadataLocation", "s3tables:UpdateTableMetadataLocation", "s3tables:GetTableData", "s3tables:PutTableData"], "Resource": [table_bucket, table]},
    ])
    resources["GlueJob"] = {"Type": "AWS::Glue::Job", "Properties": {"Name": GLUE_JOB_NAME, "Role": {"Fn::GetAtt": ["GlueExecutionRole", "Arn"]}, "Command": {"Name": "glueetl", "ScriptLocation": GLUE_SCRIPT_URI, "PythonVersion": "3"}, "GlueVersion": "5.0", "WorkerType": "G.1X", "NumberOfWorkers": 4, "Timeout": 60, "MaxRetries": 0, "ExecutionProperty": {"MaxConcurrentRuns": 5}, "DefaultArguments": {"--job-language": "python", "--datalake-formats": "iceberg", "--enable-metrics": "true", "--enable-continuous-cloudwatch-log": "true", "--conf": {"Fn::Sub": " ".join(["spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions", "--conf spark.sql.legacy.timeParserPolicy=CORRECTED", "--conf spark.sql.catalog.s3_rest_catalog=org.apache.iceberg.spark.SparkCatalog", "--conf spark.sql.catalog.s3_rest_catalog.type=rest", "--conf spark.sql.catalog.s3_rest_catalog.uri=https://s3tables.${AWS::Region}.amazonaws.com/iceberg", "--conf spark.sql.catalog.s3_rest_catalog.rest.sigv4-enabled=true", "--conf spark.sql.catalog.s3_rest_catalog.rest.signing-name=s3tables", "--conf spark.sql.catalog.s3_rest_catalog.rest.signing-region=${AWS::Region}", "--conf spark.sql.catalog.s3_rest_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO"])}}}}
    resources["ApiTaskDefinition"] = {"Type": "AWS::ECS::TaskDefinition", "Properties": {"Family": "s3-uploader-api", "RequiresCompatibilities": ["FARGATE"], "NetworkMode": "awsvpc", "Cpu": "1024", "Memory": "2048", "ExecutionRoleArn": {"Fn::GetAtt": ["ExecutionRole", "Arn"]}, "TaskRoleArn": {"Fn::GetAtt": ["ApiTaskRole", "Arn"]}, "ContainerDefinitions": [{"Name": "api", "Image": {"Ref": "ApiImageUri"}, "Essential": True, "PortMappings": [{"ContainerPort": 8090}], "Environment": _environment(("AWS_REGION", {"Ref": "AWS::Region"}), ("S3_UPLOADER_LANDING_BUCKET", {"Ref": "LandingBucketName"}), ("S3_UPLOADER_LANDING_PREFIX", LANDING_PREFIX), ("S3_UPLOADER_BASE_QUEUE_URL", {"Ref": "BaseWorkerQueue"}), ("S3_UPLOADER_LARGE_QUEUE_URL", {"Ref": "LargeWorkerQueue"}), ("S3_UPLOADER_MUTATION_QUEUE_URL", {"Ref": "MutationQueue"}), ("S3_UPLOADER_SESSION_TTL_SECONDS", "43200"), ("S3_UPLOADER_RAW_RETENTION_DAYS", "1"), ("S3_UPLOADER_COOKIE_SECURE", "true"), ("S3_UPLOADER_API_BASE_URL", f"https://{DOMAIN}"), ("S3_UPLOADER_GLUE_JOB_NAME", {"Ref": "GlueJob"}), ("S3_UPLOADER_CONTRACT_BUCKET", CONTRACT_BUCKET), ("S3_UPLOADER_CONTRACT_PREFIX", CONTRACT_PREFIX)), "Secrets": [{"Name": "S3_UPLOADER_LOGIN_PASSWORD", "ValueFrom": {"Ref": "LoginPasswordSecretArn"}}, {"Name": "S3_UPLOADER_LOGIN_SECRET", "ValueFrom": {"Ref": "LoginSigningSecretArn"}}], "LogConfiguration": _log("ApiLogGroup", "api")} ]}}
    resources["BaseWorkerTaskDefinition"] = _worker_definition(cpu="4096", memory="16384")
    resources["LargeWorkerTaskDefinition"] = _worker_definition(cpu="8192", memory="32768")
    resources["MutationDispatcherTaskDefinition"] = {"Type": "AWS::ECS::TaskDefinition", "Properties": {"Family": "s3-uploader-mutation-dispatcher", "RequiresCompatibilities": ["FARGATE"], "NetworkMode": "awsvpc", "Cpu": "512", "Memory": "1024", "ExecutionRoleArn": {"Fn::GetAtt": ["ExecutionRole", "Arn"]}, "TaskRoleArn": {"Fn::GetAtt": ["DispatcherTaskRole", "Arn"]}, "ContainerDefinitions": [{"Name": "dispatcher", "Image": {"Ref": "WorkerImageUri"}, "Essential": True, "Command": ["python3", "-m", "s3tables_uploader.mutation_dispatcher"], "Environment": _environment(("AWS_REGION", {"Ref": "AWS::Region"}), ("S3_UPLOADER_LANDING_BUCKET", {"Ref": "LandingBucketName"}), ("S3_UPLOADER_LANDING_PREFIX", LANDING_PREFIX), ("S3_UPLOADER_MUTATION_QUEUE_URL", {"Ref": "MutationQueue"}), ("S3_UPLOADER_GLUE_JOB_NAME", {"Ref": "GlueJob"}), ("S3_UPLOADER_MAX_CONCURRENT_GLUE", "5"), ("S3_UPLOADER_MAX_TRACKED_MUTATIONS", "50"), ("S3_UPLOADER_MUTATION_VISIBILITY_SECONDS", "120"), ("S3_UPLOADER_MUTATION_VISIBILITY_RENEWAL_SECONDS", "30"), ("S3_UPLOADER_MUTATION_POLL_SECONDS", "10")), "LogConfiguration": _log("WorkerLogGroup", "dispatcher")} ]}}
    resources["ApiTargetGroup"] = {"Type": "AWS::ElasticLoadBalancingV2::TargetGroup", "Properties": {"TargetType": "ip", "Port": 8090, "Protocol": "HTTP", "VpcId": {"Ref": "VpcId"}, "HealthCheckPath": "/healthz"}}
    resources["ApiService"] = {"Type": "AWS::ECS::Service", "Properties": {"Cluster": {"Ref": "ClusterArn"}, "DesiredCount": 1, "LaunchType": "FARGATE", "TaskDefinition": {"Ref": "ApiTaskDefinition"}, "NetworkConfiguration": _network(), "LoadBalancers": [{"ContainerName": "api", "ContainerPort": 8090, "TargetGroupArn": {"Ref": "ApiTargetGroup"}}]}}
    resources["MutationDispatcherService"] = {"Type": "AWS::ECS::Service", "Properties": {"Cluster": {"Ref": "ClusterArn"}, "DesiredCount": 1, "LaunchType": "FARGATE", "TaskDefinition": {"Ref": "MutationDispatcherTaskDefinition"}, "NetworkConfiguration": _network()}}
    resources["BaseWorkerPipe"] = _worker_pipe("BaseWorkerTaskDefinition", "BaseWorkerQueue")
    resources["LargeWorkerPipe"] = _worker_pipe("LargeWorkerTaskDefinition", "LargeWorkerQueue")
    for logical_id, queue, description in (("WorkerLaunchDlqAlarm", "WorkerLaunchDlq", "worker-launch messages reached the DLQ; inspect and redrive only after correction."), ("MutationDlqAlarm", "MutationDlq", "mutation messages reached the DLQ; inspect durable command and status before redrive.")):
        resources[logical_id] = {"Type": "AWS::CloudWatch::Alarm", "Properties": {"AlarmDescription": f"S3 uploader {description}", "Namespace": "AWS/SQS", "MetricName": "ApproximateNumberOfMessagesVisible", "Dimensions": [{"Name": "QueueName", "Value": {"Fn::GetAtt": [queue, "QueueName"]}}], "Statistic": "Maximum", "Period": 60, "EvaluationPeriods": 1, "Threshold": 0, "ComparisonOperator": "GreaterThanThreshold", "TreatMissingData": "notBreaching"}}
    resources["HostRule"] = {"Type": "AWS::ElasticLoadBalancingV2::ListenerRule", "Properties": {"ListenerArn": {"Ref": "AlbListenerArn"}, "Priority": {"Ref": "HostRulePriority"}, "Conditions": [{"Field": "host-header", "HostHeaderConfig": {"Values": [{"Ref": "HostRuleHostname"}]}}], "Actions": [{"Type": "forward", "TargetGroupArn": {"Ref": "ApiTargetGroup"}}]}}
    return {"AWSTemplateFormatVersion": "2010-09-09", "Description": "Production S3 uploader: API, leased workers, and one FIFO mutation dispatcher.", "Parameters": {"ClusterArn": {"Type": "String"}, "VpcId": {"Type": "AWS::EC2::VPC::Id"}, "PrivateSubnets": {"Type": "List<AWS::EC2::Subnet::Id>"}, "AlbListenerArn": {"Type": "String"}, "HostRulePriority": {"Type": "Number", "Default": 48999}, "HostRuleHostname": {"Type": "String", "Default": "s3-uploader-production.invalid"}, "ExistingAlbSecurityGroupId": {"Type": "AWS::EC2::SecurityGroup::Id"}, "LandingBucketName": {"Type": "String"}, "LandingBucketArn": {"Type": "String"}, "ApiImageUri": {"Type": "String"}, "WorkerImageUri": {"Type": "String"}, "LoginPasswordSecretArn": {"Type": "String"}, "LoginSigningSecretArn": {"Type": "String"}, "EncryptionSecretArn": {"Type": "String"}}, "Resources": resources, "Outputs": {"ApiTargetGroupArn": {"Value": {"Ref": "ApiTargetGroup"}}, "BaseQueueUrl": {"Value": {"Ref": "BaseWorkerQueue"}}, "LargeQueueUrl": {"Value": {"Ref": "LargeWorkerQueue"}}, "MutationQueueUrl": {"Value": {"Ref": "MutationQueue"}}, "GlueJobName": {"Value": {"Ref": "GlueJob"}}}}


def main() -> None:
    print(json.dumps(render_template(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
