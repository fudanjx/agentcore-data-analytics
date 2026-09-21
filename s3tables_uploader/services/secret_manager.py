"""AWS Secrets Manager implementation of :class:`SecretSource`."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from ..core.exceptions import ConfigurationError


class SecretsManagerSource:
    """Fetch secret string values from AWS Secrets Manager."""

    def __init__(self, secrets_manager_client: Any):
        self._client = secrets_manager_client

    def get_secret(self, secret_arn: str) -> str:
        try:
            response = self._client.get_secret_value(SecretId=secret_arn)
        except ClientError as error:
            raise ConfigurationError(
                f"Unable to fetch secret {secret_arn!r}: {error}"
            ) from error
        value = response.get("SecretString")
        if not isinstance(value, str) or not value:
            raise ConfigurationError(
                f"Secret {secret_arn!r} is empty or not a string"
            )
        return value


class InMemorySecretSource:
    """Test double that returns pre-registered values by ARN."""

    def __init__(self, secrets: dict[str, str] | None = None):
        self._secrets = dict(secrets or {})

    def set_secret(self, secret_arn: str, value: str) -> None:
        self._secrets[secret_arn] = value

    def get_secret(self, secret_arn: str) -> str:
        if secret_arn not in self._secrets:
            raise ConfigurationError(f"Unknown secret {secret_arn!r}")
        return self._secrets[secret_arn]
