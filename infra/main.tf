terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.30.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" {
  type    = string
  default = "fiap-project-514922"
}

variable "region" {
  type    = string
  default = "us-east1"
}

variable "environment" {
  type    = string
  default = "homolog"
}

locals {
  env_canon = var.environment == "prod" ? "production" : var.environment
  k8s_ns    = local.env_canon == "production" ? "oficina-prod" : "oficina-homolog"
}

resource "google_firestore_database" "execucao_db" {
  project                           = var.project_id
  name                              = "oficina-execucao-${local.env_canon}"
  location_id                       = var.region
  type                              = "FIRESTORE_NATIVE"
  concurrency_mode                  = "PESSIMISTIC"
  app_engine_integration_mode       = "DISABLED"
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_DISABLED"
  delete_protection_state           = "DELETE_PROTECTION_ENABLED"
}

resource "google_service_account" "execucao_sa" {
  account_id   = "oficina-execucao-${local.env_canon}-sa"
  display_name = "Runtime SA para oficina-execucao (${local.env_canon})"
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.execucao_sa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${local.k8s_ns}/oficina-execucao-sa]"
}

resource "google_project_iam_member" "execucao_firestore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.execucao_sa.email}"
}

resource "google_project_iam_member" "execucao_pubsub_publisher" {
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.execucao_sa.email}"
}

resource "google_pubsub_subscription" "execucao_comandos_sub" {
  name  = "oficina-${local.env_canon}-execucao-comandos-sub"
  topic = "projects/${var.project_id}/topics/oficina-${local.env_canon}-execucao-comandos-v1"

  ack_deadline_seconds = 30
  dead_letter_policy {
    dead_letter_topic     = "projects/${var.project_id}/topics/oficina-${local.env_canon}-execucao-comandos-v1-dlq"
    max_delivery_attempts = 5
  }
}

resource "google_pubsub_subscription_iam_member" "execucao_sub_reader" {
  subscription = google_pubsub_subscription.execucao_comandos_sub.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.execucao_sa.email}"
}

output "firestore_database_name" {
  value = google_firestore_database.execucao_db.name
}

output "service_account_email" {
  value = google_service_account.execucao_sa.email
}

output "pubsub_subscription" {
  value = google_pubsub_subscription.execucao_comandos_sub.name
}
