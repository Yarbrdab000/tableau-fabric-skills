# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability in this repository — in the skill scripts, the
vendored MCP deploy bundle, or the packaging — please report it privately rather than opening
a public issue.

- Open a GitHub **security advisory** for this repository (**Security → Advisories → Report a
  vulnerability**), or
- Contact the maintainer through their GitHub profile ([@Yarbrdab000](https://github.com/Yarbrdab000)).

Please include the affected file or component, a description of the issue, and steps to
reproduce. We aim to acknowledge reports within a few business days.

## Supported versions

This collection is under active development. Security fixes are applied to the latest released
version of the packaging manifests; older versions are not maintained.

## Handling secrets

This collection is built around strict secret discipline. The skills authenticate to Tableau
with Personal Access Tokens or Connected App (Direct Trust) secrets, and the MCP landing zone
uses a shared sidecar API key. **None of these belong in source control.**

- Never commit a real `.env`, a Tableau workbook or extract
  (`*.tds` / `*.twb` / `*.twbx` / `*.tdsx` / `*.hyper`), a PAT, a Connected App secret, or a
  sidecar API key. The repository `.gitignore` blocks these by pattern; the committed
  `.env.example` files are templates only and must never hold real values.
- A [`.gitleaks.toml`](.gitleaks.toml) ruleset is included so contributors can scan locally
  (for example `gitleaks detect`) before pushing.
- When demonstrating a deployment, use placeholder secrets and rotate or scrub them afterward.
  Rotate the MCP sidecar key via the `sidecar-api-key` Container App secret.

For the broader security posture, see [`CLEANROOM.md`](CLEANROOM.md) and
[`skills/tableau-mcp-landing-zone/resources/security-operations.md`](skills/tableau-mcp-landing-zone/resources/security-operations.md).
