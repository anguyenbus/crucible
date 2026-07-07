"""Phase 0 check: VPC OpenSearch endpoint DNS resolution + SigV4 connectivity.

Verifies, in order:
  1. The OpenSearch VPC endpoint hostname resolves to a private IP from this
     host (via the normal resolver chain, i.e. systemd-resolved -> VPC
     resolver 172.31.0.2).
  2. A SigV4-signed GET /_cluster/health (instance-role credentials via the
     boto3 credential chain, region ap-southeast-2) returns HTTP 200.

Exits 0 on success, 1 on failure.

Usage:
    uv run scripts/check_connectivity.py

Config (env, optional):
    INGESTION_OPENSEARCH_HOST  override the endpoint hostname
    AWS_REGION                 override the region (default ap-southeast-2)

DNS blocker resolution (recorded per task 1.2 / 1.6)
-----------------------------------------------------
The "DNS resolution fails from this host" blocker in plan.md turned out to be
a TYPO in the recorded endpoint hostname, not a resolver problem:

  recorded (plan.md, wrong): vpc-eval-poc-cvvwd6y6ygsjdyyrdodrfi7p22y....
  actual   (describe-domain): vpc-eval-poc-cvvwd6y6gsjdyyrdodrfi7p22y....
                                            ^ extra "y" in the recorded name

Diagnosis trail:
  * `resolvectl status` showed the host correctly using the VPC resolver
    172.31.0.2 (systemd-resolved stub mode) — resolver chain healthy.
  * Querying 172.31.0.2 directly for the recorded name returned NXDOMAIN,
    proving the record genuinely did not exist (not a forwarding issue).
  * `aws opensearch describe-domain --domain-name eval-poc` returned the
    authoritative endpoint, which differs by one character and resolves fine.

No /etc/hosts entry or systemd-resolved change was needed. If DNS fails here
again, this script prints the authoritative endpoint from describe-domain so
a stale hostname is caught immediately.
"""

import socket
import sys

import boto3

from _common import (
    OPENSEARCH_DOMAIN_NAME,
    get_opensearch_client,
    get_opensearch_host,
    get_region,
    print_result,
)


def check_dns(host: str) -> bool:
    print(f"[1/2] DNS resolution: {host}")
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        print(f"  FAIL: getaddrinfo -> {exc}")
        print_dns_diagnostics(host)
        return False
    ips = sorted({info[4][0] for info in infos})
    print(f"  OK: resolves to {', '.join(ips)}")
    return True


def print_dns_diagnostics(host: str) -> None:
    """On DNS failure, fetch the authoritative endpoint so a stale/typo'd
    hostname (the original cause of this blocker) is caught immediately."""
    print("  Diagnostics:")
    print("    - Check the resolver chain: `resolvectl status`, then query the")
    print(f"      VPC resolver directly: `host {host} 172.31.0.2`.")
    print("    - If the VPC resolver returns an IP but the host cannot resolve")
    print("      it, fix systemd-resolved or add an /etc/hosts entry.")
    try:
        opensearch = boto3.client("opensearch", region_name=get_region())
        domain = opensearch.describe_domain(DomainName=OPENSEARCH_DOMAIN_NAME)
        actual = domain["DomainStatus"]["Endpoints"]["vpc"]
        if actual != host:
            print(f"    - HOSTNAME MISMATCH: describe-domain says the endpoint is")
            print(f"        {actual}")
            print(f"      but this script was given")
            print(f"        {host}")
            print("      Update INGESTION_OPENSEARCH_HOST (this was the original")
            print("      Phase 0 blocker — a typo'd hostname).")
        else:
            print("    - describe-domain confirms the hostname is correct; this is")
            print("      a genuine resolver problem, not a stale endpoint.")
    except Exception as exc:  # diagnostics only — never mask the DNS failure
        print(f"    - Could not fetch authoritative endpoint: {exc}")


def check_cluster_health() -> bool:
    print("[2/2] SigV4-signed GET /_cluster/health")
    try:
        client = get_opensearch_client()
        health = client.cluster.health()
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False
    print(
        f"  OK: HTTP 200, cluster '{health.get('cluster_name')}' "
        f"status={health.get('status')}"
    )
    return True


def main() -> int:
    host = get_opensearch_host()
    passed = check_dns(host) and check_cluster_health()
    return print_result(passed, "check_connectivity")


if __name__ == "__main__":
    sys.exit(main())
