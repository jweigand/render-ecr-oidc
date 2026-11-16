"""Refresh a Render registry credential with a fresh AWS ECR token.

Runs as a Render cron job. AWS auth uses Render's managed OIDC: Render sets
AWS_WEB_IDENTITY_TOKEN_FILE when AWS_ROLE_ARN is present, and boto3 picks both
up automatically, so there are no long-lived AWS keys.

Env vars:
  AWS_ROLE_ARN             (required) IAM role trusted for this service's OIDC subject
  AWS_REGION               (required) region of the ECR registry
  RENDER_API_KEY           (required) Render API key used to update the credential
  RENDER_WORKSPACE_ID      (required unless REGISTRY_CREDENTIAL_ID is set) tea-... owner id
  REGISTRY_CREDENTIAL_NAME (optional) credential name to find or create; default "aws-ecr"
  REGISTRY_CREDENTIAL_ID   (optional) update this credential id directly, skipping lookup
  REDEPLOY_SERVICE_IDS     (optional) comma-separated srv-... ids to redeploy after refresh
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.config import Config

RENDER_API = "https://api.render.com/v1"


def log(msg):
    print(f"[ecr-refresh] {msg}", flush=True)


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"[ecr-refresh] missing required env var {name}")
    return val


def render_api(method, path, body=None, attempts=4):
    """Call the Render API with retries on 429/5xx and network errors."""
    url = f"{RENDER_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {env('RENDER_API_KEY', required=True)}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            retryable = e.code == 429 or e.code >= 500
            if not retryable or attempt == attempts:
                raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from None
            log(f"{method} {path} -> {e.code}, retrying (attempt {attempt}/{attempts})")
        except urllib.error.URLError as e:
            if attempt == attempts:
                raise RuntimeError(f"{method} {path} failed: {e.reason}") from None
            log(f"{method} {path} network error {e.reason}, retrying")
        time.sleep(2 ** attempt)


def get_ecr_token(region):
    """Return (account_id, password) for the ECR registry in `region`."""
    cfg = Config(region_name=region, retries={"max_attempts": 5, "mode": "standard"})
    account_id = boto3.client("sts", config=cfg).get_caller_identity()["Account"]
    auth = boto3.client("ecr", config=cfg).get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    assert user == "AWS", f"unexpected ECR token user {user!r}"
    log(f"got ECR token for {auth['proxyEndpoint']} (expires {auth['expiresAt'].isoformat()})")
    return account_id, password


def find_credential_id(name, owner_id):
    query = urllib.parse.urlencode({"name": name, "ownerId": owner_id, "limit": 100})
    items = render_api("GET", f"/registrycredentials?{query}") or []
    for item in items:
        cred = item.get("registryCredential", item)  # tolerate wrapped or bare items
        if cred.get("name") == name and cred.get("registry") == "AWS_ECR":
            return cred["id"]
    return None


def main():
    region = env("AWS_REGION", required=True)
    env("AWS_ROLE_ARN", required=True)
    if not os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE"):
        sys.exit("[ecr-refresh] AWS_WEB_IDENTITY_TOKEN_FILE not set; is Render OIDC enabled "
                 "for this workspace (Pro+) and was the service redeployed after adding AWS_ROLE_ARN?")

    name = env("REGISTRY_CREDENTIAL_NAME", "aws-ecr")
    cred_id = env("REGISTRY_CREDENTIAL_ID")

    account_id, password = get_ecr_token(region)
    body = {"registry": "AWS_ECR", "name": name, "username": account_id, "authToken": password}

    if not cred_id:
        owner_id = env("RENDER_WORKSPACE_ID", required=True)
        cred_id = find_credential_id(name, owner_id)
        if not cred_id:
            created = render_api("POST", "/registrycredentials", {**body, "ownerId": owner_id})
            log(f"created registry credential {created['id']} ({name})")
            cred_id = None  # nothing to patch

    if cred_id:
        updated = render_api("PATCH", f"/registrycredentials/{cred_id}", body)
        log(f"updated registry credential {updated['id']} ({name}) at {updated.get('updatedAt')}")

    for svc in filter(None, (s.strip() for s in env("REDEPLOY_SERVICE_IDS", "").split(","))):
        deploy = render_api("POST", f"/services/{svc}/deploys", {})
        log(f"triggered deploy {deploy.get('id')} for {svc}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # non-zero exit marks the cron run failed in Render
        log(f"FAILED: {e}")
        sys.exit(1)
