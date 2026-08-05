import pulumi
import pulumi_aws as aws

config = pulumi.Config("agent-harness")
environment = config.require("environment")
# all physical names are {resource_prefix}-{environment}-<thing>
resource_prefix = config.get("resource_prefix") or "agent-harness"
# the harness is deployed once, on the host environment — audit records ship there
host_environment = config.get("host_environment") or "staging"

if environment == host_environment:
    # object lock must be enabled at creation; governance mode + 365d default retention
    # means even the host's own credentials cannot rewrite shipped history
    audit_bucket = aws.s3.Bucket(
        "audit-bucket",
        bucket=f"{resource_prefix}-{environment}-audit",
        object_lock_enabled=True,
    )

    aws.s3.BucketVersioning(
        "audit-bucket-versioning",
        bucket=audit_bucket.id,
        versioning_configuration={"status": "Enabled"},
    )

    aws.s3.BucketObjectLockConfiguration(
        "audit-bucket-object-lock",
        bucket=audit_bucket.id,
        rule={"default_retention": {"mode": "GOVERNANCE", "days": 365}},
    )
