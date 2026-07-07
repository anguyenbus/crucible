"""Shared helpers for the Phase 0 verification scripts.

Builds a SigV4-signed opensearch-py client using the boto3 credential chain
(instance role on the POC host — no static keys required).
"""

import os

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

# Authoritative VPC endpoint for the eval-poc domain, confirmed via
# `aws opensearch describe-domain --domain-name eval-poc`.
# NOTE: plan.md originally recorded this hostname with a typo (an extra "y":
# "...cvvwd6y6ygsjdyyrdodrfi7p22y..."), which is why DNS resolution appeared
# to be broken from this host. See check_connectivity.py for the full story.
DEFAULT_OPENSEARCH_HOST = (
    "vpc-eval-poc-cvvwd6y6gsjdyyrdodrfi7p22y.ap-southeast-2.es.amazonaws.com"
)
DEFAULT_REGION = "ap-southeast-2"
OPENSEARCH_DOMAIN_NAME = "eval-poc"


def get_region() -> str:
    return os.environ.get("AWS_REGION", DEFAULT_REGION)


def get_opensearch_host() -> str:
    return os.environ.get("INGESTION_OPENSEARCH_HOST", DEFAULT_OPENSEARCH_HOST)


def get_aws_credentials():
    credentials = boto3.Session(region_name=get_region()).get_credentials()
    if credentials is None:
        raise RuntimeError(
            "No AWS credentials found in the boto3 credential chain "
            "(expected the EC2 instance role on this host)."
        )
    return credentials


def get_opensearch_client(timeout: int = 30) -> OpenSearch:
    """SigV4-signed OpenSearch client for the VPC eval-poc domain."""
    region = get_region()
    auth = AWS4Auth(
        region=region,
        service="es",
        refreshable_credentials=get_aws_credentials(),
    )
    return OpenSearch(
        hosts=[{"host": get_opensearch_host(), "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=timeout,
    )


def print_result(passed: bool, script_name: str) -> int:
    """Print the final PASS/FAIL summary line and return the exit code."""
    if passed:
        print(f"\nRESULT: PASS — {script_name}")
        return 0
    print(f"\nRESULT: FAIL — {script_name}")
    return 1
