variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "siyona"
}

variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "log_retention_days" {
  description = "How long service logs are kept. Call transcripts have their own retention policy."
  type        = number
  default     = 30
}
