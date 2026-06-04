# GCS bucket for Pedal Hidrográfico announcements.
#
# Serves the PUBLIC image URLs that Instagram and Whapi fetch
# (https://storage.googleapis.com/<bucket>/posts/<file>) and stores the dataset
# Turtle (data_manual.ttl). Matches app/storage.py: uniform bucket-level access
# + public reads, objects under posts/.
#
#   cd infra
#   cp terraform.tfvars.example terraform.tfvars   # edit bucket_name if taken
#   terraform init
#   terraform apply

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" {
  type    = string
  default = "pedal-hidrografico"
}

variable "region" {
  type    = string
  default = "southamerica-east1" # São Paulo
}

variable "location" {
  type        = string
  default     = "southamerica-east1"
  description = "Bucket location. Single region keeps egress cheap and latency low for BR."
}

variable "bucket_name" {
  type        = string
  default     = "pedal-hidrografico-anuncios"
  description = "Globally unique. Change if the default is taken."
}

resource "google_storage_bucket" "anuncios" {
  name     = var.bucket_name
  project  = var.project_id
  location = var.location

  # ACLs off; access via IAM only — what app/storage.py assumes.
  uniform_bucket_level_access = true

  # "inherited" (not "enforced") so the allUsers read binding below is allowed.
  public_access_prevention = "inherited"

  # Keep history of data_manual.ttl (and overwritten images). At ~20 posts/month
  # the storage cost is negligible; gives you an undo for the dataset.
  versioning {
    enabled = true
  }

  # Don't let `terraform destroy` wipe a bucket that has objects.
  force_destroy = false
}

# Public read for everything in the bucket. Note: this makes data_manual.ttl
# world-readable too (it's just announcement text). If you'd rather keep the
# TTL private, move it to a separate non-public bucket and point DATA_TTL there.
resource "google_storage_bucket_iam_member" "public_read" {
  bucket = google_storage_bucket.anuncios.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

output "bucket_name" {
  value = google_storage_bucket.anuncios.name
}

output "public_base_url" {
  value = "https://storage.googleapis.com/${google_storage_bucket.anuncios.name}"
}
