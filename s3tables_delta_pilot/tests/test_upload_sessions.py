import io
import hashlib
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from s3tables_delta_pilot.upload_sessions import UploadSessionStore


class UploadSessionTests(unittest.TestCase):
    def test_private_session_copies_files_once_and_hides_local_path(self):
        with TemporaryDirectory() as directory:
            store = UploadSessionStore(Path(directory), ttl_minutes=60)
            session = store.create(
                owner_user_id="local-editor", mode="create", table_bucket_arn="arn:bucket/example",
                namespace="pilot", table="surgery", files=[("source.csv", io.BytesIO(b"key,value\n1,a\n"))],
            )
            source = session.files[0]
            self.assertTrue(Path(source.path).is_file())
            self.assertEqual("key,value\n1,a\n", Path(source.path).read_text())
            self.assertEqual(hashlib.sha256(b"key,value\n1,a\n").hexdigest(), source.sha256)
            safe = session.safe_dict()
            self.assertNotIn("path", safe["files"][0])
            store.delete(session.session_id, "local-editor")
            self.assertFalse(Path(source.path).exists())

    def test_session_is_owned_by_creator(self):
        with TemporaryDirectory() as directory:
            store = UploadSessionStore(Path(directory))
            session = store.create(
                owner_user_id="alice", mode="create", table_bucket_arn="arn:bucket/example",
                namespace="pilot", table="surgery", files=[("source.csv", io.BytesIO(b"key\n1\n"))],
            )
            with self.assertRaises(KeyError):
                store.get(session.session_id, "bob")


if __name__ == "__main__":
    unittest.main()
