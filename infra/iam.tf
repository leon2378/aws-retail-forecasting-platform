locals {

  lambda_assume = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }] })

}

resource "aws_iam_role" "api" {

  name               = "${local.prefix}-api"
  assume_role_policy = local.lambda_assume

}
resource "aws_iam_role" "ingest" {

  name               = "${local.prefix}-ingest"
  assume_role_policy = local.lambda_assume

}
resource "aws_iam_role_policy" "api" {

  role = aws_iam_role.api.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:Query"], Resource = aws_dynamodb_table.results.arn },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.api.arn}:*" }
  ] })

}
resource "aws_iam_role_policy" "ingest" {

  role = aws_iam_role.ingest.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.results.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = ["${aws_s3_bucket.app["data"].arn}/raw/*", "${aws_s3_bucket.app["data"].arn}/runs/*"] },
    { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.app["data"].arn },
    { Effect = "Allow", Action = "sagemaker:StartPipelineExecution", Resource = local.pipeline_arn },
    { Effect = "Allow", Action = "sagemaker:DescribePipelineExecution", Resource = "${local.pipeline_arn}/execution/*" },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.ingest.arn}:*" }
  ] })

}

resource "aws_iam_role" "sagemaker" {

  name = "${local.prefix}-sagemaker"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow",
    Principal = { Service = "sagemaker.amazonaws.com" }, Action = "sts:AssumeRole",
    Condition = { StringEquals = { "aws:SourceAccount" = local.account }, ArnLike = { "aws:SourceArn" = "${local.arn_prefix}:sagemaker:${var.region}:${local.account}:*" } }
  }] })

}
resource "aws_iam_role_policy" "sagemaker" {

  role = aws_iam_role.sagemaker.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"], Resource = "${aws_s3_bucket.app["data"].arn}/*" },
    { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.app["data"].arn },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:BatchWriteItem"], Resource = aws_dynamodb_table.results.arn },
    { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = aws_ecr_repository.model.arn },
    { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"], Resource = "${local.arn_prefix}:logs:${var.region}:${local.account}:log-group:/aws/sagemaker/*" },
    { Effect = "Allow", Action = ["cloudwatch:PutMetricData"], Resource = "*", Condition = { StringEquals = { "cloudwatch:namespace" = "/aws/sagemaker/TrainingJobs" } } },
    { Effect = "Allow", Action = ["sagemaker:CreateProcessingJob", "sagemaker:DescribeProcessingJob", "sagemaker:StopProcessingJob", "sagemaker:CreateTrainingJob", "sagemaker:DescribeTrainingJob", "sagemaker:StopTrainingJob", "sagemaker:CreateTransformJob", "sagemaker:DescribeTransformJob", "sagemaker:StopTransformJob", "sagemaker:CreateModel", "sagemaker:DescribeModel", "sagemaker:DeleteModel", "sagemaker:AddTags"],
    Resource = [for kind in ["processing-job", "training-job", "transform-job", "model"] : "${local.arn_prefix}:sagemaker:${var.region}:${local.account}:${kind}/${local.prefix}-*"] },
    { Effect = "Allow", Action = ["sagemaker:CreateModelPackage", "sagemaker:DescribeModelPackage", "sagemaker:UpdateModelPackage", "sagemaker:AddTags"],
    Resource = ["${local.arn_prefix}:sagemaker:${var.region}:${local.account}:model-package/${local.prefix}/*", aws_sagemaker_model_package_group.forecast.arn] },
    { Effect = "Allow", Action = "iam:PassRole", Resource = aws_iam_role.sagemaker.arn, Condition = { StringEquals = { "iam:PassedToService" = "sagemaker.amazonaws.com" } } }
  ] })

}
