import json
import unittest

from infra.s3_uploader_v2_fargate import DOMAIN, render_template


class FargateTemplateTests(unittest.TestCase):
    def test_template_has_isolated_api_worker_and_safe_dns_alias(self):
        template = render_template()
        resources = template["Resources"]
        self.assertEqual(resources["ApiTargetGroup"]["Properties"]["TargetType"], "ip")
        self.assertEqual(resources["WorkerTaskDefinition"]["Properties"]["Memory"], "16384")
        self.assertIn("WorkerPipe", resources)
        self.assertEqual(resources["DnsRecord"]["Properties"]["Name"], DOMAIN + ".")
        self.assertEqual(resources["DnsRecord"]["Properties"]["AliasTarget"]["DNSName"], {"Ref": "AlbDnsName"})
        json.dumps(template)
