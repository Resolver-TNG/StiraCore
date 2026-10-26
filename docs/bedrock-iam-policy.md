# Minimum IAM Policy for StiraCore (Bedrock provider)

> This page applies only when you use the **Bedrock** provider (the default).
> Other providers (Ollama, llama.cpp, OpenAI, Anthropic, …) need no AWS account
> or IAM policy; see [providers.md](providers.md).

StiraCore needs `bedrock:InvokeModel` and
`bedrock:InvokeModelWithResponseStream` for Bedrock inference.
This invocation-only policy covers the configured normal, cron, and heartbeat models.
It does not grant console discovery, model onboarding, or permissions used by tools.

## Policy

Replace `123456789012` with your AWS account ID before creating the policy.
This template targets the standard `aws` partition.
The configured IDs are Opus 5.5 (`global.anthropic.claude-opus-5-5`),
Sonnet 5, Haiku 4.5, and Astra; availability must be checked in your account.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeConfiguredGlobalProfiles",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*:123456789012:inference-profile/global.anthropic.claude-opus-5-5",
        "arn:aws:bedrock:*:123456789012:inference-profile/global.anthropic.claude-sonnet-5",
        "arn:aws:bedrock:*:123456789012:inference-profile/global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:*:123456789012:inference-profile/global.openai.gpt-6-astra"
      ]
    },
    {
      "Sid": "InvokeBackingFoundationModels",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:*::foundation-model/openai.gpt-6-astra"
      ]
    }
  ]
}
```

## Global inference profiles: important ARN requirements

**A `global.*` model ID is an inference profile, not a foundation-model ID.**
Profile ARNs contain your account ID and `inference-profile/global.*`;
foundation-model ARNs have an empty account field and no `global.` prefix.
Both the profile and its backing models must be authorized.
The region wildcard covers global routing destinations and source-region overrides;
it does not grant access to every model.

Before deployment, an administrator should inspect each profile in the Bedrock
console or run `aws bedrock get-inference-profile --inference-profile-identifier
PROFILE_ID --region ap-northeast-1` on one line.
That inspection requires `bedrock:GetInferenceProfile`, intentionally omitted here.
Compare the returned profile ARN and `models[].modelArn` values with this template:
do not assume removing `global.` always produces the actual backing model ID.
Update the policy to match returned ARNs if they differ.
Unavailable profile IDs cannot be made available by granting IAM permissions.

Global routing can change destination regions. Regional restrictions, SCPs,
permissions boundaries, or explicit denies can still prevent invocation.
Tighten region wildcards only after checking the profile's routing requirements.

## Create and attach

1. As an administrator, open IAM → Policies → Create policy → JSON.
2. Paste the adjusted JSON and save it as `StiraStrandsBedrockInvoke`.
3. For a workload, create an IAM role with the appropriate trust relationship
   (for example, EC2 or ECS tasks), attach the policy, and assign the role.
4. For local development, prefer an Identity Center role/permission set.
   If an IAM user is necessary, create one without administrator permissions,
   attach this policy, and configure its credentials securely.
5. Complete Bedrock model access/onboarding separately using an authorized administrator.

## Verify with non-admin credentials

Select the restricted profile with `AWS_PROFILE` as described in [setup](setup.md).
Remove conflicting environment credentials and check `aws sts get-caller-identity`.
Ensure the principal has no other policies granting broad Bedrock access.
With the project environment active, save and run this Python smoke test:
```python
import boto3

client = boto3.client("bedrock-runtime", region_name="ap-northeast-1")
print(
    client.converse(
        modelId="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        messages=[{"role": "user", "content": [{"text": "Reply OK."}]}],
        inferenceConfig={"maxTokens": 16},
    )["output"]["message"]
)
```
This incurs inference charges. Repeat for each configured model.
Also test streaming through Strands to verify `InvokeModelWithResponseStream`.
On access denial, check the caller identity, both ARN types, model access, and SCPs;
do not solve the problem by attaching administrator permissions.
