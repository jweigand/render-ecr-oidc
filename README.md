# render-ecr-oidc

A Render cron job that keeps a Render **registry credential** for AWS ECR valid. It needs no long-lived AWS keys.

Every 6 hours it:

1. Authenticates to AWS through [Render's managed OIDC](https://render.com/docs/oidc). boto3 reads `AWS_ROLE_ARN` and the Render-provided `AWS_WEB_IDENTITY_TOKEN_FILE`.
2. Calls `ecr:GetAuthorizationToken`. The token lasts 12 hours.
3. Updates the Render registry credential through `PATCH /v1/registrycredentials/{id}`. If no credential with that name exists yet, it creates one.

## Why this design

Render's OIDC doesn't cover image pulls: per the [OIDC docs](https://render.com/docs/oidc#limitations), "OIDC auth does not currently enable image-backed services to pull private images from AWS ECR." So a registry credential has to be refreshed more often than every 12 hours.

**Why a cron job and not cron plus a workflow:** Workflows have [no native scheduling](https://render.com/docs/workflows), so you'd need a cron job to trigger them anyway. The work is three API calls that finish in a few seconds. A workflow would add a second service, the Render SDK and another failure point, and wouldn't add any durability. The cron job alone gets its durability from:

- **A 6-hour schedule against a 12-hour token.** Any single failed run still leaves a valid token.
- **Retries inside each run.** boto3 uses its standard retry mode, and Render API calls retry on 429 and 5xx with backoff.
- **A non-zero exit on failure.** Render marks the run failed, so failure notifications fire.
- **At most one active run at a time.** Render guarantees this for cron jobs.
- **Idempotent runs.** The credential is found by name, then created or patched. Re-running is always safe.

## Setup

Requires a **Pro** workspace or higher, which OIDC needs.

1. **AWS OIDC provider.** In IAM, add an OpenID Connect provider:
   - URL: `https://oidc.render.com/<WORKSPACE_ID>`
   - Audience: `sts.amazonaws.com`
2. **Deploy the Blueprint** (`render.yaml`) and fill in the env vars marked `sync: false`. The first run will fail until steps 3 and 4 are done.
3. **IAM role.** Create a role using [`aws/trust-policy.json`](aws/trust-policy.json) and [`aws/permissions-policy.json`](aws/permissions-policy.json).
   - Scope the `sub` condition to the cron job's `srv-...` ID. Check the exact subject format against the [OIDC docs](https://render.com/docs/oidc).
   - The pull permissions in the policy aren't used by this job. They're there because the ECR token inherits the caller's permissions, and Render needs those permissions to pull with the token.
4. **Set the env vars**, then redeploy so Render injects `AWS_WEB_IDENTITY_TOKEN_FILE`:

   | Var | Value |
   |---|---|
   | `AWS_ROLE_ARN` | ARN of the role from step 3 |
   | `AWS_REGION` | ECR region, e.g. `us-east-1` |
   | `RENDER_API_KEY` | A Render API key |
   | `RENDER_WORKSPACE_ID` | `tea-...` (owner of the credential) |
   | `REGISTRY_CREDENTIAL_NAME` | Defaults to `aws-ecr` |
   | `REGISTRY_CREDENTIAL_ID` | Optional. Patch this ID directly and skip the lookup |
   | `REDEPLOY_SERVICE_IDS` | Optional. Comma-separated `srv-...` IDs to redeploy after each refresh |

5. **Trigger a run** from the dashboard. Then point your image-backed services at the `aws-ecr` credential. The image URL is `<account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>`.

## Notes

- The token belongs to one region. To pull from several regions, run one cron job per region, each with its own credential name.
- `RENDER_API_KEY` is the only long-lived secret, and Render's API has no OIDC equivalent. Store it only in this service's env.
- The API reference says services using a credential "must be redeployed to use updated values". Render pulls images on every deploy, restart and scale event. If those pulls turn out to use a stale copy of the credential, set `REDEPLOY_SERVICE_IDS`. Verify this in your workspace before relying on it.
