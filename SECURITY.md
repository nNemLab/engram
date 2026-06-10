# Security Policy

Engram is a security-adjacent MCP server: it performs outbound HTTP fetching
(polled sources, URL/arXiv ingestion, self-hosted web search) and reads/writes
files within a configured vault. We take SSRF protection and path containment
seriously and welcome reports of any weakness in these areas.

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

Only the latest 0.1.x release line receives security fixes.

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Use GitHub's private vulnerability reporting:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability** (under *Security Advisories*).
3. Provide a clear description, affected version(s), and reproduction steps.

This routes your report privately to the maintainer so the issue can be
triaged and fixed before public disclosure.

## Response expectations

Engram is a solo-maintained, open-source project. Responses are best-effort:
we aim to acknowledge a report within about a week and to coordinate a fix and
disclosure timeline with you from there. Thank you for reporting responsibly.
