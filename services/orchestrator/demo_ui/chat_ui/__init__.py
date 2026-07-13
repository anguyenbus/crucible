"""
Browser-free UI logic for the Chainlit demo (plain importable modules).

Everything that can be unit-tested without a browser lives here — the SSE
line parser (``sse``), history windowing (``history``), envelope→render-model
mapping (``render``), the OTel tracer bootstrap (``tracing``), the HTTP
client glue (``client``), and the env surface (``config``). The Chainlit
callback file (``app.py``) stays a thin shell over these modules.

Dependency firewall: this package NEVER imports the orchestrator's ``app.*``
— the orchestrator is reached only over HTTP via ``ORCHESTRATOR_URL``.
"""
