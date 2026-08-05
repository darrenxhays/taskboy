"""internet-facing alb for the mission control dashboard.

the alb terminates tls and runs auth0 oidc at the edge — unauthenticated requests
never reach the instance. the app additionally verifies the signed identity header
and enforces the email domain + admin allowlist (config.yaml dashboard section).

skipped entirely when dashboard_domain is unset — the dashboard is optional.
"""

import json

import pulumi
import pulumi_aws as aws
from vpc import public_subnet_ids, vpc_id

config = pulumi.Config("agent-harness")
environment = config.require("environment")
# all physical names are {resource_prefix}-{environment}-<thing>
resource_prefix = config.get("resource_prefix") or "agent-harness"
# the harness is deployed once, on the host environment — the dashboard rides along
host_environment = config.get("host_environment") or "staging"
# public hostname for the dashboard (e.g. "agent.example.com"); empty skips the alb,
# target group, dns records, and the auth0 secret read entirely
dashboard_domain = config.get("dashboard_domain") or ""
# route53 hosted zone the domain lives in (e.g. "example.com.")
route53_zone = config.get("route53_zone") or ""
# secrets manager bundle name; default derives from the environment
secret_name = config.get("secret_name") or f"AGENT_HARNESS_SECRETS_{environment.upper()}"

DASHBOARD_PORT = 8787

if environment == host_environment and dashboard_domain:
    from ec2 import instance, security_group

    def require_auth0_settings(secret_string: str) -> dict[str, str]:
        values = json.loads(secret_string)
        required = ("auth0_domain", "auth0_client_id", "auth0_client_secret")
        missing = [key for key in required if not str(values.get(key) or "").strip()]
        if missing:
            raise ValueError(f"{secret_name} is missing: {', '.join(missing)}")
        domain = str(values["auth0_domain"]).strip()
        if "://" in domain or "/" in domain:
            raise ValueError("auth0_domain must be a bare hostname without a scheme or trailing slash")
        return {key: str(values[key]).strip() for key in required}

    # read at pulumi eval time: the secret bundle must already hold the auth0 keys
    # (push secrets before `pulumi up` — see README.md)
    auth0_secret_version = aws.secretsmanager.get_secret_version_output(
        secret_id=secret_name,
    )
    auth0_settings = pulumi.Output.secret(auth0_secret_version.secret_string).apply(require_auth0_settings)
    AUTH0_DOMAIN = auth0_settings.apply(lambda values: values["auth0_domain"])
    AUTH0_CLIENT_ID = auth0_settings.apply(lambda values: values["auth0_client_id"])
    AUTH0_CLIENT_SECRET = auth0_settings.apply(lambda values: values["auth0_client_secret"])

    alb_security_group = aws.ec2.SecurityGroup(
        "alb-security-group",
        name=f"{resource_prefix}-{environment}-alb-security-group",
        description="mission control alb: https from anywhere, egress to the instance",
        vpc_id=vpc_id,
        ingress=[
            {"from_port": 443, "to_port": 443, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]},
            {"from_port": 80, "to_port": 80, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]},
        ],
        egress=[{"from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"]}],
    )

    # the instance had zero ingress before this; only the alb may reach the dashboard port
    aws.vpc.SecurityGroupIngressRule(
        "dashboard-from-alb",
        security_group_id=security_group.id,
        referenced_security_group_id=alb_security_group.id,
        from_port=DASHBOARD_PORT,
        to_port=DASHBOARD_PORT,
        ip_protocol="tcp",
    )

    zone = aws.route53.get_zone(name=route53_zone, private_zone=False)

    certificate = aws.acm.Certificate(
        "dashboard-certificate",
        domain_name=dashboard_domain,
        validation_method="DNS",
    )

    validation_record = aws.route53.Record(
        "dashboard-certificate-validation",
        zone_id=zone.zone_id,
        name=certificate.domain_validation_options[0].resource_record_name,
        type=certificate.domain_validation_options[0].resource_record_type,
        records=[certificate.domain_validation_options[0].resource_record_value],
        ttl=300,
    )

    certificate_validation = aws.acm.CertificateValidation(
        "dashboard-certificate-validated",
        certificate_arn=certificate.arn,
        validation_record_fqdns=[validation_record.fqdn],
    )

    alb = aws.lb.LoadBalancer(
        "dashboard-alb",
        name=f"{resource_prefix}-{environment}-dashboard",
        internal=False,
        load_balancer_type="application",
        security_groups=[alb_security_group.id],
        subnets=public_subnet_ids,
    )

    target_group = aws.lb.TargetGroup(
        "dashboard-target-group",
        name=f"{resource_prefix}-{environment}-dashboard",
        port=DASHBOARD_PORT,
        protocol="HTTP",
        vpc_id=vpc_id,
        target_type="instance",
        health_check={
            "path": "/healthz",
            "port": str(DASHBOARD_PORT),
            "matcher": "200",
            "interval": 30,
            "healthy_threshold": 2,
            "unhealthy_threshold": 3,
        },
    )

    aws.lb.TargetGroupAttachment(
        "dashboard-target",
        target_group_arn=target_group.arn,
        target_id=instance.id,
        port=DASHBOARD_PORT,
    )

    aws.lb.Listener(
        "dashboard-https",
        load_balancer_arn=alb.arn,
        port=443,
        protocol="HTTPS",
        ssl_policy="ELBSecurityPolicy-TLS13-1-2-2021-06",
        certificate_arn=certificate_validation.certificate_arn,
        default_actions=[
            {
                "type": "authenticate-oidc",
                "order": 1,
                "authenticate_oidc": {
                    "issuer": pulumi.Output.concat("https://", AUTH0_DOMAIN, "/"),
                    "authorization_endpoint": pulumi.Output.concat("https://", AUTH0_DOMAIN, "/authorize"),
                    "token_endpoint": pulumi.Output.concat("https://", AUTH0_DOMAIN, "/oauth/token"),
                    "user_info_endpoint": pulumi.Output.concat("https://", AUTH0_DOMAIN, "/userinfo"),
                    "client_id": AUTH0_CLIENT_ID,
                    "client_secret": AUTH0_CLIENT_SECRET,
                    "scope": "openid email",
                    "on_unauthenticated_request": "authenticate",
                    "session_timeout": 43200,  # 12h before re-auth
                },
            },
            {"type": "forward", "order": 2, "target_group_arn": target_group.arn},
        ],
    )

    aws.lb.Listener(
        "dashboard-http-redirect",
        load_balancer_arn=alb.arn,
        port=80,
        protocol="HTTP",
        default_actions=[{"type": "redirect", "redirect": {"port": "443", "protocol": "HTTPS", "status_code": "HTTP_301"}}],
    )

    aws.route53.Record(
        "dashboard-dns",
        zone_id=zone.zone_id,
        name=dashboard_domain,
        type="A",
        aliases=[{"name": alb.dns_name, "zone_id": alb.zone_id, "evaluate_target_health": True}],
    )

    pulumi.export("dashboard_url", f"https://{dashboard_domain}")
