"""Bedrock runtime client (boto3 credential chain — instance role on the host)."""

from functools import lru_cache

import boto3

from app.config import get_settings


@lru_cache(maxsize=1)
def get_bedrock_client():
    return boto3.client("bedrock-runtime", region_name=get_settings().aws_region)
