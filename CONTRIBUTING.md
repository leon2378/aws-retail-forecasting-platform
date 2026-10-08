# Project maintenance

This portfolio project is maintained by **Leon Lusbo** ([leon2378](https://github.com/leon2378)).

Use the owner's configured Git identity for repository commits. Commit messages should describe the implemented behavior and validation, without co-author trailers or tool branding. Apply the same convention to documentation, source comments and generated assets.

Before publishing changes:

1. Run the Python behavior tests and frontend syntax check.
2. Format and validate Terraform when infrastructure changes.
3. Check that datasets, credentials, local configuration, Terraform state and build outputs are excluded.
4. Keep synthetic data, approximate uncertainty, and simulated savings explicitly labelled.

AWS deployment requires a reviewed plan, a provisioning role with suitable permissions, and confirmed regional quotas. Keep schedules and optional experiment infrastructure disabled until an end-to-end replay has been validated.
