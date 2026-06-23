"""
Pure, infra-free evaluation kernel.

``app.kernel`` holds the import-pure core (comparison stats, schema
validation, RAG/judge interfaces, DeepEval metric specs + evaluator) that copies
verbatim into the monorepo ``app/kernel/``. The kernel may import only stdlib,
``pydantic``, ``jsonschema``, ``scipy``, ``deepeval`` and ``beartype``. It must
NEVER import ``boto3``, ``phoenix``/``arize``, ``dotenv``, the migrating eval
service, or the non-migrating demo/CLI layer, nor read/write ``os.environ``
(except the single allowlisted DeepEval telemetry opt-out in the rag_metrics
subpackage).
Purity is enforced by the import-linter ``kernel-pure`` contract plus the kernel
grep-gate pre-commit hook.
"""
