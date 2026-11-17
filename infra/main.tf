terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

variable "project_id" {
  type    = string
  default = "fiap-project-514922"
}

variable "environment" {
  type    = string
  default = "homolog"
}

# Recursos exclusivos do serviço oficina-execucao (Firestore database nomeado, SA exclusiva e subscriptions)
# Deploys e provisionamento permanecem bloqueados até a autorização na Etapa P7.
output "firestore_database_name" {
  value = "oficina-execucao-${var.environment}"
}
