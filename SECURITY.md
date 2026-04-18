# Security Policy

## Supported Versions

Only the latest release receives security fixes.

|Version|Supported|
|-|-|
|Latest|✅ Yes|
|Older|❌ No|

## Reporting a Vulnerability

If you discover a security issue (e.g. malicious ASCII/MDF/BLF/DBC file causing
arbitrary code execution), please **do not open a public GitHub issue**.

Instead, report it privately via GitHub's
[Security Advisories](https://github.com/dinacran/CANScope/security/advisories/new)
feature, or email the maintainer directly.

Please include:

* A description of the vulnerability
* Steps to reproduce (a minimal test case if possible — no proprietary data)
* The potential impact

You can expect an acknowledgement within 72 hours.

## Scope

This tool reads BLF and DBC files from the local filesystem only.
It makes no network connections. The primary attack surface is
**malformed or malicious input files**.

