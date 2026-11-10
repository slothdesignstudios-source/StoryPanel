# Private asset storage for one environment: an S3 bucket with encryption, versioning,
# lifecycle rules and a least-privilege IAM user for the API and worker.
#   terraform init && terraform apply -var-file=staging.tfvars
# NOT YET APPLIED: `terraform validate` could not be run where this was written.
terraform {
  required_version = ">= 1.6"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 5.0" } }
}

variable "environment" {
  type = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}
variable "region" { type = string }
variable "web_origins" {
  type        = list(string)
  description = "Origins allowed to load signed images (the web app)."
}
variable "export_retention_days" {
  type    = number
  default = 30
}

provider "aws" {
  region = var.region
  default_tags { tags = { app = "storypanel", environment = var.environment } }
}

resource "aws_s3_bucket" "assets" {
  bucket = "storypanel-assets-${var.environment}"
}
resource "aws_s3_bucket_public_access_block" "assets" {
  bucket                  = aws_s3_bucket.assets.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "assets" {
  bucket = aws_s3_bucket.assets.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id
  rule { apply_server_side_encryption_by_default { sse_algorithm = "AES256" } }
}
# Versioning is the restore path for accidental deletes; old versions expire so that a
# user's deletion request is fully honoured within the documented window.
resource "aws_s3_bucket_versioning" "assets" {
  bucket = aws_s3_bucket.assets.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_lifecycle_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id
  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
resource "aws_s3_bucket_cors_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id
  cors_rule {
    allowed_methods = ["GET"]
    allowed_origins = var.web_origins
    allowed_headers = ["*"]
    max_age_seconds = 600
  }
}
data "aws_iam_policy_document" "tls_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.assets.arn, "${aws_s3_bucket.assets.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}
resource "aws_s3_bucket_policy" "assets" {
  bucket = aws_s3_bucket.assets.id
  policy = data.aws_iam_policy_document.tls_only.json
}

# The application can read, write and delete objects in this one bucket. Nothing else.
resource "aws_iam_user" "app" {
  name = "storypanel-${var.environment}-app"
}
data "aws_iam_policy_document" "app" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.assets.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.assets.arn]
  }
}
resource "aws_iam_user_policy" "app" {
  name   = "assets"
  user   = aws_iam_user.app.name
  policy = data.aws_iam_policy_document.app.json
}

output "bucket" { value = aws_s3_bucket.assets.bucket }
output "app_user" { value = aws_iam_user.app.name }
