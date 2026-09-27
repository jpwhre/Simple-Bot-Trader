# Security Policy

Thank you for helping keep Simple Bot Trader and its users safe.

## Supported versions

Security fixes are released in the latest release only. Please update to the
latest release before reporting an issue.

## Reporting a vulnerability — private

- **Preferred:** trigger the app's built-in diagnostic so it posts an
  encrypted, signed report to the private bug inbox
  (`jpwhre/Simple-Bot-Trader-Bugs`). Only the project's private key can read
  or alter it, and the signature prevents tampering.
- **Manual:** create an issue in `jpwhre/Simple-Bot-Trader-Bugs` with the
  encrypted report payload. Do **not** include tokens, keys, exchange API
  secrets, or personal data as plaintext in any issue or PR.

Please do not create public issues containing sensitive data. This repo is
public; anything you post here is visible to everyone.

## What to include in a report

- App version and build (Settings > About)
- Operating system
- Exchange, if relevant
- A short description of the failure and the encrypted diagnostic payload

## After you report

Reasonable-effort acknowledgment within 7 days; a fix is released with the
next version when it is within scope.
