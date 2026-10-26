# 認証と環境構築 / Authentication & Environment

StiraCore uses the Strands Agents SDK with Amazon Bedrock.

## 前提条件 / Prerequisites

- Python 3.11 or newer.
- An AWS account with Bedrock access and permission to invoke the selected models.
- AWS CLI v2 for profile setup, SSO, and identity verification.
- A local checkout of StiraCore.

From the repository root, create an environment and install:
```bash
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
python -m pip install -e .
```

On Windows PowerShell, replace the activation command with:
```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```
If activation is blocked by your execution policy, run `.venv\Scripts\python.exe` directly.

## 認証の仕組み / How Authentication Works

`NXPAgent` calls `BedrockModel(model_id=..., region_name=...)`.
It does not pass a profile or `boto_session`; boto3 resolves credentials through
its default credential chain: environment variables, shared AWS credentials/config
(including selected profiles and SSO), or EC2/ECS role credentials.
Choose one method below. Never commit credentials or put them in documentation.
On EC2/ECS, prefer an attached IAM role rather than stored access keys.

### 方法1：環境変数 / Method 1: Environment Variables

macOS / Linux:
```bash
export AWS_ACCESS_KEY_ID="REPLACE_WITH_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="REPLACE_WITH_SECRET_ACCESS_KEY"
# Required only for temporary credentials:
export AWS_SESSION_TOKEN="REPLACE_WITH_SESSION_TOKEN"
aws sts get-caller-identity
```

Windows PowerShell:
```powershell
$env:AWS_ACCESS_KEY_ID="REPLACE_WITH_ACCESS_KEY_ID"
$env:AWS_SECRET_ACCESS_KEY="REPLACE_WITH_SECRET_ACCESS_KEY"
# Required only for temporary credentials:
$env:AWS_SESSION_TOKEN="REPLACE_WITH_SESSION_TOKEN"
aws sts get-caller-identity
```
For long-lived keys, leave `AWS_SESSION_TOKEN` unset; remove any stale token.

### 方法2：名前付きプロファイル / Method 2: Named Profile

Run this command and enter your access key, secret, and `ap-northeast-1`:
```bash
aws configure --profile stira
```
Select the profile in the terminal that will run StiraCore:
```bash
export AWS_PROFILE=stira                 # macOS / Linux
```
```powershell
$env:AWS_PROFILE="stira"                  # Windows PowerShell
```
Run `aws sts get-caller-identity` to verify. Credentials are stored in
`~/.aws/credentials` (Windows: `%USERPROFILE%\.aws\credentials`).
Remove previously exported access-key credentials so they do not override the profile.

### 方法3：SSO / Method 3: IAM Identity Center (SSO)

```bash
aws configure sso --profile stira-sso
aws sso login --profile stira-sso
```
Follow the prompts for your organization's SSO settings and AWS account/role.
Select `stira-sso` using `AWS_PROFILE` as above, then run
`aws sts get-caller-identity`. Re-run the login command when the session expires.
Remove conflicting environment credentials first.

## リージョン設定 / Region Configuration

`model_factory.create_model` defaults to `ap-northeast-1`.
Override it explicitly: `create_model(ModelRole.ORCHESTRATOR, region="us-west-2")`.
For direct SDK construction, use
`BedrockModel(model_id="YOUR_MODEL_ID", region_name="us-west-2")`.
AWS CLI commands can use `--region us-west-2`.
`AWS_DEFAULT_REGION` does not override an explicit `region_name` or the factory default.
Strings passed to `create_model` are literal model IDs, not role names.

## モデルアクセス / Model Access

In the AWS Bedrock console, verify/enable model access for the selected models
and complete any required provider onboarding or subscription steps.
IAM permissions alone do not guarantee model availability.
Normal routing uses Opus 5.5 for orchestrator, Astra for coder, Sonnet 5 for
reviewer/researcher, and Haiku 4.5 for fast gate.
Cron uses Haiku for orchestrator/fast gate and Sonnet for other roles;
heartbeat uses Haiku for every role.
See [Minimum IAM Policy](bedrock-iam-policy.md) for exact configured IDs and permissions.

## 検証環境と制限 / Tested Environments & Limitations

| OS | Status | Notes |
| --- | --- | --- |
| macOS | ✓ Tested | Local development supported |
| Windows | ✓ Tested with caveats | Enable Developer Mode for symlink tests |
| Linux | ✓ CI | Automated CI runs on Ubuntu only |

Windows symlink tests may fail without Developer Mode.
Ubuntu-only CI does not continuously validate macOS, Windows, or other Linux distributions.
