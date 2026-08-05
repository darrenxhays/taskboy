import pulumi
import pulumi_aws as aws
from vpc import private_subnet_ids, vpc_id

config = pulumi.Config("agent-harness")
environment = config.require("environment")
# all physical names are {resource_prefix}-{environment}-<thing>
resource_prefix = config.get("resource_prefix") or "agent-harness"
# the harness is deployed once, on the host environment — no instances in other stacks
host_environment = config.get("host_environment") or "staging"

if environment == host_environment:
    security_group = aws.ec2.SecurityGroup(
        "security-group",
        name=f"{resource_prefix}-{environment}-security-group",
        description="agent harness host: no inbound (slack socket mode is outbound), open egress",
        vpc_id=vpc_id,
        egress=[
            {
                "from_port": 0,
                "to_port": 0,
                "protocol": "-1",
                "cidr_blocks": ["0.0.0.0/0"],
            }
        ],
    )

    ami = aws.ec2.get_ami(
        most_recent=True,
        owners=["amazon"],
        filters=[
            {"name": "name", "values": ["al2023-ami-2023*-x86_64"]},
            {"name": "state", "values": ["available"]},
        ],
    )

    instance = aws.ec2.Instance(
        "instance",
        ami=ami.id,
        instance_type="t3.large",
        subnet_id=private_subnet_ids[0],
        vpc_security_group_ids=[security_group.id],
        iam_instance_profile=f"{resource_prefix}-{environment}-orchestrator",
        root_block_device={
            "volume_size": 100,
            "volume_type": "gp3",
        },
        metadata_options={
            # imdsv2 only; hop limit 1 keeps containers/nat'd processes away from the role
            "http_tokens": "required",
            "http_put_response_hop_limit": 1,
        },
        # the Name tag is the ssm deploy selector: ci targets Key=tag:Name,Values=<resource_prefix>
        tags={"Name": resource_prefix},
        opts=pulumi.ResourceOptions(ignore_changes=["ami"]),  # don't replace the host on every ami release
    )
