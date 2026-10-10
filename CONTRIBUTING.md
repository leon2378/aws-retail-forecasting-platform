# Project maintenance

OrderFlow is maintained by **Leon Lusbo** ([leon2378](https://github.com/leon2378)). Keep the repository name unchanged until its owner requests a rename.

Use the owner's configured Git identity for commits. Describe implemented behavior and validation without co-author trailers or tool branding. Apply the same convention to documentation, source comments and generated assets.

Before publishing a change:

1. Run the Python behavior and HTTP security tests, and the frontend syntax check.
2. Format and validate Terraform when infrastructure changes. A successful local validation is not a live deployment test.
3. Check that credentials, local configuration, databases, backups, Terraform state and generated outputs remain excluded from Git.
4. Label the synthetic catalog, local role demonstration and simulated commerce explicitly.
5. Check the inventory, idempotency, durable outbox and recovery invariants affected by the change.

The current work is local. Do not provision resources, enable schedules or incur AWS charges as part of routine development. A deployment needs a concrete reviewed plan, suitable permissions, an account and quota check, budget configuration and an explicit decision to proceed. Preserve private settings and ignored data when replacing obsolete application files.

Do not claim production readiness from code or unit tests alone. Record which deployment, load, alert delivery, restore and external integration checks have actually run, and keep unresolved boundaries visible.
