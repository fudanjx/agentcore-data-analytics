"""Publish insights.bot-alex.com through the existing ALB and Route 53 zone."""

import argparse

import boto3
import botocore.exceptions


REGION = "ap-southeast-1"
INSTANCE_ID = "i-06f7b81355b8c5346"
VPC_ID = "vpc-0ead319ed7ea44443"
INSTANCE_SECURITY_GROUP = "sg-038dd1c37fb91b05b"
ALB_SECURITY_GROUP = "sg-05e9b2c2291a12ffc"
ALB_NAME = "openwebui-alb"
TARGET_GROUP_NAME = "openwebui-insights"
TARGET_PORT = 3001
HOSTNAME = "insights.bot-alex.com"
HOSTED_ZONE_ID = "Z069491219YFUFHMLLV7E"
RULE_PRIORITY = 10


ec2 = boto3.client("ec2", region_name=REGION)
elbv2 = boto3.client("elbv2", region_name=REGION)
route53 = boto3.client("route53")


def load_balancer() -> dict:
    return elbv2.describe_load_balancers(Names=[ALB_NAME])["LoadBalancers"][0]


def target_group_arn() -> str:
    groups = elbv2.describe_target_groups()["TargetGroups"]
    existing = next(
        (group for group in groups if group["TargetGroupName"] == TARGET_GROUP_NAME),
        None,
    )
    if existing:
        return existing["TargetGroupArn"]
    return elbv2.create_target_group(
        Name=TARGET_GROUP_NAME,
        Protocol="HTTP",
        Port=TARGET_PORT,
        VpcId=VPC_ID,
        TargetType="instance",
        HealthCheckEnabled=True,
        HealthCheckProtocol="HTTP",
        HealthCheckPath="/health",
        HealthCheckPort="traffic-port",
        HealthCheckIntervalSeconds=15,
        HealthCheckTimeoutSeconds=5,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=3,
        Matcher={"HttpCode": "200"},
    )["TargetGroups"][0]["TargetGroupArn"]


def ensure_host_rule(target_group: str) -> None:
    alb = load_balancer()
    listeners = elbv2.describe_listeners(
        LoadBalancerArn=alb["LoadBalancerArn"]
    )["Listeners"]
    https_listener = next(item for item in listeners if item["Port"] == 443)
    rules = elbv2.describe_rules(
        ListenerArn=https_listener["ListenerArn"]
    )["Rules"]
    host_rule = next(
        (
            rule
            for rule in rules
            if any(
                HOSTNAME in condition.get("Values", [])
                for condition in rule.get("Conditions", [])
            )
        ),
        None,
    )
    conditions = [
        {
            "Field": "host-header",
            "HostHeaderConfig": {"Values": [HOSTNAME]},
        }
    ]
    actions = [{"Type": "forward", "TargetGroupArn": target_group}]
    if host_rule:
        elbv2.modify_rule(
            RuleArn=host_rule["RuleArn"],
            Conditions=conditions,
            Actions=actions,
        )
    else:
        elbv2.create_rule(
            ListenerArn=https_listener["ListenerArn"],
            Priority=RULE_PRIORITY,
            Conditions=conditions,
            Actions=actions,
        )


def prepare() -> str:
    try:
        ec2.authorize_security_group_ingress(
            GroupId=INSTANCE_SECURITY_GROUP,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": TARGET_PORT,
                    "ToPort": TARGET_PORT,
                    "UserIdGroupPairs": [
                        {
                            "GroupId": ALB_SECURITY_GROUP,
                            "Description": "ALB to OpenWebUI Insights",
                        }
                    ],
                }
            ],
        )
    except botocore.exceptions.ClientError as error:
        if error.response.get("Error", {}).get("Code") != "InvalidPermission.Duplicate":
            raise

    arn = target_group_arn()
    elbv2.register_targets(
        TargetGroupArn=arn,
        Targets=[{"Id": INSTANCE_ID, "Port": TARGET_PORT}],
    )
    elbv2.modify_target_group_attributes(
        TargetGroupArn=arn,
        Attributes=[
            {"Key": "deregistration_delay.timeout_seconds", "Value": "30"}
        ],
    )
    # A target group reports Target.NotInUse until a listener references it.
    # Attach the host-only rule before DNS exists so ALB health checks can run.
    ensure_host_rule(arn)
    print(f"target_group={arn}")
    return arn


def publish() -> None:
    arn = prepare()
    health = elbv2.describe_target_health(TargetGroupArn=arn)[
        "TargetHealthDescriptions"
    ]
    if not health or health[0]["TargetHealth"]["State"] != "healthy":
        state = health[0]["TargetHealth"]["State"] if health else "missing"
        raise RuntimeError(f"Insights target is not healthy: {state}")

    alb = load_balancer()
    route53.change_resource_record_sets(
        HostedZoneId=HOSTED_ZONE_ID,
        ChangeBatch={
            "Comment": "OpenWebUI v0.10.2 AgentCore Insights",
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": HOSTNAME,
                        "Type": "A",
                        "AliasTarget": {
                            "HostedZoneId": alb["CanonicalHostedZoneId"],
                            "DNSName": alb["DNSName"],
                            "EvaluateTargetHealth": True,
                        },
                    },
                }
            ],
        },
    )
    print(f"url=https://{HOSTNAME}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["prepare", "publish", "status"])
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare()
    elif args.phase == "publish":
        publish()
    else:
        arn = target_group_arn()
        health = elbv2.describe_target_health(TargetGroupArn=arn)[
            "TargetHealthDescriptions"
        ]
        print(health)


if __name__ == "__main__":
    main()
