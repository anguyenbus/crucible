import boto3
import pytest


def _aws_credentials_available() -> bool:
    try:
        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Skip `requires_aws` tests cleanly when no AWS credentials are present."""
    if _aws_credentials_available():
        return
    skip_aws = pytest.mark.skip(reason="AWS credentials not available")
    for item in items:
        if "requires_aws" in item.keywords:
            item.add_marker(skip_aws)
