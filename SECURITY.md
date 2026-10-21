# Security policy

LITEfile handles court filings and sensitive personal information. Please report
suspected security vulnerabilities privately so maintainers can investigate and
coordinate a fix.

## Supported versions

Security fixes target the latest code on the `main` branch. Older commits,
release tags, and forks do not have a guaranteed security maintenance window.
Deployment operators should keep their installations and dependencies up to date.
Please report suspected vulnerabilities even if you found them in an older version.

## Report a vulnerability

Email [litlab@suffolk.edu](mailto:litlab@suffolk.edu) with the subject
`LITEfile security report`. This is the Suffolk LIT Lab contact used in the
project's support documentation.

Do not disclose vulnerabilities in public GitHub issues, pull requests, or
discussions. For ordinary bugs without a security impact, use the repository's
issue tracker.

Include as much of the following as you can:

- A description of the vulnerability and its potential impact.
- The affected component, commit or version, and deployment configuration.
- Steps to reproduce, preferably using synthetic data in a local environment.
- A minimal proof of concept, or redacted logs and screenshots.
- Any authentication or access needed to reproduce the issue.
- A way to contact you for follow-up and your preference for public credit.

Do not send passwords, API keys, session tokens, real court documents, or personal
information. If sensitive evidence is necessary, describe what you have and ask
the maintainers to arrange an appropriate transfer method first.

If the issue affects a specific hosted installation, also notify its operator
privately so they can address immediate exposure.

## Response and disclosure

Maintainers will review reports, request additional details when needed, and
coordinate remediation and public disclosure with the reporter. Response and fix
times depend on severity and maintainer availability; this policy does not
guarantee a response deadline. If you have not received a response within seven
business days, please follow up on the same email thread.

Please give maintainers a reasonable opportunity to investigate and provide a fix
before publishing technical details. Agree on disclosure timing and attribution
with the maintainers. This policy does not promise a bounty or other compensation.

## Safe testing

Use a local or explicitly authorized test environment with synthetic documents
and accounts. Do not access other users' information, disrupt services, or submit
test filings to live courts. Obtain the operator's permission before testing a
hosted installation or third-party integration.

If you encounter sensitive information unexpectedly, stop testing and report the
issue privately. Avoid copying or retaining that information beyond what is
necessary to describe the issue.
