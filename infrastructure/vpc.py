import pulumi

config = pulumi.Config("agent-harness")

# vpc_source: "stack-reference" pulls vpc_id/subnet ids from another pulumi stack (vpc_stack_ref,
# e.g. "my-org/vpc/staging"); "lookup" (default) takes them straight from stack config
# (vpc_id, private_subnet_ids, public_subnet_ids).
vpc_source = config.get("vpc_source") or "lookup"

if vpc_source == "stack-reference":
    vpc = pulumi.StackReference(config.require("vpc_stack_ref"))
    vpc_id = vpc.require_output("vpc_id")
    private_subnet_ids = vpc.require_output("private_subnet_ids")
    public_subnet_ids = vpc.require_output("public_subnet_ids")
else:
    vpc_id = config.require("vpc_id")
    private_subnet_ids = config.require_object("private_subnet_ids")
    # public subnets are only needed when dashboard_domain is set (alb.py)
    public_subnet_ids = config.get_object("public_subnet_ids") or []
