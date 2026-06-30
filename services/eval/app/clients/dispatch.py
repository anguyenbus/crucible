"""Worker dispatch — the seam between control plane and data plane (§11 Option A).

This is the ONLY place the control plane references the worker, and it does so as
an image+entrypoint, never by importing app.worker / app.runners / app.deepeval.
The kubernetes client is imported LAZILY so the API pod (and the import-smoke)
do not require it.

Set EVAL_DISPATCH=noop to make this a no-op (used by tests / local boot without a
cluster). Default is k8s_job.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def _job_name(run_id: str) -> str:
    # DNS-1123-safe, deterministic ⇒ re-dispatch is idempotent (409 AlreadyExists).
    return f"eval-{run_id.replace('_', '-').lower()}"[:63]


def launch_run(run_id: str) -> None:
    """Launch a one-shot K8s Job running `python -m app.worker --run-id <id>`.

    Non-blocking: returns once the Job object is accepted. Does NOT score.
    """
    mode = os.environ.get("EVAL_DISPATCH", "k8s_job")
    if mode == "noop":
        log.info("EVAL_DISPATCH=noop: skipping real dispatch for %s", run_id)
        return

    namespace = os.environ.get("EVAL_NAMESPACE", "eval-system")
    image = os.environ["EVAL_WORKER_IMAGE"]
    worker_sa = os.environ.get("EVAL_WORKER_SA", "sa-eval-worker")

    from kubernetes import client, config  # lazy: not needed by the API plane

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=_job_name(run_id),
            namespace=namespace,
            labels={"app": "eval-worker", "run-id": run_id},
        ),
        spec=client.V1JobSpec(
            backoff_limit=2,
            ttl_seconds_after_finished=3600,
            active_deadline_seconds=3600,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app": "eval-worker", "run-id": run_id}),
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    service_account_name=worker_sa,  # worker IAM, not the API's
                    containers=[
                        client.V1Container(
                            name="worker",
                            image=image,
                            command=["python", "-m", "app.worker", "--run-id", run_id],
                        )
                    ],
                ),
            ),
        ),
    )

    try:
        client.BatchV1Api().create_namespaced_job(namespace=namespace, body=job)
    except client.ApiException as e:
        if e.status == 409:
            return  # already dispatched — idempotent no-op
        raise
