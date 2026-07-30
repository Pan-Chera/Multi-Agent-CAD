# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | ✅ |

## Reporting a Vulnerability

If you discover a security vulnerability in MAC, please report it privately via email to the project maintainers rather than opening a public issue.

We will acknowledge your report as soon as possible and work to resolve confirmed vulnerabilities promptly.

## Scope

Security concerns relevant to this project include:

- Unsanitized user input in code generation pipelines that could lead to arbitrary code execution
- Exposure of API keys or credentials in generated output or logs
- Supply chain risks in the dependency chain (build123d, aider-chat, langgraph, etc.)

This project is a research-oriented CAD code-generation system. It executes model-generated Python code in a subprocess — that is by design and is not itself a vulnerability. However, if you find a way that a crafted input could escape the subprocess sandbox or access resources beyond the intended scope, that qualifies as a reportable issue.
