# Networking module for NAT Gateway access
# Required for App Runner VPC connector to reach external services (GitHub API, SQS)

# Get availability zones
data "aws_availability_zones" "available" {
  state = "available"
}

# Elastic IP for NAT Gateway
resource "aws_eip" "nat" {
  domain = "vpc"

  tags = merge(var.tags, {
    Name = "${var.project_name}-nat-eip"
  })
}

# NAT Gateway in one of the public subnets
resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = var.public_subnet_id

  tags = merge(var.tags, {
    Name = "${var.project_name}-nat"
  })

  depends_on = [aws_eip.nat]
}

# Private subnets for App Runner VPC connector
resource "aws_subnet" "private" {
  count = 2

  vpc_id            = var.vpc_id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 8) # Use 172.31.128.0/20 and 172.31.144.0/20 for default VPC
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = merge(var.tags, {
    Name = "${var.project_name}-private-${count.index + 1}"
    Type = "private"
  })
}

# Route table for private subnets
resource "aws_route_table" "private" {
  vpc_id = var.vpc_id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-private-rt"
  })
}

# Associate private subnets with route table
resource "aws_route_table_association" "private" {
  count = 2

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}
