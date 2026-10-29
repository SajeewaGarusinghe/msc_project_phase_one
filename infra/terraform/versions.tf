terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Local state is fine for a single-student research bench. If you later want
  # remote state, add an S3 backend here -- but note S3 + DynamoDB locking adds
  # a small monthly cost, so it is deliberately left out while on credits.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project_name
      Phase       = "P0-bench"
      ManagedBy   = "terraform"
      CostCenter  = "msc-research"
    }
  }
}
