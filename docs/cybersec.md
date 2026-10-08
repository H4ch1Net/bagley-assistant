# Cybersec mode

Cybersec mode turns Bagley into a defensive security analyst for systems you own or are
authorised to test: your own machines, your home or lab network, and CTF challenges. Pick it
from the mode switcher (code `SEC`). It loads the `security` tool group (these analyst tools)
and the `system` group (this computer's status and the watchdog's system checks), and it tells
the model to think like an incident responder, cite CVEs and prefer read-only checks.

Everything here is defensive: the tools look at data you paste, read public information, or
probe hosts you have told Bagley are yours. Nothing is ever executed, and the two tools that
touch the network refuse targets you have not shown you own.

## Tools

| Tool | What it does | Needs approval |
|------|--------------|----------------|
| `cve_lookup` | Looks up a CVE (dates, description, CVSS score/severity/vector, CWEs, affected products, references, CISA KEV). | no |
| `decode` | Decodes pasted data without running it. | no |
| `identify_hash` | Guesses the algorithm behind a hash, with hashcat mode numbers. | no |
| `explain_port` | Explains a port, its typical risks and how to harden it. | no |
| `scan_ports` | TCP connect check of a host on your own network. | yes |
| `tls_check` | Reads a host's TLS certificate and reports its health. | no |
| `analyze_log` | Scans pasted log text for IPs, failed logins, scanners and suspicious commands. | no |

### `cve_lookup`

Give it a CVE id (`CVE-2024-3094`). It queries the [NVD API 2.0](https://nvd.nist.gov/), and
if NVD does not answer it falls back to the [CIRCL CVE service](https://cve.circl.lu/). Results
are cached for 24 hours in a `cve_cache` table, so repeated questions about the same CVE are
instant and offline. The answer says where it came from and when it was retrieved; always check
the vendor advisory for anything you act on. This is the only cybersec tool that reaches the
internet, so a scheduled task that has already read a web page will not be allowed to run it
(see the privacy note below).

### `decode`

Decodes suspicious strings without ever executing them. With `encoding="auto"` (the default) it
peels up to four layers and tells you each step it took, for example base64 of a gzip of JSON.
It understands base64 and base64url, hex, URL and HTML-entity escaping, ROT13, gzip or zlib
wrapped in base64, quoted-printable, and JWTs. For a JWT it decodes the header and payload only;
it never verifies the signature, and it flags `alg: none` tokens and expired or not-yet-valid
claims. Output is capped.

### `identify_hash`

Names the likely algorithms for a hash from its length, character set and any prefix (`$2b$`,
`$6$`, `$argon2id$`, `$y$`, ...), with the matching hashcat `-m` numbers. Lengths overlap (MD5
and NTLM are both 32 hex characters), so it returns ranked candidates rather than a single
answer.

### `explain_port`

Looks a port up in a bundled table of about 120 common services. For each it gives the usual
service, what to watch for and how to harden it. Unknown ports get a note about which range
they fall in and how to find what is listening.

### `scan_ports` (asks first)

An asyncio TCP connect check: it reports which ports accept a connection, each annotated with
`explain_port`. It only runs against hosts that resolve to a **private (RFC 1918), loopback,
link-local or Tailscale CGNAT (100.64.0.0/10)** address, or a host you have recorded in the
work asset inventory. Public addresses are refused, because probing them needs the owner's
authorisation. `ports` accepts `top100` (the default), `all-known`, or a list like
`22,80,8000-8100` (at most 1024 ports). It is a connect check only: an open port means the
port accepted a connection, not that the service is vulnerable.

### `tls_check`

Reads a host's TLS certificate and reports the subject, issuer, SANs, expiry and days left, the
protocol and cipher, and whether the certificate matches the host name. Public hosts are fine;
a private address is only allowed when it is in your asset inventory. It changes nothing.

### `analyze_log`

Scans pasted log text (`kind` is `auto`, `auth`, `web` or `generic`) for the things worth your
attention: source IPs, targeted user names, failed-authentication bursts, HTTP status-code
spikes, scanner user agents, and command patterns often seen in attacks (`curl | sh`,
`base64 -d`, `nc -e`, `/dev/tcp`, path traversal, and so on). It returns counts and the top
entries plus a few hints, for the model to explain. It reads the log; it never runs anything
in it.

## Privacy and unattended runs

`scan_ports` needs your approval, so a scheduled task can never run it. `cve_lookup` brings text
from the internet into the conversation, so once a scheduled task has read any web content the
policy stops it reading your private data or opening new addresses. The decode, hash, port and
log tools work only on text you give them and never leave the machine.

## Requirements

No setup and no API keys. `cve_lookup` and `tls_check` need outbound HTTPS; everything else
works fully offline. `scan_ports` and `tls_check` make direct TCP connections to the target.
