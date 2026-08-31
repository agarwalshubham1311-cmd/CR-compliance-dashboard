"""
Shared AWS Bedrock client — replaces the local Ollama backend. Both
ai_classify.py and ai_draft.py call through call_bedrock() so there's one
place that knows how to talk to Bedrock, same pattern as the old
_call_ollama() helper.

Credentials: boto3 reads AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from
the environment automatically — no explicit credential handling needed
here. Set them via aws.env (see docker-compose.yml / run.ps1).
"""
import os
import boto3
from botocore.exceptions import ClientError, NoCredentialsError, EndpointConnectionError

AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _client


def call_bedrock(prompt, max_tokens=200, temperature=0.4):
    """Sends a single-turn prompt to the configured Bedrock model and
    returns the plain text response. Raises on any credential, network,
    or Bedrock-side error — callers already handle exceptions the same
    way they did for the old Ollama call (draft endpoints return a
    clear error to the frontend; ai_classify logs and skips)."""
    client = _get_client()
    try:
        response = client.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
    except NoCredentialsError:
        raise RuntimeError(
            "AWS credentials not found. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
            "in the Flask container's environment (see aws.env)."
        )
    except EndpointConnectionError as e:
        raise RuntimeError(f"Could not reach Bedrock in region {AWS_REGION}: {e}")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "AccessDeniedException":
            raise RuntimeError(
                f"AWS credentials don't have permission to invoke {BEDROCK_MODEL_ID}. "
                f"Check the IAM policy grants bedrock:InvokeModel / bedrock:Converse."
            )
        if code == "ValidationException" and "on-demand throughput" in str(e).lower():
            raise RuntimeError(
                f"{BEDROCK_MODEL_ID} isn't enabled for on-demand use in {AWS_REGION}. "
                f"Enable model access in the Bedrock console for this region."
            )
        raise RuntimeError(f"Bedrock error ({code}): {e}")

    return response["output"]["message"]["content"][0]["text"].strip()
