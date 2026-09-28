"""Command implementations for retained script entries."""

COMMAND_MODULES = {
    "assemble_artifacts": "artifacts",
    "await_workflow": "awaiting",
    "build_style_artifacts": "style",
    "collect_project_rules": "project_rules",
    "detect_prior_review": "prior_review",
    "diff_numstat": "numstat",
    "emit_style_context": "style_hook",
    "ensure_output_dir": "output_dir",
    "generate_contract_requirements": "contract_gen",
    "materialize_artifacts": "materialize",
    "post_review": "delivery.post",
    "render_fix_tasks": "fix_tasks",
    "report_patches": "patches",
    "resolve_config": "config",
    "resolve_pr_identity": "pr_identity",
    "stale_truncate": "stale",
    "sync_agent_rules": "agent_rules",
    "verify_findings": "verify.decide",
    "write_shared_context": "shared_context",
}
