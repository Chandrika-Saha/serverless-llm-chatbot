# Standard library imports - no additional dependencies needed, keeps Lambda package small
import json
import os
import logging
import base64
import hashlib
from typing import Any

# AWS SDK - included in Lambda runtime by default, no need to package it
import boto3
from botocore.exceptions import ClientError

# --- Logger Configuration ---
# Using Python's built-in logger (free, no third-party logging service costs)
logger = logging.getLogger()
# Default to WARNING level to reduce CloudWatch Logs ingestion costs
# CloudWatch charges $0.50/GB for log ingestion - fewer logs = lower costs
logger.setLevel(os.getenv("LOG_LEVEL", "WARNING"))

# --- Environment Variables ---
# AWS_REGION: Uses Lambda's default region, no cross-region data transfer costs
REGION = os.getenv("AWS_REGION", "us-east-1")

# MODEL_ID: Nova Micro is the cheapest Bedrock model
# Input: $0.000035/1K tokens, Output: $0.00014/1K tokens
# ~4-5x cheaper than Titan Lite, ~100x cheaper than Claude
MODEL_ID = os.getenv("MODEL_ID", "amazon.nova-micro-v1:0")

# MAX_PROMPT_LENGTH: Limits input tokens to control Bedrock costs
# Shorter prompts = fewer input tokens = lower costs
MAX_PROMPT_LENGTH = int(os.getenv("MAX_PROMPT_LENGTH", "2000"))

# --- Bedrock Client (Module-Level) ---
# Created outside handler to reuse TCP connections across warm invocations
# Reduces latency and avoids repeated connection overhead
bedrock = boto3.client("bedrock-runtime", region_name=REGION)

# --- CORS Headers ---
# ALLOWED_ORIGIN: Configurable via env var for security
# Using "*" in development, should be restricted in production
CORS_HEADERS = {
    "Access-Control-Allow-Origin": os.getenv("ALLOWED_ORIGIN", "*"),
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}

# --- In-Memory Cache (Module-Level) ---
# Persists across warm Lambda invocations (same execution environment)
# Caching repeated prompts avoids redundant Bedrock API calls
# Each cached hit saves ~$0.00002-0.00005 in Bedrock costs
_cache: dict[str, str] = {}


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """
    Helper function to build consistent HTTP responses.
    Reduces code duplication and ensures CORS headers are always included.

    Args:
        status: HTTP status code
        body: Response payload dictionary

    Returns:
        API Gateway compatible response dictionary
    """
    return {
        "statusCode": status,
        "headers": CORS_HEADERS,
        "body": json.dumps(body),
    }


def _get_http_method(event: dict) -> str:
    """
    Extract HTTP method from API Gateway event.
    Supports both HTTP API and REST API formats.

    HTTP API is ~70% cheaper than REST API:
    - HTTP API: $1.00 per million requests
    - REST API: $3.50 per million requests

    Args:
        event: Lambda event from API Gateway

    Returns:
        HTTP method string (GET, POST, OPTIONS, etc.)
    """
    # HTTP API format (cheaper, recommended)
    http_api_method = event.get("requestContext", {}).get("http", {}).get("method")
    # REST API format (fallback for compatibility)
    rest_api_method = event.get("httpMethod")
    # Return whichever is available, or empty string
    return http_api_method or rest_api_method or ""


def _parse_body(event: dict) -> dict:
    """
    Parse request body from API Gateway event.
    Handles both plain JSON and Base64-encoded bodies.

    REST API may send Base64-encoded bodies for binary content.
    Proper handling prevents runtime errors and unnecessary retries.

    Args:
        event: Lambda event from API Gateway

    Returns:
        Parsed JSON body as dictionary

    Raises:
        json.JSONDecodeError: If body is not valid JSON
    """
    # Get raw body, default to empty JSON object
    raw = event.get("body") or "{}"

    # REST API may Base64-encode the body
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")

    # Parse JSON string to dictionary
    return json.loads(raw)


def _get_cache_key(prompt: str) -> str:
    """
    Generate cache key from prompt text.

    Uses MD5 hash for consistent, fixed-length keys.
    Normalizes prompt (lowercase, stripped) to increase cache hits.
    More cache hits = fewer Bedrock API calls = lower costs.

    Args:
        prompt: User's input text

    Returns:
        32-character hexadecimal hash string
    """
    # Normalize: lowercase and strip whitespace for better cache hit rate
    normalized = prompt.lower().strip()
    # MD5 is fast and sufficient for cache keys (not security-critical)
    return hashlib.md5(normalized.encode()).hexdigest()


def lambda_handler(event: dict, context: Any) -> dict[str, Any]:
    """
    Main Lambda handler for the serverless chatbot.

    Cost optimization strategies used:
    1. In-memory caching to avoid redundant Bedrock calls
    2. Input length validation to prevent token abuse
    3. Low maxTokens setting to limit output costs
    4. Low temperature for shorter, more focused responses
    5. Minimal logging to reduce CloudWatch costs

    Args:
        event: API Gateway event containing HTTP request data
        context: Lambda context (unused but required by AWS)

    Returns:
        API Gateway compatible HTTP response
    """

    # --- CORS Preflight Handling ---
    # Browsers send OPTIONS request before actual POST
    # Return immediately with 204 (no content) to avoid Bedrock costs
    if _get_http_method(event) == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    # --- Configuration Validation ---
    # Fail fast if MODEL_ID not configured - prevents wasted compute time
    if not MODEL_ID:
        logger.error("MODEL_ID not configured")
        return _resp(500, {"error": "Service misconfigured"})

    # --- Request Body Parsing ---
    # Parse JSON body from API Gateway event
    try:
        body = _parse_body(event)
    except json.JSONDecodeError:
        # Return 400 immediately - don't waste time on invalid requests
        return _resp(400, {"error": "Body must be valid JSON"})

    # --- Input Validation ---
    # Extract and clean prompt text
    prompt = (body.get("prompt") or "").strip()

    # Reject empty prompts - no point calling Bedrock with nothing
    if not prompt:
        return _resp(400, {"error": "Missing 'prompt' in request body"})

    # Reject oversized prompts - prevents token abuse and runaway costs
    # Each character is roughly 0.25-0.5 tokens
    # 2000 chars ≈ 500-1000 tokens max input
    if len(prompt) > MAX_PROMPT_LENGTH:
        return _resp(400, {"error": f"Prompt exceeds {MAX_PROMPT_LENGTH} characters"})

    # --- Cache Lookup ---
    # Check if we've seen this prompt before (in this warm Lambda instance)
    cache_key = _get_cache_key(prompt)
    if cache_key in _cache:
        # Cache hit! Return cached response without calling Bedrock
        # This is essentially free - no Bedrock API costs
        logger.info("Cache hit")
        return _resp(200, {"answer": _cache[cache_key]})

    # --- Logging (Minimal) ---
    # Only log prompt length, not content (privacy + reduces log size)
    logger.info(f"Processing prompt of length {len(prompt)}")

    # --- Build Conversation for Converse API ---
    # Converse API is model-agnostic - same format works for Nova, Claude, etc.
    # Easier to switch models later without code changes
    messages = [
        {
            "role": "user",
            "content": [{"text": prompt}]
        }
    ]

    # --- Invoke Bedrock ---
    try:
        # Use converse() instead of invoke_model()
        # - Cleaner API: no manual JSON serialization needed
        # - Model-agnostic: same code works across different models
        # - Better error handling: structured exceptions
        response = bedrock.converse(
            modelId=MODEL_ID,
            messages=messages,
            # System prompt for consistent chatbot behavior
            # Instructing brevity helps reduce output tokens = lower costs
            system=[{"text": "You are a helpful, concise assistant. Keep responses brief."}],
            inferenceConfig={
                # maxTokens: Limits output length - biggest cost control lever
                # 150 tokens ≈ 100-120 words, sufficient for most answers
                # At $0.00014/1K output tokens, 150 tokens costs ~$0.00002
                "maxTokens": 150,

                # temperature: Lower = more deterministic/focused responses
                # Focused responses tend to be shorter = fewer output tokens
                "temperature": 0.3,

                # topP: Nucleus sampling threshold
                # Slightly restricted for more predictable outputs
                "topP": 0.85,
            }
        )

        # --- Extract Response ---
        # Response structure confirmed from AWS documentation example
        answer = response["output"]["message"]["content"][0]["text"]

        # --- Cache Storage ---
        # Store response for future identical prompts
        # Limit cache size to prevent Lambda memory issues (256MB recommended)
        # 100 entries × ~500 chars average = ~50KB, well within limits
        if answer and len(_cache) < 100:
            _cache[cache_key] = answer

        # --- Return Success Response ---
        return _resp(200, {"answer": answer})

    # --- Error Handling ---
    except ClientError as e:
        # AWS SDK errors - structured error information available
        error_code = e.response["Error"]["Code"]
        logger.error(f"Bedrock error: {error_code}")

        # Handle throttling specifically
        # Return 429 so clients can implement exponential backoff
        # Proper retry logic on client side prevents wasted Lambda invocations
        if error_code == "ThrottlingException":
            return _resp(429, {"error": "Rate limit exceeded"})

        # Generic error for other AWS errors
        # Don't expose internal details for security
        return _resp(500, {"error": "Model invocation failed"})

    except Exception:
        # Catch-all for unexpected errors
        # Log full exception for debugging (only when LOG_LEVEL is DEBUG/INFO)
        logger.exception("Unexpected error")
        # Generic error message - don't leak internals to client
        return _resp(500, {"error": "Internal server error"})
