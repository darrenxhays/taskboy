import pulumi
import pulumi_aws as aws

config = pulumi.Config("agent-harness")
environment = config.require("environment")
# all physical names are {resource_prefix}-{environment}-<thing>
resource_prefix = config.get("resource_prefix") or "agent-harness"
# the harness is deployed once, on the host environment — ci ships tarballs here and the host pulls them
host_environment = config.get("host_environment") or "staging"

if environment == host_environment:
    aws.s3.Bucket(
        "deployment-bucket",
        bucket=f"{resource_prefix}-{environment}-deployment-bucket",
        opts=pulumi.ResourceOptions(ignore_changes=["server_side_encryption_configuration"]),
    )
