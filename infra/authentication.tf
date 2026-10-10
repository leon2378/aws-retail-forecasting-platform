locals {
  auth_callback_url = "https://${aws_cloudfront_distribution.app.domain_name}/"
  auth_issuer       = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.app.id}"
  auth_domain       = "https://${aws_cognito_user_pool_domain.app.domain}.auth.${var.region}.amazoncognito.com"
  auth_scopes       = ["openid", "profile", "orderflow/read", "orderflow/write"]
}
resource "aws_cognito_user_pool" "app" {
  name                     = "${local.prefix}-users"
  user_pool_tier           = "LITE"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OPTIONAL"
  admin_create_user_config {
    allow_admin_create_user_only = true
  }
  username_configuration {
    case_sensitive = false
  }
  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }
  software_token_mfa_configuration {
    enabled = true
  }
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
  user_attribute_update_settings {
    attributes_require_verification_before_update = ["email"]
  }
}
resource "aws_cognito_resource_server" "app" {
  identifier   = "orderflow"
  name         = "OrderFlow API"
  user_pool_id = aws_cognito_user_pool.app.id
  scope {
    scope_name        = "read"
    scope_description = "Read orders, inventory and operations"
  }
  scope {
    scope_name        = "write"
    scope_description = "Request operations subject to server-side role checks"
  }
}
resource "aws_cognito_user_pool_client" "web" {
  name                                 = "${local.prefix}-web"
  user_pool_id                         = aws_cognito_user_pool.app.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = local.auth_scopes
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = distinct(concat([local.auth_callback_url], var.auth_additional_callback_urls))
  logout_urls                          = distinct(concat([local.auth_callback_url], var.auth_additional_logout_urls))
  default_redirect_uri                 = local.auth_callback_url
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  read_attributes                      = ["email", "email_verified"]
  write_attributes                     = ["email"]
  access_token_validity                = 15
  id_token_validity                    = 15
  refresh_token_validity               = 1
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
  depends_on = [aws_cognito_resource_server.app]
}
resource "aws_cognito_user_pool_domain" "app" {
  domain                = "${local.prefix}-${substr(sha256(aws_cognito_user_pool.app.id), 0, 12)}"
  user_pool_id          = aws_cognito_user_pool.app.id
  managed_login_version = 1
}
resource "aws_cognito_user_group" "roles" {
  for_each = {
    viewer = 30, operator = 20, admin = 10
  }
  name         = each.key
  user_pool_id = aws_cognito_user_pool.app.id
  description  = "OrderFlow ${each.key} role; application authorization enforces capabilities"
  precedence   = each.value
}
resource "aws_apigatewayv2_authorizer" "app" {
  api_id           = aws_apigatewayv2_api.api.id
  name             = "${local.prefix}-cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.web.id]
    issuer   = local.auth_issuer
  }
}
resource "aws_apigatewayv2_route" "public" {
  for_each           = toset(["GET /api/health", "GET /api/auth/config"])
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = "NONE"
}
resource "aws_apigatewayv2_route" "write" {
  for_each             = toset(["POST /api/orders", "POST /api/operations/retry/{id}", "POST /api/orders/{id}/cancel", "POST /api/inventory/{sku}/adjust", "POST /api/operations/drill"])
  api_id               = aws_apigatewayv2_api.api.id
  route_key            = each.value
  target               = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type   = "JWT"
  authorizer_id        = aws_apigatewayv2_authorizer.app.id
  authorization_scopes = ["orderflow/write"]
}
