resource "aws_s3_bucket" "app" {

  for_each      = local.bucket_names
  bucket        = each.value
  force_destroy = false

}

resource "aws_s3_bucket_versioning" "app" {

  for_each = aws_s3_bucket.app
  bucket   = each.value.id
  versioning_configuration {
    status = "Enabled"
  }

}

resource "aws_s3_bucket_server_side_encryption_configuration" "app" {

  for_each = aws_s3_bucket.app
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }

}

resource "aws_s3_bucket_public_access_block" "app" {

  for_each                = aws_s3_bucket.app
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

}

resource "aws_s3_bucket_policy" "app" {

  for_each = aws_s3_bucket.app
  bucket   = each.value.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Sid = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
    Resource = [each.value.arn, "${each.value.arn}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } } }
    ], each.key == "frontend" ? [
    { Sid    = "CloudFrontRead", Effect = "Allow", Principal = { Service = "cloudfront.amazonaws.com" },
      Action = "s3:GetObject", Resource = "${each.value.arn}/*",
    Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.app.arn } } }
  ] : []) })

}

resource "aws_s3_bucket_lifecycle_configuration" "data" {

  bucket     = aws_s3_bucket.app["data"].id
  depends_on = [aws_s3_bucket_versioning.app]
  rule {

    id     = "OldNoncurrentSnapshots"
    status = "Enabled"
    filter {
      prefix = "runs/"
    }
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

  }

}

resource "aws_dynamodb_table" "results" {

  name         = "${local.prefix}-results"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"
  attribute {
    name = "pk"
    type = "S"

  }
  attribute {
    name = "sk"
    type = "S"

  }
  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled = true
  }

}

resource "aws_ecr_repository" "model" {

  name                 = local.prefix
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }

}

resource "aws_ecr_lifecycle_policy" "model" {

  repository = aws_ecr_repository.model.name
  policy = jsonencode({ rules = [{ rulePriority = 1, description = "Expire only untagged images after 14 days",
    selection = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 14 },
  action = { type = "expire" } }] })

}
