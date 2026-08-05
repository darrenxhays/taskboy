# infrastructure — reference AWS deployment

The harness itself only needs a Linux box with systemd (see `deploy/install.sh`).
This Pulumi project is one way to host it on AWS: a single private EC2 instance
(no inbound, SSM-managed), an S3 deployment bucket for CI tarballs, a write-only
audit bucket, an empty Secrets Manager shell, per-environment read-only
diagnostics roles, and an optional Auth0-protected ALB for the dashboard.

## Stack config (`pulumi.Config("agent-harness")`)

| key | meaning | default |
|---|---|---|
| `environment` | name of this stack's environment | required |
| `host_account_id` | AWS account id where the host instance lives | required |
| `host_environment` | the single environment that hosts the harness; host-only resources (ec2, buckets, secret, orchestrator/deployer roles, alb) are created when `environment == host_environment` | `staging` |
| `resource_prefix` | prefix for every physical name: `{resource_prefix}-{environment}-<thing>`; also the EC2 `Name` tag CI targets via SSM | `agent-harness` |
| `dashboard_domain` | public hostname for the dashboard (e.g. `agent.example.com`); empty/unset skips the ALB, target group, DNS records, and the Auth0 secret read entirely | unset |
| `route53_zone` | hosted zone for `dashboard_domain` (e.g. `example.com.`) | unset |
| `vpc_source` | `stack-reference` (read vpc outputs from another stack) or `lookup` (take ids from config) | `lookup` |
| `vpc_stack_ref` | stack to reference when `vpc_source: stack-reference` (e.g. `my-org/vpc/staging`) | unset |
| `vpc_id` / `private_subnet_ids` / `public_subnet_ids` | vpc + subnet ids when `vpc_source: lookup` (public only needed for the dashboard) | unset |
| `github_repo` | repo (e.g. `your-org/your-fork`) trusted by the OIDC deployer role; unset skips the role | unset |
| `assume_external_id` | sts external id the orchestrator presents to diagnostics roles | `agent-harness` |
| `secret_name` | Secrets Manager bundle name | `AGENT_HARNESS_SECRETS_{ENV}` |

## Ordering constraint

When `dashboard_domain` is set, `alb.py` reads the Auth0 credentials
(`auth0_domain`, `auth0_client_id`, `auth0_client_secret`) from the secret at
Pulumi eval time. Run the setup wizard's secret push — or create the secret and
`aws secretsmanager put-secret-value` manually — **before** `pulumi up`.
