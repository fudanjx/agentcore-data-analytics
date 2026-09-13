import json
import unittest

from infra.s3_uploader_fargate import GLUE_JOB_NAME, LANDING_PREFIX, SKILL_BUNDLE_BUCKET, SKILL_BUNDLE_PREFIX, render_template


class FargateTemplateTests(unittest.TestCase):
    def test_production_stack_uses_neutral_resources_and_blue_green_host_rule(self):
        template = render_template()
        resources = template["Resources"]
        self.assertNotIn("LandingBucket", resources)
        self.assertNotIn("JobQueue", resources)
        self.assertNotIn("WorkerPipe", resources)
        self.assertEqual(resources["BaseWorkerQueue"]["Properties"]["QueueName"], "s3-uploader-base.fifo")
        self.assertEqual(resources["LargeWorkerQueue"]["Properties"]["QueueName"], "s3-uploader-large.fifo")
        self.assertEqual(resources["MutationQueue"]["Properties"]["QueueName"], "s3-uploader-mutations.fifo")
        self.assertEqual(resources["BaseWorkerQueue"]["Properties"]["RedrivePolicy"]["deadLetterTargetArn"], {"Fn::GetAtt": ["WorkerLaunchDlq", "Arn"]})
        self.assertEqual(resources["MutationQueue"]["Properties"]["RedrivePolicy"]["deadLetterTargetArn"], {"Fn::GetAtt": ["MutationDlq", "Arn"]})
        self.assertIn("WorkerLaunchDlqAlarm", resources)
        self.assertIn("MutationDlqAlarm", resources)
        self.assertEqual(resources["BaseWorkerTaskDefinition"]["Properties"]["Memory"], "16384")
        self.assertEqual(resources["LargeWorkerTaskDefinition"]["Properties"]["Memory"], "32768")
        self.assertEqual(resources["ApiTaskDefinition"]["Properties"]["Family"], "s3-uploader-api")
        self.assertEqual(resources["MutationDispatcherTaskDefinition"]["Properties"]["Family"], "s3-uploader-mutation-dispatcher")
        self.assertEqual(resources["MutationDispatcherService"]["Properties"]["DesiredCount"], 1)
        self.assertIn("BaseWorkerPipe", resources)
        self.assertIn("LargeWorkerPipe", resources)
        self.assertEqual(template["Parameters"]["HostRuleHostname"]["Default"], "s3-uploader-production.invalid")
        self.assertNotIn("Condition", resources["HostRule"])
        self.assertEqual(resources["HostRule"]["Properties"]["Conditions"][0]["HostHeaderConfig"]["Values"], [{"Ref": "HostRuleHostname"}])
        self.assertEqual(resources["GlueJob"]["Properties"]["Name"], GLUE_JOB_NAME)
        self.assertEqual(resources["GlueJob"]["Properties"]["Role"], {"Fn::GetAtt": ["GlueExecutionRole", "Arn"]})
        self.assertNotIn("warehouse=", resources["GlueJob"]["Properties"]["DefaultArguments"]["--conf"]["Fn::Sub"])
        self.assertNotIn("EnableV3Leases", template["Parameters"])
        self.assertNotIn("EnableHostRouting", template["Parameters"])
        json.dumps(template)

    def test_task_roles_receive_only_required_neutral_configuration(self):
        resources = render_template()["Resources"]
        api_environment = resources["ApiTaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Environment"]
        self.assertIn({"Name": "S3_UPLOADER_LANDING_PREFIX", "Value": LANDING_PREFIX}, api_environment)
        self.assertIn({"Name": "S3_UPLOADER_MUTATION_QUEUE_URL", "Value": {"Ref": "MutationQueue"}}, api_environment)
        self.assertIn({"Name": "S3_UPLOADER_SKILL_BUNDLE_BUCKET", "Value": SKILL_BUNDLE_BUCKET}, api_environment)
        self.assertIn({"Name": "S3_UPLOADER_SKILL_BUNDLE_PREFIX", "Value": SKILL_BUNDLE_PREFIX}, api_environment)
        dispatcher_environment = resources["MutationDispatcherTaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Environment"]
        self.assertIn({"Name": "S3_UPLOADER_GLUE_JOB_NAME", "Value": {"Ref": "GlueJob"}}, dispatcher_environment)
        statements = resources["ApiTaskRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        actions = {action for statement in statements for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])}
        self.assertIn("s3tables:CreateTableBucket", actions)
        self.assertIn("s3tables:DeleteTable", actions)
        self.assertIn("glue:StartJobRun", actions)
        self.assertNotIn("s3-uploader-v3", json.dumps(resources))

        skill_object_statement = next(
            statement for statement in statements
            if statement.get("Resource") == f"arn:aws:s3:::{SKILL_BUNDLE_BUCKET}/{SKILL_BUNDLE_PREFIX}/*"
        )
        self.assertEqual(set(skill_object_statement["Action"]), {"s3:GetObject", "s3:PutObject", "s3:DeleteObject"})

    def test_historical_landing_data_is_read_only(self):
        resources = render_template()["Resources"]
        for role_name in ("ApiTaskRole", "WorkerTaskRole", "DispatcherTaskRole"):
            statements = resources[role_name]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
            for statement in statements:
                resources_json = json.dumps(statement.get("Resource", ""))
                actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
                if "s3-uploader-v2" in resources_json:
                    self.assertTrue(set(actions).issubset({"s3:GetObject", "s3:GetObjectVersion"}))


if __name__ == "__main__":
    unittest.main()
