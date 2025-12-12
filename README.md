# GroupMe Spam Remover

A FastAPI service that watches GroupMe conversations and uses an Ollama-hosted LLM to flag and react to spam in real time. The project also exposes lightweight web consoles for operators and testers.

## Feature Highlights

- GroupMe webhook (`/kill-da-clanker`) that classifies incoming messages and applies strike/banning rules.
- `/ai` endpoint for direct access to the underlying prompt logic, plus a user-facing playground UI.
- Admin console for managing API credentials and the Ollama model catalog.
- Project-scoped API keys so different automations can be isolated without running separate deployments.

## Quick Start

1. **Install dependencies.** Use Python 3.11+ and install the required packages:
   ```cmd
   pip install fastapi uvicorn[standard] argon2-cffi ollama requests python-dotenv
   ```
   Adjust if you already maintain a requirements file for your environment.

2. **Generate an admin key.** Run:
   ```cmd
   python sec/generate_admin_key.py
   ```
   Copy the printed secret; you will not see it again. The hashed form is stored in `sec/admin.key`.

3. **Start the service.**
   ```cmd
   uvicorn vaayuronics:app --host 0.0.0.0 --port 7110 --reload
   ```
   The webhook and UIs are now available under `http://localhost:7110/`.

4. **Configure the GroupMe bot.** Point your GroupMe callback URL at `/kill-da-clanker` on a publicly reachable address (ngrok, reverse proxy, etc.).

## Authentication Model

### Admin key

- Stored hashed in `sec/admin.key` and supplied via the `X-API-Admin-Key` header (or `admin_key` query parameter).
- Required for all `/admin/*` routes and the Admin UI login.
- Only the hashed value is kept on disk; regenerate with `python sec/generate_admin_key.py` if the secret is lost or compromised.

### API keys (user/service/admin roles)

- Persisted in `sec/users.key` as Argon2 hashes.
- Sent with `X-API-Key` (or `api_key` query parameter) on protected endpoints such as `/ai`.
- Roles are metadata to help you differentiate callers:
  - `user`: interactive clients, default permissions.
  - `service`: background jobs or other automations.
  - `admin`: still verified like any other API key, but marked clearly in listings.
- The plaintext secret is only shown once when a key is generated through the admin console or API.

### Project scoping

- Keys can include a `projects` array. When present, every request made with that key must send `X-API-Project` with one of the allowed values.
- Use this to isolate different integrations (e.g., `groupme`, `discord`, `internal-tool`).
- Omit `projects` or include `"*"` for wildcard access across all projects.

## Web Consoles

- `/admin/ui`: Manage API keys, view metadata, and run Ollama model operations (pull, delete, switch). Requires the admin key to log in.
- `/user_ui`: Playground for the `/ai` endpoint. Users can supply the main text plus optional system message, training snippets, conversation history, and the `think` flag. The UI now shows both the request payload and response JSON for easier API onboarding.

## HTTP Endpoints

- `POST /ai`: Invoke the moderation prompt manually (requires `X-API-Key`).
- `POST /auth/login`: Validate an API key and return metadata (role, allowed projects).
- `/admin/*`: Key management, model management, and git utilities — all require `X-API-Admin-Key`.
- `GET /status`: Lightweight health probe.

## Security Notes

- Always run behind HTTPS when exposed publicly. Terminate TLS at a reverse proxy (Caddy, Nginx, cloud load balancer) and forward to the FastAPI app.
- Keep `sec/` contents and any Ollama model files restricted to the service account.
- Rotate API keys regularly and remove stale entries using the admin tools.
