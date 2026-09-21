# Security and private-data policy

Do not commit reusable credentials or private runtime state.

Never commit passwords, auth tokens, cookies, browser/session profiles, API keys, private recordings, generated mosaics, local logs containing sensitive data, or machine-specific secret configuration.

Use sanitized templates and environment/local configuration outside Git. If a credential is ever committed, rotate it immediately before cleaning repository history.
