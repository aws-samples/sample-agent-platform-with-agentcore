output "namespace" {
  value = local.namespace
}

output "release_names" {
  description = "Helm releases this module owns, as namespace/name (the helm provider's import id)."
  value       = ["${helm_release.edge_sg.namespace}/${helm_release.edge_sg.name}", "${helm_release.edge.namespace}/${helm_release.edge.name}"]
}
