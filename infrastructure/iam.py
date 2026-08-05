import json

import pulumi
import pulumi_aws as aws

config = pulumi.Config("agent-harness")
environment = config.require("environment")
# all physical names are {resource_prefix}-{environment}-<thing>
resource_prefix = config.get("resource_prefix") or "agent-harness"
# the harness is deployed once, on the host environment; host_account_id is that account's id
host_environment = config.get("host_environment") or "staging"
host_account_id = config.require("host_account_id")
# external id the orchestrator presents when assuming diagnostics roles
assume_external_id = config.get("assume_external_id") or "agent-harness"
# secrets manager bundle name; default derives from the environment
secret_name = config.get("secret_name") or f"AGENT_HARNESS_SECRETS_{environment.upper()}"
# github repo (e.g. "your-org/your-fork") trusted to deploy via oidc; empty skips the deployer role
github_repo = config.get("github_repo") or ""

ORCHESTRATOR_ROLE_ARN = f"arn:aws:iam::{host_account_id}:role/{resource_prefix}-{host_environment}-orchestrator"


# ---------------------------------------------------------------------------
# diagnostics role — created in EVERY stack so the agent can read each
# environment it is pointed at. assumed per task by the orchestrator with
# session name ar-<task_id>, which makes every cloudtrail entry task-attributable.
# writes are impossible: no mutating allows exist, plus explicit denies.
# ---------------------------------------------------------------------------

diagnostics_role = aws.iam.Role(
    "diagnostics-role",
    name=f"{resource_prefix}-{environment}-diagnostics",
    max_session_duration=3600,
    assume_role_policy=f"""{{
        "Version": "2012-10-17",
        "Statement": [
            {{
                "Effect": "Allow",
                "Principal": {{"AWS": "{ORCHESTRATOR_ROLE_ARN}"}},
                "Action": "sts:AssumeRole",
                "Condition": {{"StringEquals": {{"sts:ExternalId": "{assume_external_id}"}}}}
            }}
        ]
    }}""",
)

# metadata-shaped baseline: List*/Describe* without data-plane reads (unlike ReadOnlyAccess)
aws.iam.RolePolicyAttachment(
    "diagnostics-view-only-attachment",
    role=diagnostics_role.name,
    policy_arn="arn:aws:iam::aws:policy/job-function/ViewOnlyAccess",
)

# the diagnostic reads view-only misses: logs, metrics, traces, lambda config
aws.iam.RolePolicy(
    "diagnostics-supplement-policy",
    role=diagnostics_role.id,
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "DiagnosticReads",
                    "Effect": "Allow",
                    "Action": [
                        "logs:GetLogEvents",
                        "logs:FilterLogEvents",
                        "logs:GetLogRecord",
                        "logs:StartQuery",
                        "logs:GetQueryResults",
                        "logs:StopQuery",
                        "cloudwatch:GetMetricData",
                        "cloudwatch:GetMetricStatistics",
                        "xray:BatchGetTraces",
                        "xray:GetTraceSummaries",
                        "xray:GetTraceGraph",
                        "lambda:GetFunctionConfiguration",
                    ],
                    "Resource": "*",
                },
                {
                    # deny wins even if a future edit broadens the allows: no secrets,
                    # no parameter store, no kms, no s3 objects, no dynamodb items
                    "Sid": "NeverDataOrSecrets",
                    "Effect": "Deny",
                    "Action": [
                        "secretsmanager:GetSecretValue",
                        "ssm:GetParameter",
                        "ssm:GetParameters",
                        "ssm:GetParametersByPath",
                        "kms:Decrypt",
                        "s3:GetObject",
                        "s3:GetObjectVersion",
                        "dynamodb:GetItem",
                        "dynamodb:BatchGetItem",
                        "dynamodb:Query",
                        "dynamodb:Scan",
                    ],
                    "Resource": "*",
                },
                {
                    # approved regions only
                    "Sid": "ApprovedRegionsOnly",
                    "Effect": "Deny",
                    "Action": "*",
                    "Resource": "*",
                    "Condition": {"StringNotEquals": {"aws:RequestedRegion": "us-east-1"}},
                },
            ],
        }
    ),
)


# ---------------------------------------------------------------------------
# orchestrator role — only on the host environment, where the single ec2
# instance lives (one deployment serves all environments). reads the secret
# bundle, assumes the per-environment diagnostics roles, writes its own logs.
# ---------------------------------------------------------------------------

if environment == host_environment:
    account_id = aws.get_caller_identity().account_id

    orchestrator_role = aws.iam.Role(
        "orchestrator-role",
        name=f"{resource_prefix}-{host_environment}-orchestrator",
        assume_role_policy="""{
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole"
                }
            ]
        }""",
    )

    aws.iam.InstanceProfile(
        "orchestrator-instance-profile",
        name=f"{resource_prefix}-{host_environment}-orchestrator",
        role=orchestrator_role.name,
    )

    # ssm session manager access to the host (no ssh, no inbound ports)
    aws.iam.RolePolicyAttachment(
        "orchestrator-ssm-attachment",
        role=orchestrator_role.name,
        policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    )

    # github actions deployer: merge to main packages the app, uploads to the deployment
    # bucket, and triggers the update on the host via ssm run command. trust is limited to
    # the configured repo through the account's github oidc provider.
    if github_repo:
        deployer_role = aws.iam.Role(
            "deployer-role",
            name=f"{resource_prefix}-{host_environment}-deployer",
            assume_role_policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Federated": f"arn:aws:iam::{account_id}:oidc-provider/token.actions.githubusercontent.com"},
                            "Action": "sts:AssumeRoleWithWebIdentity",
                            "Condition": {
                                "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
                                "StringLike": {"token.actions.githubusercontent.com:sub": f"repo:{github_repo}:*"},
                            },
                        }
                    ],
                }
            ),
        )

        aws.iam.RolePolicy(
            "deployer-policy",
            role=deployer_role.id,
            policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "UploadRelease",
                            "Effect": "Allow",
                            "Action": "s3:PutObject",
                            "Resource": f"arn:aws:s3:::{resource_prefix}-{host_environment}-deployment-bucket/*",
                        },
                        {
                            "Sid": "RunUpdateOnHost",
                            "Effect": "Allow",
                            "Action": "ssm:SendCommand",
                            "Resource": [
                                "arn:aws:ssm:us-east-1::document/AWS-RunShellScript",
                                f"arn:aws:ec2:us-east-1:{account_id}:instance/*",
                            ],
                        },
                        {
                            "Sid": "WatchCommand",
                            "Effect": "Allow",
                            "Action": ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations", "ec2:DescribeInstances"],
                            "Resource": "*",
                        },
                    ],
                }
            ),
        )

    aws.iam.RolePolicy(
        "orchestrator-policy",
        role=orchestrator_role.id,
        policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "ReadOwnSecretBundle",
                        "Effect": "Allow",
                        "Action": "secretsmanager:GetSecretValue",
                        "Resource": f"arn:aws:secretsmanager:us-east-1:{account_id}:secret:{secret_name}-*",
                    },
                    {
                        # account id and environment are wildcarded because diagnostics roles may
                        # live in other accounts; the trust policy on each role is the real gate
                        "Sid": "AssumeDiagnosticsRoles",
                        "Effect": "Allow",
                        "Action": "sts:AssumeRole",
                        "Resource": f"arn:aws:iam::*:role/{resource_prefix}-*-diagnostics",
                    },
                    {
                        "Sid": "ServiceLogs",
                        "Effect": "Allow",
                        "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                        "Resource": f"arn:aws:logs:us-east-1:{account_id}:log-group:{resource_prefix}-*",
                    },
                    {
                        # write-only: object lock + no read/delete perms = shipped history is immutable to the host
                        "Sid": "ShipAuditLogs",
                        "Effect": "Allow",
                        "Action": "s3:PutObject",
                        "Resource": f"arn:aws:s3:::{resource_prefix}-{host_environment}-audit/*",
                    },
                    {
                        # read-only: pull ci-shipped release tarballs during deploys
                        "Sid": "FetchDeployArtifacts",
                        "Effect": "Allow",
                        "Action": "s3:GetObject",
                        "Resource": f"arn:aws:s3:::{resource_prefix}-{host_environment}-deployment-bucket/*",
                    },
                    {
                        "Sid": "IssueAttachments",
                        "Effect": "Allow",
                        "Action": ["s3:PutObject", "s3:GetObject"],
                        "Resource": f"arn:aws:s3:::{resource_prefix}-{host_environment}-issue-uploads/*",
                    },
                ],
            }
        ),
    )
