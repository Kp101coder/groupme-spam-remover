# groupme-spam-remover

Removes Spam from a GroupMe chat using DeepSeek Ollama.

## Security and API Key setup

This service exposes HTTP endpoints. The HTML console, `/status`, and the GroupMe webhook (`/kill-da-clanker`) remain public. All other routes require a valid API key supplied in the `X-API-Key` header. Keys can optionally be scoped to projects by sending `X-API-Project: <project>`; when a key declares one or more projects, requests must include a matching project header.

### Provisioning keys manually

Keys live in `api_keys.json` and are stored as Argon2 hashes along with optional metadata:

```json
{
  "api_keys": [
    {
      "name": "groupme-service",
      "hash": "argon2id$...",
      "projects": ["groupme"],
      "role": "service",
      "created_at": "2025-11-04T17:25:01.123456+00:00"
    }
  ]
}
```

Use the Admin UI (`/admin/ui`) to generate keys and copy the plaintext secret once. You can also call `POST /admin/generate-key` with JSON payload `{"name": "discord-bot", "projects": "discord", "role": "service"}` to create scoped keys via API.

Plain-text key lists (`api_keys.txt`) are no longer supported.

Important non-code security steps you must take:

- Run this behind HTTPS (use a reverse proxy like NGINX or a cloud provider's load balancer). Do not expose the app directly over plain HTTP on the public internet.
- Store API keys in a secrets manager (AWS Secrets Manager, Azure Key Vault, HashiCorp Vault) for production instead of files.
- Rotate keys periodically and remove unused keys.
- Limit network access with firewalls / security groups so only trusted IPs or services can reach the app.
- (Optional) Use mTLS or OAuth2 for stronger authentication if required.

When deploying, ensure the `access_token.txt` and model files are protected and only readable by the service user.

## Running at home (port forwarding)

If you want to run this on a home PC with port forwarding, follow these additional steps:

1. Reserve a static local IP for your machine (DHCP reservation on your router) or use the machine's local IP.
2. Forward ports 80 and 443 from your router to your machine's local IP.
3. Ensure your ISP allows inbound traffic on those ports (some ISPs block 80/443).
4. Use a reverse proxy (Caddy or Nginx) on your machine to obtain and manage Let's Encrypt certificates, forwarding to `127.0.0.1:7110`.

Example using Caddy on a home machine (Caddy will request certs from Let's Encrypt):

```text
example.com {
  reverse_proxy 127.0.0.1:7110
}
```

Then run the app bound to localhost:

```cmd
python -m uvicorn anti_clanker:app --host 127.0.0.1 --port 7110
```

If you cannot open ports or want a quick option for public HTTPS, use ngrok to create a secure tunnel and use the ngrok URL for callbacks.

## Admin endpoints

- POST `/admin/generate-key` (header `X-API-Admin-Key`): create a new API key (accepts optional `projects`, `role`, `notes` fields)
- GET `/admin/list-keys` (header `X-API-Admin-Key`): list key metadata without revealing secrets
- POST `/admin/revoke-key` (header `X-API-Admin-Key`, body `{ "name": "..." }`): revoke key by name
- POST `/admin/models/list` (header `X-API-Admin-Key`): list ollama models
- POST `/admin/models/pull` (header `X-API-Admin-Key`, body `{model}`): pull model
- POST `/admin/models/delete` (header `X-API-Admin-Key`, body `{model}`): delete model
- POST `/admin/models/switch` (header `X-API-Admin-Key`, body `{model}`): switch active model
- POST `/admin/git-pull` (header `X-API-Admin-Key`): run `git fetch` + `git pull` and return output

Note: Admin endpoints are protected by the admin key in `admin_key.txt` (or `admin_key.json`).
