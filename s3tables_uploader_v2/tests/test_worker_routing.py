import unittest

from s3tables_uploader_v2.worker_routing import MIB, RoutingError, SelectedFile, route_files


class WorkerRoutingTests(unittest.TestCase):
    def test_routes_each_supported_format_at_its_base_allowance(self):
        for name, size in [
            ("a.parquet", 128 * MIB), ("a.parquet.gzip", 128 * MIB), ("a.csv", 64 * MIB),
            ("a.tsv", 64 * MIB), ("a.xlsx", 32 * MIB), ("a.xls", 32 * MIB),
        ]:
            self.assertEqual(route_files([SelectedFile(name, size)]).worker_size, "BASE")

    def test_routes_above_boundary_or_combined_score_to_large(self):
        self.assertEqual(route_files([SelectedFile("a.parquet", 300 * MIB)]).worker_size, "LARGE")
        self.assertEqual(route_files([SelectedFile("a.parquet", 64 * MIB), SelectedFile("b.parquet", 64 * MIB)]).worker_size, "BASE")
        self.assertEqual(route_files([SelectedFile("a.parquet", 64 * MIB), SelectedFile("b.csv", 33 * MIB)]).worker_size, "LARGE")

    def test_rejects_unknown_or_empty_file_metadata(self):
        with self.assertRaises(RoutingError):
            route_files([SelectedFile("a.zip", 1)])
        with self.assertRaises(RoutingError):
            route_files([SelectedFile("a.parquet", 0)])
