# VPC, per-AZ private (node/pod) subnets, public subnets for egress, and a single NAT
# gateway. The VPC CNI hands pod IPs out of the node subnet, so the private subnets are
# sized (/20 each) to hold every pod, not just every node.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # Private subnets take the low CIDR blocks (0, 1, 2, ...); public subnets take blocks
  # high in the space so the two ranges never collide as az_count grows.
  private_subnet_cidrs = [
    for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, var.private_subnet_newbits, i)
  ]
  public_subnet_cidrs = [
    for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, var.public_subnet_newbits, pow(2, var.public_subnet_newbits) - 1 - i)
  ]
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "vpc-${local.cluster_name}" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "igw-${local.cluster_name}" }
}

# Private subnets — where the node group and every pod IP live. Tagged for EKS subnet
# discovery and internal load balancers.
resource "aws_subnet" "private" {
  count             = var.az_count
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = {
    Name                              = "snet-private-${local.cluster_name}-${local.azs[count.index]}"
    "kubernetes.io/role/internal-elb" = "1"
  }
}

# Public subnets — NAT egress and the optional ALB ingress.
resource "aws_subnet" "public" {
  count                   = var.az_count
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.public_subnet_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name                     = "snet-public-${local.cluster_name}-${local.azs[count.index]}"
    "kubernetes.io/role/elb" = "1"
  }
}

# Single NAT gateway for private-subnet egress (image pulls, S3, control-plane reach).
# One NAT (not one per AZ) keeps the PoC cheap; production would use one per AZ for HA.
resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "eip-nat-${local.cluster_name}" }
}

resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "nat-${local.cluster_name}" }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "rt-public-${local.cluster_name}" }
}

resource "aws_route_table_association" "public" {
  count          = var.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this.id
  }
  tags = { Name = "rt-private-${local.cluster_name}" }
}

resource "aws_route_table_association" "private" {
  count          = var.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}
