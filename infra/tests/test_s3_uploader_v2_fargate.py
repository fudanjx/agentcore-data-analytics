import json
import unittest

from infra.s3_uploader_v2_fargate import DOMAIN, render_template


class FargateTemplateTests(unittest.TestCase):
    def test_template_has_isolated_api_worker_and_safe_dns_alias(self):
        template = render_template()
        resources = template["Resources"]
        self.assertEqual(resources["ApiTargetGroup"]["Properties"]["TargetType"], "ip")
        self.assertEqual(resources["BaseWorkerTaskDefinition"]["Properties"]["Memory"], "16384")
        self.assertEqual(resources["WorkerTaskDefinition"]["Properties"]["Memory"], "32768")
        self.assertIn("BaseWorkerPipe", resources)
        self.assertIn("LargeWorkerPipe", resources)
        self.assertIn("MutationQueue", resources)
        self.assertEqual(resources["MutationDispatcherService"]["Properties"]["DesiredCount"], 1)
        self.assertEqual(resources["MutationQueue"]["Properties"]["VisibilityTimeout"], 120)
        self.assertEqual(resources["MutationQueue"]["Properties"]["RedrivePolicy"]["maxReceiveCount"], 5)
        self.assertIn("MutationQueueDlqAlarm", resources)
        dispatcher_environment = resources["MutationDispatcherTaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Environment"]
        self.assertIn({"Name": "S3_UPLOADER_V3_MUTATION_QUEUE_URL", "Value": {"Ref": "MutationQueue"}}, dispatcher_environment)
        self.assertIn({"Name": "S3_UPLOADER_V2_GLUE_JOB_NAME", "Value": {"Ref": "V3GlueJob"}}, dispatcher_environment)
        self.assertIn({"Name": "S3_UPLOADER_V3_MUTATION_POLL_SECONDS", "Value": "10"}, dispatcher_environment)
        self.assertEqual(template["Parameters"]["EnableV3Leases"]["Default"], "false")
        self.assertEqual(resources["DnsRecord"]["Properties"]["Name"], DOMAIN + ".")
        self.assertEqual(resources["DnsRecord"]["Properties"]["AliasTarget"]["DNSName"], {"Ref": "AlbDnsName"})
        statements = resources["ApiTaskRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        s3tables_actions = {action for statement in statements for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"]) if action.startswith("s3tables:")}
        self.assertTrue({"s3tables:ListTableBuckets", "s3tables:CreateNamespace", "s3tables:ListTables", "s3tables:DeleteTable"}.issubset(s3tables_actions))
        self.assertIn("s3tables:CreateTableBucket", s3tables_actions)
        bucket_create = next(statement for statement in statements if statement["Action"] == "s3tables:CreateTableBucket")
        self.assertEqual(bucket_create["Resource"], "*")
        glue_actions = {action for statement in statements for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"]) if action.startswith("glue:")}
        self.assertEqual(glue_actions, {"glue:GetJobRun", "glue:StartJobRun"})
        landing_access = next(statement for statement in statements if {"s3:GetObject", "s3:PutObject"}.issubset(set(statement["Action"])))
        self.assertIn("s3:DeleteObject", landing_access["Action"])
        self.assertIn("s3:DeleteObjectVersion", landing_access["Action"])
        lifecycle = resources["LandingBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
        raw_lifecycle = next(rule for rule in lifecycle if rule["Id"] == "expire-raw")
        self.assertEqual(raw_lifecycle["NoncurrentVersionExpiration"], {"NoncurrentDays": 1})
        worker_statements = resources["WorkerTaskRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        worker_landing_access = next(statement for statement in worker_statements if "s3:GetObjectVersion" in statement["Action"])
        self.assertIn("s3:DeleteObject", worker_landing_access["Action"])
        dispatcher_lock_list = next(statement for statement in worker_statements if statement["Action"] == "s3:ListBucket" and statement["Condition"]["StringLike"]["s3:prefix"] == ["s3-uploader-v2/table-locks", "s3-uploader-v2/table-locks/*"])
        self.assertEqual(dispatcher_lock_list["Resource"], {"Fn::GetAtt": ["LandingBucket", "Arn"]})
        history_list = next(statement for statement in statements if statement["Action"] == "s3:ListBucket" and statement["Resource"] == "arn:aws:s3:::ah-data-analytics")
        self.assertEqual(history_list["Condition"]["StringLike"]["s3:prefix"], ["temp_s3_update/web_ingest/upload_history/*"])
        metadata_read = next(statement for statement in statements if statement["Action"] == "s3:GetObject" and statement["Resource"] == "arn:aws:s3:::*--table-s3/metadata/*")
        self.assertEqual(metadata_read["Resource"], "arn:aws:s3:::*--table-s3/metadata/*")
        table_data_read = next(statement for statement in statements if "s3tables:GetTableData" in statement["Action"])
        self.assertTrue({"s3tables:GetTable", "s3tables:DeleteTable", "s3tables:GetTableData", "s3tables:GetTableMetadataLocation"}.issubset(table_data_read["Action"]))
        self.assertEqual(table_data_read["Resource"], [{"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/*/table/*"}])
        table_control = next(statement for statement in statements if "s3tables:ListNamespaces" in statement["Action"])
        self.assertEqual(table_control["Resource"], [{"Fn::Sub": "arn:${AWS::Partition}:s3tables:${AWS::Region}:${AWS::AccountId}:bucket/*"}])
        contract_write = next(statement for statement in statements if statement["Action"] == "s3:PutObject" and "table_contracts/*" in statement["Resource"])
        self.assertEqual(contract_write["Resource"], "arn:aws:s3:::ah-data-analytics/temp_s3_update/web_ingest/table_contracts/*")
        self.assertEqual(resources["V3GlueJob"]["Properties"]["Name"], "s3-uploader-v3-ingest")
        api_environment = resources["ApiTaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Environment"]
        self.assertIn({"Name": "S3_UPLOADER_V2_GLUE_JOB_NAME", "Value": {"Ref": "V3GlueJob"}}, api_environment)
        self.assertIn({"Name": "S3_UPLOADER_V3_MUTATION_QUEUE_URL", "Value": {"Ref": "MutationQueue"}}, api_environment)
        json.dumps(template)
