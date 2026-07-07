"""Phase 0 check: Bedrock Titan Embed Text v2 contract verification.

Verifies, in order:
  1. `amazon.titan-embed-text-v2:0` invoke_model accepts a body of
     {"inputText": ..., "dimensions": 1024} and returns a 1024-dim float
     vector (plus inputTextTokenCount).
  2. Token-limit behavior at/near the documented 8192-token input limit:
       - an input calibrated to ~95% of the limit must embed successfully;
       - an input calibrated to ~115% of the limit is sent to observe the
         hard-limit behavior (expected: ValidationException; alternatively
         silent truncation, detected via inputTextTokenCount).
     Either observed behavior is recorded; what matters for the pipeline is
     that the limit is characterized so embed.py can guard it explicitly.

Exits 0 on success, 1 on failure.

Usage:
    uv run scripts/check_titan.py

Discovered constraint (recorded per task 1.3)
---------------------------------------------
Titan v2 enforces a REQUEST-LEVEL CHARACTER LIMIT in addition to the token
limit: inputText over 50,000 characters is rejected up front with
`ValidationException: Malformed input request: expected maxLength: 50000`.
Ordinary English prose averages ~8.5 chars/token here, so a prose input hits
the 50k-char cap around ~5,900 tokens — BEFORE the 8192-token limit. The
token-limit probes below therefore use a token-dense unit (~2 chars/token) to
stay under 50k chars while approaching 8192 tokens. embed.py must guard BOTH
limits: len(text) <= 50,000 chars AND tokens <= 8,192 (800-token chunks stay
far below both).
"""

import json
import sys

import boto3
from botocore.exceptions import ClientError

from _common import get_region, print_result

MODEL_ID = "amazon.titan-embed-text-v2:0"
DIMENSIONS = 1024
TOKEN_LIMIT = 8192
CHAR_LIMIT = 50_000

# Token-dense probe unit (~2 chars/token) so token-limit probes fit inside the
# 50,000-character request cap. Calibrated against Titan's own token counter.
DENSE_UNIT = "9Zq3 "


def embed(client, text: str) -> dict:
    response = client.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({"inputText": text, "dimensions": DIMENSIONS}),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())


def check_basic_contract(client) -> bool:
    print(f"[1/3] invoke_model with dimensions={DIMENSIONS} on a short input")
    try:
        result = embed(client, "Hybrid search combines k-NN vectors with BM25.")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False
    vector = result.get("embedding")
    if not isinstance(vector, list) or len(vector) != DIMENSIONS:
        length = len(vector) if isinstance(vector, list) else "n/a"
        print(f"  FAIL: expected a {DIMENSIONS}-dim vector, got "
              f"{type(vector).__name__} of length {length}")
        return False
    if not all(isinstance(v, (int, float)) for v in vector[:10]):
        print("  FAIL: vector elements are not numeric")
        return False
    print(f"  OK: {len(vector)}-dim float vector, "
          f"inputTextTokenCount={result.get('inputTextTokenCount')}")
    return True


def calibrate_tokens_per_repeat(client) -> float:
    """Measure Titan's own token count for the repeated probe unit so the
    near/over-limit inputs are sized by Titan's tokenizer, not a guess."""
    repeats = 500
    result = embed(client, DENSE_UNIT * repeats)
    tokens = result["inputTextTokenCount"]
    per_repeat = tokens / repeats
    print(f"  calibration: {repeats} repeats ({repeats * len(DENSE_UNIT)} chars)"
          f" -> {tokens} tokens ({per_repeat:.3f} tokens/repeat)")
    return per_repeat


def build_probe(per_repeat: float, target_tokens: int) -> str:
    text = DENSE_UNIT * int(target_tokens / per_repeat)
    if len(text) > CHAR_LIMIT:
        raise RuntimeError(
            f"probe unit not dense enough: {target_tokens} tokens needs "
            f"{len(text)} chars (> {CHAR_LIMIT}-char request cap)"
        )
    return text


def check_near_limit(client, per_repeat: float) -> bool:
    print(f"[2/3] input near the {TOKEN_LIMIT}-token limit (~95%)")
    text = build_probe(per_repeat, int(TOKEN_LIMIT * 0.95))
    try:
        result = embed(client, text)
    except Exception as exc:
        print(f"  FAIL: near-limit input ({len(text)} chars) was rejected: {exc}")
        return False
    tokens = result["inputTextTokenCount"]
    vector_len = len(result.get("embedding") or [])
    if vector_len != DIMENSIONS:
        print(f"  FAIL: expected {DIMENSIONS}-dim vector, got {vector_len}")
        return False
    print(f"  OK: {tokens} tokens ({len(text)} chars) embedded successfully "
          f"into a {vector_len}-dim vector")
    return True


def check_over_limit(client, per_repeat: float) -> bool:
    print(f"[3/3] input over the {TOKEN_LIMIT}-token limit (~115%) — "
          "characterize the hard-limit behavior")
    text = build_probe(per_repeat, int(TOKEN_LIMIT * 1.15))
    try:
        result = embed(client, text)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        message = exc.response.get("Error", {}).get("Message")
        print(f"  OK: over-limit input ({len(text)} chars) raises {code}: "
              f"{message}")
        print("  -> behavior confirmed: HARD ERROR; embed.py must enforce the "
              f"{TOKEN_LIMIT}-token guard client-side")
        return True
    except Exception as exc:
        print(f"  FAIL: unexpected non-API error: {exc}")
        return False
    tokens = result["inputTextTokenCount"]
    if tokens <= TOKEN_LIMIT and len(result.get("embedding") or []) == DIMENSIONS:
        print(f"  OK: over-limit input silently truncated to {tokens} tokens")
        print("  -> behavior confirmed: SILENT TRUNCATION; embed.py should "
              "still guard to avoid losing chunk content")
        return True
    print(f"  FAIL: over-limit input accepted with {tokens} tokens and no "
          "identifiable truncation — behavior could not be characterized")
    return False


def main() -> int:
    client = boto3.client("bedrock-runtime", region_name=get_region())
    passed = check_basic_contract(client)
    if passed:
        try:
            per_repeat = calibrate_tokens_per_repeat(client)
            passed = check_near_limit(client, per_repeat)
            passed = check_over_limit(client, per_repeat) and passed
        except Exception as exc:
            print(f"  FAIL: {exc}")
            passed = False
    return print_result(passed, "check_titan")


if __name__ == "__main__":
    sys.exit(main())
