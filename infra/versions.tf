terraform {

  required_version = ">= 1.6, < 2.0"
  required_providers {

    aws = { source = "hashicorp/aws", version = "~> 6.0" }

  }

}

provider "aws" {

  region = var.region
  default_tags {
    tags = { Project = var.project, ManagedBy = "Terraform" }
  }

}

data "aws_caller_identity" "current" {
}
data "aws_partition" "current" {
}

locals {

  prefix       = var.project
  account      = data.aws_caller_identity.current.account_id
  partition    = data.aws_partition.current.partition
  arn_prefix   = "arn:${local.partition}"
  pipeline_arn = "${local.arn_prefix}:sagemaker:${var.region}:${local.account}:pipeline/${local.prefix}"
  bucket_names = {
    data     = "${local.prefix}-${local.account}-${var.region}-data"
    frontend = "${local.prefix}-${local.account}-${var.region}-web"
    delivery = "${local.prefix}-${local.account}-${var.region}-delivery"
  }

}
