# Placeholder deployment topology for Siyona (R2 target: single region, ECS Fargate).
# Nothing here is applied yet; `terraform validate` is wired into CI from R2.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

resource "aws_ecr_repository" "services" {
  name                 = "${var.project}-services"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_cloudwatch_log_group" "services" {
  name              = "/${var.project}/services"
  retention_in_days = var.log_retention_days
}
