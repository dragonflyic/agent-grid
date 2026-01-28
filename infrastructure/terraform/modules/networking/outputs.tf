output "private_subnet_ids" {
  description = "Private subnet IDs for App Runner VPC connector"
  value       = aws_subnet.private[*].id
}

output "nat_gateway_id" {
  description = "NAT Gateway ID"
  value       = aws_nat_gateway.main.id
}

output "nat_public_ip" {
  description = "NAT Gateway public IP"
  value       = aws_eip.nat.public_ip
}
