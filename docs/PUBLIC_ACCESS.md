# Public access (friends outside a private network)

Multi-user **already works** on a private VPN (e.g. Tailscale) with login and isolated `data/users/<id>/`.

To let people who are **not** on that network use the tutor, you need **HTTPS** and must **never expose Ollama** (or other internal ports) to the internet.

## Recommended options (safest first)

### 1. Tailscale Funnel (if you already use Tailscale)

Public HTTPS URL managed by Tailscale; the machine stays without open router ports.

```bash
# Example — use your gateway port (default in examples: 8790)
sudo tailscale funnel --bg 8790
tailscale funnel status   # prints https://<machine>.<tailnet>.ts.net/
```

Docs: https://tailscale.com/kb/1311/tailscale-funnel

Server environment (systemd override example: `deploy/public-override.conf.example`):

```bash
Environment=TUTOR_PUBLIC=1
Environment=TUTOR_COOKIE_SECURE=1
Environment=TUTOR_TRUST_PROXY=1
Environment=TUTOR_SESSION_DAYS=14
```

### 2. Cloudflare Tunnel (no open router ports)

Run `cloudflared` on the host and map a hostname to `http://127.0.0.1:8790`.  
Bind the app to localhost only: `--host 127.0.0.1 --port 8790`.

Same env vars as above.

### 3. Public IP + Caddy (Let’s Encrypt)

Only if you control a domain and firewall:

- Allow **443/tcp** only (not the app port, not Ollama).
- Caddy `reverse_proxy` → `127.0.0.1:8790`
- Uvicorn: `--host 127.0.0.1`

## What stays private

| Service | Public? |
|---------|---------|
| Tutor gateway (via HTTPS tunnel) | Yes (login required) |
| Ollama (default port 11434) | **Never** |
| SSH | VPN / keys only |
| Other local agents | Not required for the tutor |

## Accounts

- **No self-registration** (by design).
- Admin creates accounts:

```bash
cd /path/to/tutor && source .venv/bin/activate
python tools/create_user.py alice   # prompts for a strong password
```

- Each user: `data/users/<id>/learner_state.json` and `web_conversation.json`.

## Checklist before inviting people

1. [ ] HTTPS works without certificate warnings  
2. [ ] Login works with `TUTOR_COOKIE_SECURE=1`  
3. [ ] Ollama is not reachable from the internet  
4. [ ] Strong unique passwords per user  
5. [ ] Login rate limit active (built-in)  
6. [ ] Optional: shorter sessions (`TUTOR_SESSION_DAYS=14`)

## Threat model (honest)

Friend-scale, not a bank:

- Helps against: casual scanning, password guessing, session theft on plain HTTP  
- Does not replace: full WAF, SSO, GPU quotas, abuse monitoring  

End-user guide to send learners: [HOW_TO_USE.md](HOW_TO_USE.md).
