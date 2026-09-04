import json
import logging
import unittest

from s3tables_delta_pilot.observability import JsonFormatter, request_id_var, user_id_var


class ObservabilityTests(unittest.TestCase):
    def test_json_formatter_keeps_safe_correlation_fields(self):
        request = request_id_var.set("request-123")
        user = user_id_var.set("local-editor")
        try:
            record = logging.LogRecord("test", logging.INFO, __file__, 1, "finished", (), None)
            record.phase = "PROFILING"
            payload = json.loads(JsonFormatter().format(record))
        finally:
            request_id_var.reset(request)
            user_id_var.reset(user)
        self.assertEqual("request-123", payload["request_id"])
        self.assertEqual("local-editor", payload["user_id"])
        self.assertEqual("PROFILING", payload["phase"])
