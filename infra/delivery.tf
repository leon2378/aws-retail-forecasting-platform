resource "aws_iam_role" "build" {

  count              = var.enable_delivery ? 1 : 0
  name               = "${local.prefix}-build"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole" }] })

}
resource "aws_iam_role" "deploy" {

  count              = var.enable_delivery ? 1 : 0
  name               = "${local.prefix}-deploy"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole" }] })

}
resource "aws_cloudwatch_log_group" "delivery" {

  count             = var.enable_delivery ? 1 : 0
  name              = "/aws/codebuild/${local.prefix}"
  retention_in_days = 30

}
resource "aws_iam_role_policy" "build" {

  count = var.enable_delivery ? 1 : 0
  role  = aws_iam_role.build[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], Resource = "${aws_s3_bucket.app["delivery"].arn}/*" },
    { Effect = "Allow", Action = ["s3:GetBucketLocation", "s3:GetBucketVersioning"], Resource = aws_s3_bucket.app["delivery"].arn },
    { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"], Resource = aws_ecr_repository.model.arn },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.delivery[0].arn}:*" }
  ] })

}
resource "aws_iam_role_policy" "deploy" {

  count = var.enable_delivery ? 1 : 0
  role  = aws_iam_role.deploy[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.app["delivery"].arn}/*" },
    { Effect = "Allow", Action = ["s3:GetBucketLocation", "s3:GetBucketVersioning"], Resource = aws_s3_bucket.app["delivery"].arn },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.app["frontend"].arn },
    { Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.app["frontend"].arn}/*" },
    { Effect = "Allow", Action = ["lambda:UpdateFunctionCode", "lambda:GetFunctionConfiguration"], Resource = [aws_lambda_function.api.arn, aws_lambda_function.ingest.arn] },
    { Effect = "Allow", Action = "cloudfront:CreateInvalidation", Resource = aws_cloudfront_distribution.app.arn },
    { Effect = "Allow", Action = "sagemaker:UpdatePipeline", Resource = local.pipeline_arn },
    { Effect = "Allow", Action = "iam:PassRole", Resource = aws_iam_role.sagemaker.arn, Condition = { StringEquals = { "iam:PassedToService" = "sagemaker.amazonaws.com" } } },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.delivery[0].arn}:*" }
  ] })

}
locals {

  build_environment = {
    PROJECT_NAME            = local.prefix, ECR_URI = aws_ecr_repository.model.repository_url,
    SAGEMAKER_ROLE_ARN      = aws_iam_role.sagemaker.arn, DATA_BUCKET = aws_s3_bucket.app["data"].id,
    RESULTS_TABLE           = aws_dynamodb_table.results.name,
    SAGEMAKER_INSTANCE_TYPE = var.sagemaker_instance_type
  }
  deploy_environment = {
    PROJECT_NAME    = local.prefix, SAGEMAKER_ROLE_ARN = aws_iam_role.sagemaker.arn,
    FRONTEND_BUCKET = aws_s3_bucket.app["frontend"].id, DISTRIBUTION_ID = aws_cloudfront_distribution.app.id
  }

}
resource "aws_codebuild_project" "build" {

  count         = var.enable_delivery ? 1 : 0
  name          = "${local.prefix}-build"
  service_role  = aws_iam_role.build[0].arn
  build_timeout = 30
  artifacts {
    type = "CODEPIPELINE"
  }
  source {
    type = "CODEPIPELINE"
  }
  environment {

    compute_type    = "BUILD_GENERAL1_MEDIUM"
    image           = "aws/codebuild/standard:7.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = true
    dynamic "environment_variable" {

      for_each = local.build_environment
      content {
        name  = environment_variable.key
        value = environment_variable.value

      }

    }

  }
  logs_config {
    cloudwatch_logs {
      group_name  = aws_cloudwatch_log_group.delivery[0].name
      stream_name = "build"

    }
  }

}
resource "aws_codebuild_project" "deploy" {

  count         = var.enable_delivery ? 1 : 0
  name          = "${local.prefix}-deploy"
  service_role  = aws_iam_role.deploy[0].arn
  build_timeout = 15
  artifacts {
    type = "CODEPIPELINE"
  }
  source {
    type      = "CODEPIPELINE"
    buildspec = "aws/buildspec-deploy.yml"

  }
  environment {

    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/standard:7.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = false
    dynamic "environment_variable" {

      for_each = local.deploy_environment
      content {
        name  = environment_variable.key
        value = environment_variable.value

      }

    }

  }
  logs_config {
    cloudwatch_logs {
      group_name  = aws_cloudwatch_log_group.delivery[0].name
      stream_name = "deploy"

    }
  }

}
resource "aws_iam_role" "pipeline" {

  count              = var.enable_delivery ? 1 : 0
  name               = "${local.prefix}-delivery"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "codepipeline.amazonaws.com" }, Action = "sts:AssumeRole" }] })

}
resource "aws_iam_role_policy" "pipeline" {

  count = var.enable_delivery ? 1 : 0
  role  = aws_iam_role.pipeline[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], Resource = "${aws_s3_bucket.app["delivery"].arn}/*" },
    { Effect = "Allow", Action = ["s3:GetBucketVersioning", "s3:GetBucketLocation"], Resource = aws_s3_bucket.app["delivery"].arn },
    { Effect = "Allow", Action = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"], Resource = [aws_codebuild_project.build[0].arn, aws_codebuild_project.deploy[0].arn] }
  ] })

}
resource "aws_codepipeline" "delivery" {

  count          = var.enable_delivery ? 1 : 0
  name           = local.prefix
  role_arn       = aws_iam_role.pipeline[0].arn
  pipeline_type  = "V2"
  execution_mode = "QUEUED"
  artifact_store {
    location = aws_s3_bucket.app["delivery"].id
    type     = "S3"

  }
  stage {

    name = "Source"
    action {

      name             = "SourceArchive"
      category         = "Source"
      owner            = "AWS"
      provider         = "S3"
      version          = "1"
      output_artifacts = ["Source"]
      configuration    = { S3Bucket = aws_s3_bucket.app["delivery"].id, S3ObjectKey = var.source_object_key, PollForSourceChanges = "false" }

    }

  }
  stage {

    name = "BuildAndTest"
    action {

      name             = "Build"
      category         = "Build"
      owner            = "AWS"
      provider         = "CodeBuild"
      version          = "1"
      input_artifacts  = ["Source"]
      output_artifacts = ["Built"]
      configuration    = { ProjectName = aws_codebuild_project.build[0].name }

    }

  }
  stage {

    name = "Review"
    action {

      name          = "ApproveDeployment"
      category      = "Approval"
      owner         = "AWS"
      provider      = "Manual"
      version       = "1"
      configuration = { CustomData = "Review tests, image vulnerability scan and pipeline definition before deployment." }

    }

  }
  stage {

    name = "Deploy"
    action {

      name            = "DeployApplication"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["Built"]
      configuration   = { ProjectName = aws_codebuild_project.deploy[0].name }

    }

  }

}
