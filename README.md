# Impact Story Builder

Schema-driven interview prototype for drafting SEI impact stories.

## Current pilot shape

- Frontend: static HTML/CSS/JS served by the Python app
- Backend: `server.py`
- Auth: `manual_invite` demo mode or `local_dev`
- Database:
  - local SQLite if `DATABASE_URL` is not set
  - Neon/Postgres if `DATABASE_URL` is set
- AI:
  - `mock`
  - `claude`
  - `openai_compatible`

Browser `SpeechRecognition` dictation was tested and is currently disabled in the user-facing UI for demo reliability. `Clean up notes` remains available in the workspace.

## Local run

1. Copy `.env.example` to `.env`.
2. Set local demo auth:
   - `AUTH_MODE=manual_invite`
   - `DEMO_SHARED_PASSWORD=...`
   - `DEMO_ALLOWED_EMAILS=email1@example.org,email2@example.org`
3. Start the app:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-demo.ps1 -Port 4173 -AuthMode manual_invite
```

4. Open [http://127.0.0.1:4173](http://127.0.0.1:4173).
5. Sign in with an allowlisted email and the shared team password.

For local development without the invite screen:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-demo.ps1 -Port 4173 -AuthMode local_dev
```

If you want to test Postgres locally instead of SQLite, install the Python dependency first:

```powershell
python -m pip install -r requirements.txt
```

## Local database behavior

- If `DATABASE_URL` is empty or unset, the app uses SQLite.
- Default SQLite file: `impact_story_builder_demo.sqlite3`
- The backend creates the database and required tables automatically on startup.
- Minimal schema migration runs automatically for older local interview tables.

To reset local SQLite state:

```powershell
Remove-Item .\impact_story_builder_demo.sqlite3* -Force
```

## Auth modes

- `manual_invite`
  - intended short-term internal demo mode
  - user enters an email and a shared team password
  - backend checks the email against `DEMO_ALLOWED_EMAILS`
  - backend checks the password against `DEMO_SHARED_PASSWORD`
  - backend issues an HttpOnly session cookie
  - demo-only: the email is user-entered and not strongly verified

- `local_dev`
  - development-only bypass mode
  - no login screen
  - backend signs requests in as `DEV_USER_EMAIL`

`manual_invite` is not production-ready security. For production, replace it with stronger identity verification such as Cloudflare Access, OTP, SSO, or validated JWT/session middleware.

## Free pilot deployment target

This repo is prepared for:

- Render Free Web Service
- Neon Free Postgres
- `AUTH_MODE=manual_invite`

It is not set up for Cloudflare Access, OTP, SSO, public sharing, or complex permissions yet.

## Render setup

### Build command

```bash
python -m pip install -r requirements.txt
```

### Start command

```bash
python server.py --host 0.0.0.0 --port $PORT
```

### Required Render env vars

Set these in the Render dashboard:

- `AUTH_MODE=manual_invite`
- `DEMO_ALLOWED_EMAILS=person1@sei.org,person2@sei.org`
- `DEMO_SHARED_PASSWORD=choose-a-demo-password`
- `DATABASE_URL=<your Neon connection string>`
- `SESSION_COOKIE_SECURE=auto`

Recommended defaults:

- `DEFAULT_AI_PROVIDER=mock`
- `CLAUDE_API_KEY=<optional shared Claude key>`
- `CLAUDE_DEFAULT_BASE_URL=https://api.anthropic.com`
- `CLAUDE_DEFAULT_MODEL=claude-sonnet-4-6`
- `OPENAI_COMPATIBLE_DEFAULT_BASE_URL=https://api.openai.com`
- `OPENAI_COMPATIBLE_DEFAULT_MODEL=gpt-5.4-mini`

Optional:

- `SESSION_COOKIE_NAME=impact_story_demo_session`

Not required for the current session model:

- `SESSION_SECRET`

This app uses server-generated random session tokens stored in the database, so there is no frontend-exposed shared secret or signed-cookie secret to configure yet.

## Neon setup

### What you need to do manually

1. Create a free Neon project.
2. Create or use the default database.
3. Copy the connection string from Neon.
4. Keep Neon’s SSL requirement in the URL. A typical Neon URL already includes `sslmode=require`.
5. Paste that value into Render as `DATABASE_URL`.

The app will automatically:

- detect `DATABASE_URL`
- use Postgres instead of local SQLite
- create the required tables on first start
- apply the minimal interview-table migration if needed

## Render deployment steps

1. Push this repo to GitHub.
2. In Render, create a new Web Service from that GitHub repo.
3. Let Render detect Python from `requirements.txt`.
4. Use the build and start commands above.
5. Add the required environment variables.
6. Deploy.
7. Open the Render service URL in a browser.
8. Sign in with one of the allowlisted demo emails.

## Shared draft workflow to test after deployment

Use two allowlisted emails, for example:

- `pilot.one@sei.org`
- `pilot.two@sei.org`

### Test private interview isolation

1. Sign in as `pilot.one@sei.org`.
2. Create an interview and keep `Visibility = Private`.
3. Save it.
4. Sign out.
5. Sign in as `pilot.two@sei.org`.
6. Confirm that private draft does not appear in `My Interviews` or `Shared Interviews`.

### Test shared interview visibility

1. Sign in as `pilot.one@sei.org`.
2. Open one of your drafts.
3. Set `Visibility = Shared`.
4. Save if needed.
5. Sign out.
6. Sign in as `pilot.two@sei.org`.
7. Confirm that draft appears in `Shared Interviews`.

### Test copy behavior

1. As `pilot.two@sei.org`, open the shared interview.
2. Confirm the original opens in view-only mode if you are not the owner.
3. Click `Copy`.
4. Confirm the copied draft opens as your own editable interview.
5. Confirm the copied draft is `Private` by default and appears in `My Interviews`.

## Provider behavior in the pilot

- `mock` mode works without any API key.
- Claude and OpenAI-compatible requests remain server-side.
- Claude supports two credential paths:
  - `CLAUDE_API_KEY` on the server takes priority and lets testers use Claude without entering a key in the UI.
  - A request-provided Claude API key is still supported as a fallback for local testing when `CLAUDE_API_KEY` is not set.
- OpenAI-compatible mode still uses the existing per-session UI fields.

## Claude key handling

- `CLAUDE_API_KEY` is an optional server-managed environment variable for Claude mode.
- In Render, set `CLAUDE_API_KEY` in Environment Variables if you want manual-invite testers to use Claude without typing their own key.
- Keep `DEFAULT_AI_PROVIDER=mock` unless you explicitly want Claude selected by default.
- Never commit real API keys.
- Do not put real API keys in frontend code, screenshots, README examples, or GitHub.
- This is still a demo setup. Shared API keys should live only in server environment variables.

## Repo hygiene

- `.env` is ignored
- local SQLite files are ignored
- build output is ignored
- do not commit real emails, shared passwords, database URLs, or API keys

## Known free-pilot limitations

- `manual_invite` is demo-only and not strong identity verification
- all demo users share one password
- there is no Cloudflare Access, OTP, SSO, or JWT validation yet
- there is no full story library yet
- there is no admin panel, reporting, public sharing, or Word export
- AI credentials are still part of a demo-oriented setup even when `CLAUDE_API_KEY` is server-managed
