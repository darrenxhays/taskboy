import pulumi
import pulumi_aws as aws

config = pulumi.Config("agent-harness")
environment = config.require("environment")
# all physical names are {resource_prefix}-{environment}-<thing>
resource_prefix = config.get("resource_prefix") or "agent-harness"
# the harness is deployed once, on the host environment
host_environment = config.get("host_environment") or "staging"

if environment == host_environment:
    issue_uploads_bucket = aws.s3.Bucket(
        "issue-uploads-bucket",
        bucket=f"{resource_prefix}-{environment}-issue-uploads",
    )

    aws.s3.BucketPublicAccessBlock(
        "issue-uploads-public-access-block",
        bucket=issue_uploads_bucket.id,
        block_public_acls=True,
        block_public_policy=True,
        ignore_public_acls=True,
        restrict_public_buckets=True,
    )
