variables {
  private_subnet_ids     = ["subnet-0123456789abcdef0"]
  node_security_group_id = "sg-0123456789abcdef0"
  node_instance_profile  = "offline-test-profile"
  approved_ami_id        = "ami-0123456789abcdef0"
}

run "isolated_burst_proposal" {
  command = plan
  assert {
    condition     = output.budget.nominal_total == 2 && output.budget.fixed_nodes == 1 && !output.budget.safe_to_deploy
    error_message = "The proposal must preserve the baseline and never claim hard budget enforcement."
  }
  assert {
    condition     = output.node_pool.spec.limits.cpu == "2" && output.node_pool.spec.limits.memory == "16Gi"
    error_message = "Nominal burst resources must fit exactly one selected r5a.large."
  }
  assert {
    condition     = output.node_pool.spec.template.spec.taints[0].value == "serving" && output.node_pool.spec.template.metadata.labels.workload == "system"
    error_message = "Inference must match the pool while non-tolerating training pods remain excluded."
  }
  assert {
    condition     = output.node_pool.spec.disruption.consolidateAfter == "5m" && output.node_class.spec.instanceProfile == var.node_instance_profile
    error_message = "Consolidation must be delayed and worker identity must be explicit."
  }
}
