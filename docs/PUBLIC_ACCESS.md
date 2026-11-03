# Public access (friends outside a private network)

Multi-user already works on a private VPN (e.g. Tailscale) with login and isolated `data/users/<id>/`.

To let people who are **not** on that network use the tutor, you need **HTTPS** and must **never expose Ollama** (or other internal ports) to the internet.

This document is **generic**. Do not commit real hostnames, tailnet names, funnel URLs, or account lists to a public repository.

## Recommended options (safest first)

### 1. Tailscale Funnel (if you already use Tailscale)

Public HTTPS URL managed by Tailscale; the machine stays without open router ports.

```bash
# Example — use your gateway port
sudo tailscale funnel --bg 8790
tailscale funnel status
```

See current Tailscale Funnel documentation. Point learners at the HTTPS URL Funnel prints; keep Ollama bound to localhost only.

### 2. Cloudflare Tunnel or similar

Map a hostname to `http://127.0.0.1:8790`. Bind the app to localhost only.

### 3. Public IP + reverse proxy (Let’s Encrypt)

Only if you control a domain and firewall:

- Allow **443/tcp** only (not the app port, not Ollama)
- Reverse proxy → `127.0.0.1:8790`
- Gateway: `--host 127.0.0.1`

## What stays private

| Service | Public? |
|---------|---------|
| Tutor gateway (via HTTPS tunnel) | Yes (login required) |
| Ollama (default port 11434) | **Never** |
| SSH | VPN / keys only |

## Accounts

- No self-registration (by design).
- Admin creates accounts, e.g. `python tools/create_user.py <name>` (strong unique passwords).
- Each user: `data/users/<id>/` (gitignored).

## Checklist before inviting people

1. HTTPS works without certificate warnings
2. Login works with secure cookies when using HTTPS
3. Ollama is not reachable from the internet
4. Strong unique passwords per user
5. Login rate limit active

## Threat model (honest)

Friend-scale, not a bank. Publishing this *generic* guide does not expose your home network. Publishing secrets, real Funnel hostnames, or open Ollama ports does.
