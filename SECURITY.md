# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability and do not
include secrets, private prompts, worker state, or exploit details in public
discussions.

Use GitHub private vulnerability reporting for this repository:

Private vulnerability reporting is enabled and verified before the initial public release.

1. Open the repository's **Security** tab.
2. Choose **Advisories** and **Report a vulnerability**.
3. Include affected versions, operating system, impact, reproduction steps,
   and any proposed mitigation.

Direct link: [privately report a vulnerability](https://github.com/holdonyb/codex-worker-dispatcher/security/advisories/new).

If private vulnerability reporting is temporarily unavailable, wait for it to
be restored rather than publishing sensitive details in an issue. Maintainers
will acknowledge a private report, investigate it, and coordinate disclosure
and remediation according to severity.

## Scope

Security-sensitive areas include process identity verification and
termination, task-state integrity, path authorization, skill installation and
uninstallation, credential or prompt disclosure, and JSON output boundaries.
The separately installed Codex CLI and its service are outside this project's
codebase; report their vulnerabilities through the channels provided by their
vendor.
