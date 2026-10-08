# These plans use only Terraform's mock provider. They do not read AWS, create
# users, deploy resources or write real state. The fixture is hashed, not uploaded.
mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_cloudfront_distribution" {
    defaults = { domain_name = "test.example.cloudfront.net" }
  }
  mock_resource "aws_cognito_user_pool" {
    defaults = { id = "ap-southeast-2_fixture" }
  }
  mock_resource "aws_cognito_user_pool_client" {
    defaults = { id = "fixturepublicclient" }
  }
  mock_resource "aws_apigatewayv2_authorizer" {
    defaults = { id = "fixtureauthorizer" }
  }
}

variables {
  lambda_zip               = "tests/authentication.tftest.hcl"
  schedule_enabled         = false
  enable_delivery          = false
  enable_clearml           = false
  pipeline_definition_file = ""
}

run "protected_api_defaults" {
  command = plan

  assert {
    condition = (
      aws_apigatewayv2_route.api.authorization_type == "JWT" &&
      aws_apigatewayv2_route.api.authorizer_id == aws_apigatewayv2_authorizer.app.id &&
      aws_apigatewayv2_route.api.authorization_scopes == toset(["retail/read"])
    )
    error_message = "Every catch-all application route must require the configured Cognito JWT and an access-token scope."
  }
  assert {
    condition = (
      toset([for route in aws_apigatewayv2_route.public : route.route_key]) ==
      toset(["GET /api/health", "GET /api/auth/config"]) &&
      alltrue([for route in aws_apigatewayv2_route.public : route.authorization_type == "NONE"])
    )
    error_message = "Only liveness and public sign-in settings may bypass sign-in. Session and data routes must remain protected."
  }
  assert {
    condition = (
      aws_lambda_function.api.environment[0].variables["AUTH_ENABLED"] == "true" &&
      aws_lambda_function.api.environment[0].variables["AUTH_ISSUER"] == aws_apigatewayv2_authorizer.app.jwt_configuration[0].issuer &&
      length(aws_apigatewayv2_authorizer.app.jwt_configuration[0].audience) == 1 &&
      contains(aws_apigatewayv2_authorizer.app.jwt_configuration[0].audience, aws_cognito_user_pool_client.web.id)
    )
    error_message = "Gateway and application must enforce the same configured issuer and client."
  }
  assert {
    condition = (
      aws_cognito_user_pool.app.admin_create_user_config[0].allow_admin_create_user_only &&
      aws_cognito_user_pool.app.password_policy[0].minimum_length >= 12 &&
      aws_cognito_user_pool.app.software_token_mfa_configuration[0].enabled &&
      aws_cognito_user_pool.app.mfa_configuration == "OPTIONAL" &&
      aws_cognito_user_pool.app.user_pool_tier == "LITE" &&
      aws_cognito_user_pool_domain.app.managed_login_version == 1
    )
    error_message = "The pilot must remain invite-only with the password/TOTP safeguards and the explicitly selected Lite hosted sign-in."
  }
  assert {
    condition = (
      !aws_cognito_user_pool_client.web.generate_secret &&
      aws_cognito_user_pool_client.web.allowed_oauth_flows == toset(["code"]) &&
      aws_cognito_user_pool_client.web.access_token_validity == 15 &&
      aws_cognito_user_pool_client.web.token_validity_units[0].access_token == "minutes" &&
      aws_cognito_user_pool_client.web.enable_token_revocation &&
      aws_cognito_user_pool_client.web.callback_urls == toset(["https://test.example.cloudfront.net/"]) &&
      aws_cognito_user_pool_client.web.logout_urls == toset(["https://test.example.cloudfront.net/"])
    )
    error_message = "The browser client must have no secret, use code flow, short access tokens and the registered CloudFront root. PKCE is enforced by the browser flow."
  }
  assert {
    condition = (
      aws_cognito_user_group.viewer.name == "viewer" &&
      aws_cognito_user_group.planner.name == "planner" &&
      !contains(aws_cognito_user_pool_client.web.allowed_oauth_scopes, "aws.cognito.signin.user.admin")
    )
    error_message = "Role groups must exist without granting Cognito self-service administration scope to the browser. Group membership requires an IAM-authorized operator."
  }
  assert {
    condition     = !strcontains(local.auth_domain, "123456789012")
    error_message = "The public sign-in domain must use the pool hash rather than exposing the account ID."
  }
}

run "explicit_preview_redirects" {
  command = plan

  variables {
    auth_additional_callback_urls = ["http://127.0.0.1:8000/", "https://preview.example.com/"]
    auth_additional_logout_urls   = ["http://localhost:8000/", "http://[::1]:8000/"]
  }

  assert {
    condition = (
      contains(aws_cognito_user_pool_client.web.callback_urls, "http://127.0.0.1:8000/") &&
      contains(aws_cognito_user_pool_client.web.callback_urls, "https://test.example.cloudfront.net/") &&
      contains(aws_cognito_user_pool_client.web.logout_urls, "http://[::1]:8000/") &&
      aws_cognito_user_pool_client.web.default_redirect_uri == "https://test.example.cloudfront.net/"
    )
    error_message = "Additional explicitly approved previews must not replace the cloud callback default."
  }
}

run "remote_http_redirect_is_rejected" {
  command = plan
  variables {
    auth_additional_callback_urls = ["http://preview.example.com/"]
  }
  expect_failures = [var.auth_additional_callback_urls]
}

run "fragment_logout_redirect_is_rejected" {
  command = plan
  variables {
    auth_additional_logout_urls = ["https://preview.example.com/#operations"]
  }
  expect_failures = [var.auth_additional_logout_urls]
}
