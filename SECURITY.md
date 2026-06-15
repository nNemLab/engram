# Security Policy

Engram is a security-adjacent MCP server: it performs outbound HTTP fetching
(polled sources, URL/arXiv ingestion, self-hosted web search) and reads/writes
files within a configured vault. SSRF protection and path containment are
priority areas, and reports of any weakness there are especially welcome.

## Supported versions

Engram is pre-1.0 and released continuously. Security fixes target the
**latest released 0.x version**. For an issue on an older release, please
confirm it reproduces on the latest release first.

## Reporting a vulnerability

**Please do not open a public issue, discussion, or pull request for security
vulnerabilities.**

Report privately through GitHub:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Submit the private advisory form.

This routes the report privately to the maintainer for triage and a fix before
public disclosure.

### What to include

Keep it minimal — only the first two are required:

- **Required**
  - **Title** — one-line summary.
  - **Description** — what the issue is and why it is a security concern.
- **Helpful (optional)**
  - Affected version or commit.
  - Reproduction steps or proof of concept.
  - Impact — what an attacker could do.
  - Suggested fix or mitigation.

## Response expectations

Engram is a solo-maintained, open-source project. Responses are best-effort:
acknowledgement within about 7 days, then validation, fix, and disclosure
timing coordinated with you.

## Coordinated disclosure

Please keep the report private until a fix is released and disclosure is
coordinated.
