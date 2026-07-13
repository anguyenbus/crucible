# Orchestrator RAG demo

Chat with the LIVE RAG pipeline (`POST /query/stream` over SSE).

- Tokens stream in real time — no simulated typing.
- Citations, sources, timings, and provenance render only from the final
  validated envelope.
- Each turn costs one paid Bedrock generation plus one Titan query embedding.
- Conversation memory is per-session only.

See `README.md` for setup and the honesty rules this UI is bound by.
