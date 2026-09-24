# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | ✅ |

## Reporting a Vulnerability

If you discover a security vulnerability in MAC, please report it via GitHub's Private Vulnerability Reporting feature rather than opening a public issue:

➜ Go to **[Security tab](https://github.com/Pan-Chera/Multi-Agent-CAD/security)** → **"Report a vulnerability"**

This keeps your report private and visible only to the repository maintainers. If you prefer not to use the GitHub flow, you can also email the maintainers directly at the address listed in the project profile.

> **Note to maintainers**: the GitHub Private Vulnerability Reporting feature must be enabled in repo settings: **Settings → Security → Code security → Private vulnerability reporting → Enable**.

We will acknowledge your report as soon as possible and work to resolve confirmed vulnerabilities promptly.

## Scope

Security concerns relevant to this project include:

- Unsanitized user input in code generation pipelines that could lead to arbitrary code execution
- Exposure of API keys or credentials in generated output or logs
- Supply chain risks in the dependency chain (build123d, aider-chat, langgraph, etc.)

This project executes model-generated Python code in a child process. A child
process provides lifecycle isolation, **not a security sandbox**: generated code
runs with the same operating-system permissions as the user who started MAC.
Run the project only with trusted prompts and dependencies, preferably in a
disposable container or restricted account. This release strips
environment-variable secrets from the generated-Python subprocess to reduce
secret leakage; this is a mitigation, **not** a sandbox — filesystem, network,
and process-spawn access with the operator's UID remain unchanged. The Web UI
binds to localhost by default and must not be exposed to an untrusted network
without an independent authentication and sandboxing layer.

Reports of credential exposure, unsafe default network exposure, or unexpected
access beyond the documented process privileges are in scope.
