"""API-facing skill-bundle operations.

Thin wrapper around :mod:`skill_bundle` that converts raw
``SkillBundleError`` into the typed exception hierarchy exposed through
``core.exceptions`` and threads the Settings-driven skill-bundle destination
(bucket + prefix) into every call so hardened envs never fall back to the
DEV defaults.
"""

from __future__ import annotations

from .. import skill_bundle
from ..config import Settings
from ..core.exceptions import UploaderError


def _wrap(error: skill_bundle.SkillBundleError) -> UploaderError:
    wrapped = UploaderError(str(error), error_code="SKILL_BUNDLE_ERROR")
    wrapped.status_code = error.status_code
    return wrapped


class SkillService:
    """List, publish and remove skill files/versions for a table bucket."""

    def __init__(self, settings: Settings):
        self._settings = settings

    @property
    def _dest(self) -> dict[str, str]:
        return {
            "destination_bucket": self._settings.skill_bundle_bucket,
            "destination_prefix": self._settings.skill_bundle_prefix,
        }

    def list_files(self, table_bucket_arn: str) -> dict[str, object]:
        try:
            return skill_bundle.list_skill_files(table_bucket_arn, **self._dest)
        except skill_bundle.SkillBundleError as error:
            raise _wrap(error) from error

    def list_versions(self, table_bucket_arn: str) -> dict[str, object]:
        try:
            return skill_bundle.list_skill_versions(table_bucket_arn, **self._dest)
        except skill_bundle.SkillBundleError as error:
            raise _wrap(error) from error

    def publish_version(
        self,
        table_bucket_arn: str,
        user_id: str,
        filename: str,
        content: bytes,
    ) -> dict[str, object]:
        try:
            return skill_bundle.publish_version(
                table_bucket_arn, user_id, filename, content, **self._dest
            )
        except skill_bundle.SkillBundleError as error:
            raise _wrap(error) from error

    def publish_files(
        self,
        table_bucket_arn: str,
        user_id: str,
        payload: list[tuple[str, bytes]],
    ) -> dict[str, object]:
        try:
            return skill_bundle.publish_files(
                table_bucket_arn, user_id, payload, **self._dest
            )
        except skill_bundle.SkillBundleError as error:
            raise _wrap(error) from error

    @staticmethod
    def parse_paths_json(paths_json: str) -> list[str]:
        try:
            return skill_bundle.parse_paths_json(paths_json)
        except skill_bundle.SkillBundleError as error:
            raise _wrap(error) from error

    def version_location(self, table_bucket_arn: str, filename: str) -> tuple[str, str]:
        try:
            return skill_bundle.version_location(
                table_bucket_arn, filename, **self._dest
            )
        except skill_bundle.SkillBundleError as error:
            raise _wrap(error) from error

    def file_location(
        self, table_bucket_arn: str, path: str
    ) -> tuple[str, str, str]:
        try:
            return skill_bundle.skill_file_location(
                table_bucket_arn, path, **self._dest
            )
        except skill_bundle.SkillBundleError as error:
            raise _wrap(error) from error

    @property
    def s3(self):  # noqa: D401 - passthrough to preserve legacy access pattern
        """Expose the underlying skill-bundle S3 client used for downloads."""
        return skill_bundle.s3

    @property
    def max_file_bytes(self) -> int:
        return skill_bundle.MAX_FILE_BYTES

    @property
    def max_zip_bytes(self) -> int:
        return skill_bundle.MAX_ZIP_BYTES
