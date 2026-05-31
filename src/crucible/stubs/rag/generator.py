"""
LLM-powered answer generation for RAG.

NOTE: This is a reference stub implementation provided for demonstration purposes.
It is not intended for production use. This module provides the LLMGenerator class
which supports both OpenAI and AWS Bedrock (Anthropic, Amazon, Meta) models.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Final

# Load .env file if it exists
_env_path = Path.cwd() / ".env"
if _env_path.exists():
    from dotenv import load_dotenv

    load_dotenv(_env_path)

# Lazy tracer import to avoid circular dependency
_tracer = None


def _get_tracer():
    """Get global tracer instance for span emission."""
    global _tracer
    if _tracer is None:
        try:
            from opentelemetry.trace import get_tracer

            # Use globally registered tracer via set_global_tracer_provider=True
            _tracer = get_tracer(__name__)
        except (ImportError, Exception):
            pass  # Tracing not available
    return _tracer


def _is_bedrock_model(model: str) -> bool:
    """Check if model identifier is for AWS Bedrock."""
    return model.startswith(("anthropic.", "amazon.", "meta.", "mistral.", "cohere."))


class LLMGenerator:
    """
    LLM-powered answer generator for RAG.

    NOTE: This is a reference stub implementation for demonstration purposes.
    It is not intended for production use.

    Supports:
    - OpenAI: gpt-4o-mini, gpt-4o-mini, gpt-4-turbo, etc.
    - AWS Bedrock: anthropic.claude-3-5-sonnet-20241022-v2:0,
      amazon.titan-text-express-v1, meta.llama3-1-70b-instruct, etc.

    Attributes:
        _model: Model identifier.
        _provider: "openai" or "bedrock".
        _api_key: API key (for OpenAI).
        _deterministic_mode: Whether to use deterministic generation (temp=0).

    Example:
        >>> generator = LLMGenerator()  # Uses RAG_GENERATOR_MODEL env var
        >>> answer = generator.generate(
        ...     question="What is this?",
        ...     retrieved_chunks=[...]
        ... )
        >>> print(answer["text"])

    """

    __slots__ = ("_model", "_provider", "_api_key", "_deterministic_mode")

    MODEL_NAME_ATTR: Final[str] = "llm.model_name"
    INPUT_MESSAGE_ATTR: Final[str] = "llm.input_messages.{i}.message"
    OUTPUT_MESSAGE_ATTR: Final[str] = "llm.output_messages.{i}.message"
    TOKEN_COUNT_ATTR: Final[str] = "llm.token_count.total"
    MESSAGE_ROLE: Final[str] = "role"
    MESSAGE_CONTENT: Final[str] = "content"

    def __init__(
        self, model: str | None = None, deterministic_mode: bool = False
    ) -> None:
        """
        Initialize LLM generator.

        Args:
            model: Model identifier. If None, uses RAG_GENERATOR_MODEL env var
                or defaults to gpt-4o-mini. Bedrock models start with "anthropic.",
                "amazon.", "meta.", etc.
            deterministic_mode: If True, use temperature=0 for reproducible output.

        Raises:
            ValueError: If required credentials are missing.

        """
        if model is None:
            model = os.getenv("RAG_GENERATOR_MODEL", "gpt-4o-mini")
        self._model: str = model
        self._provider: str = "bedrock" if _is_bedrock_model(model) else "openai"
        self._deterministic_mode: bool = deterministic_mode

        if self._provider == "openai":
            self._api_key = os.getenv("OPENAI_API_KEY")
            if not self._api_key:
                raise ValueError(
                    "OPENAI_API_KEY environment variable required for OpenAI models"
                )
        else:
            # Bedrock uses AWS credentials from environment/aws config
            self._api_key = None

    def generate(
        self, question: str, retrieved_chunks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        Generate answer using LLM with retrieved context.

        Args:
            question: User question to answer.
            retrieved_chunks: List of retrieved chunks with chunk_id, text, etc.

        Returns:
            Dictionary containing:
                - text: Generated answer text
                - answer_supported: Whether corpus supports the answer
                - citations: List of citation dictionaries
                - timings_ms: Timing information

        Raises:
            ValueError: If API call fails or returns invalid response.

        """
        tracer = _get_tracer()

        # Check if tracer is usable (not NoOpTracer)
        # NoOpTracer doesn't support OpenInference span kinds
        is_noop_tracer = (
            tracer is not None
            and hasattr(tracer, "real_tracer")
            and tracer.real_tracer is None
        )

        if tracer is None or is_noop_tracer:
            if self._provider == "openai":
                return self._generate_openai(question, retrieved_chunks)
            else:
                return self._generate_bedrock(question, retrieved_chunks)

        from openinference.semconv.trace import OpenInferenceSpanKindValues

        LLM = OpenInferenceSpanKindValues.LLM

        # Try using tracer, fall back if it fails
        try:
            span_name = (
                "bedrock.generate" if self._provider == "bedrock" else "openai.generate"
            )
            span_context = tracer.start_as_current_span(
                span_name,
                openinference_span_kind=LLM,
            )
            span = span_context.__enter__()

            # Set model name
            span.set_attribute(self.MODEL_NAME_ATTR, self._model)

            # Set input message
            span.set_attribute(f"{self.INPUT_MESSAGE_ATTR}.{self.MESSAGE_ROLE}", "user")
            # Build context for prompt
            context_parts = []
            for chunk in retrieved_chunks:
                chunk_id = chunk.get("chunk_id", "unknown")
                text = chunk.get("text", "")
                context_parts.append(f"[{chunk_id}]: {text}")
            context = "\n\n".join(context_parts)
            user_message = f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer:"
            span.set_attribute(
                f"{self.INPUT_MESSAGE_ATTR}.{self.MESSAGE_CONTENT}", user_message
            )

            # Generate answer
            if self._provider == "openai":
                result = self._generate_openai(question, retrieved_chunks)
            else:
                result = self._generate_bedrock(question, retrieved_chunks)

            # Set output message attributes
            span.set_attribute(
                f"{self.OUTPUT_MESSAGE_ATTR}.{self.MESSAGE_ROLE}", "assistant"
            )
            span.set_attribute(
                f"{self.OUTPUT_MESSAGE_ATTR}.{self.MESSAGE_CONTENT}",
                result.get("text", ""),
            )

            # Set token count (0 since we don't track it)
            span.set_attribute(self.TOKEN_COUNT_ATTR, 0)

            span_context.__exit__(None, None, None)
            return result

        except TypeError:
            # Tracer doesn't support OpenInference params (NoOpTracer)
            # Fall back to direct generation
            if self._provider == "openai":
                return self._generate_openai(question, retrieved_chunks)
            else:
                return self._generate_bedrock(question, retrieved_chunks)

    def _generate_openai(
        self, question: str, retrieved_chunks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Generate using OpenAI API."""
        start_time = time.perf_counter()

        context_parts = []
        for chunk in retrieved_chunks:
            chunk_id = chunk.get("chunk_id", "unknown")
            text = chunk.get("text", "")
            context_parts.append(f"[{chunk_id}]: {text}")

        context = "\n\n".join(context_parts)

        system_prompt = (
            "You are a helpful assistant that answers questions based on "
            "the provided context.\n"
            "When answering, you MUST cite your sources using the chunk_ids "
            "in square brackets like [chunk_id].\n"
            'For example: "The answer is [doc1_chunk_00000]."\n\n'
            "If the context doesn't contain enough information to answer "
            "the question confidently, say \"I don't have enough information "
            'to answer this question."\n'
        )

        user_message = f"""Context:
{context}

Question: {question}

Answer:"""

        try:
            from openai import OpenAI

            client = OpenAI(api_key=self._api_key)

            # Use temperature=0 if in deterministic mode
            temperature = 0.0 if self._deterministic_mode else 0.0

            response = client.chat.completions.create(
                model=self._model,
                max_tokens=1024,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            )

            answer_text = response.choices[0].message.content or ""
            answer_supported = "I don't have enough information" not in answer_text
            generation_time = (time.perf_counter() - start_time) * 1000

            return {
                "text": answer_text,
                "answer_supported": answer_supported,
                "citations": [],
                "timings_ms": {"generation": generation_time},
            }

        except Exception as e:
            raise ValueError(f"OpenAI API call failed: {e}") from e

    def _generate_bedrock(
        self, question: str, retrieved_chunks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Generate using AWS Bedrock API."""
        start_time = time.perf_counter()

        context_parts = []
        for chunk in retrieved_chunks:
            chunk_id = chunk.get("chunk_id", "unknown")
            text = chunk.get("text", "")
            context_parts.append(f"[{chunk_id}]: {text}")

        context = "\n\n".join(context_parts)

        system_prompt = (
            "You are a helpful assistant that answers questions based on "
            "the provided context.\n"
            "When answering, you MUST cite your sources using the chunk_ids "
            "in square brackets like [chunk_id].\n"
            'For example: "The answer is [doc1_chunk_00000]."\n\n'
            "If the context doesn't contain enough information to answer "
            "the question confidently, say \"I don't have enough information "
            'to answer this question."\n'
        )

        user_message = f"""Context:
{context}

Question: {question}

Answer:"""

        try:
            import boto3

            client = boto3.client("bedrock-runtime", region_name="us-east-1")

            # Anthropic Claude format
            if self._model.startswith("anthropic."):
                request_body = {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 1024,
                    "temperature": 0.0 if self._deterministic_mode else 0.0,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_message}],
                }
            # Amazon Titan / Meta Llama / other models
            else:
                request_body = {
                    "maxTokenCount": 1024,
                    "temperature": 0.0 if self._deterministic_mode else 0.0,
                    "textGenerationConfig": {
                        "maxTokenCount": 1024,
                        "temperature": 0.0 if self._deterministic_mode else 0.0,
                    },
                    "inputText": f"{system_prompt}\n\n{user_message}",
                }

            response = client.invoke_model(
                modelId=self._model,
                body=json.dumps(request_body),
            )

            response_body = json.loads(response["body"].read())

            # Parse response based on model type
            if self._model.startswith("anthropic."):
                answer_text = response_body.get("content", [{}])[0].get("text", "")
            else:
                answer_text = response_body.get("results", [{}])[0].get(
                    "outputText", ""
                )

            answer_supported = "I don't have enough information" not in answer_text
            generation_time = (time.perf_counter() - start_time) * 1000

            return {
                "text": answer_text,
                "answer_supported": answer_supported,
                "citations": [],
                "timings_ms": {"generation": generation_time},
            }

        except Exception as e:
            raise ValueError(f"Bedrock API call failed: {e}") from e


# Aliases for backward compatibility with imports
OpenAIGenerator = LLMGenerator
ClaudeGenerator = LLMGenerator
