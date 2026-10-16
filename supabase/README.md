# Viper backend (Supabase)

Sign in with Google (through your browser) and the free E2B sandbox quota.
Everything here fits the free tiers. No email server is needed, because Google sends no email through Supabase.

## 1. Database and secrets

1. SQL editor: run `migrations/0001_init.sql`.
2. Table Editor > `app_secrets` > Insert row: `name = E2B_API_KEY`, `value = <your E2B key>`.

## 2. Turn on Google sign-in

### 2a. In Google Cloud (console.cloud.google.com)
1. Create or pick a project.
2. **APIs & Services > OAuth consent screen**: choose External, fill in the app name (Viper) and your support email,
   then **Publish app** so any Google account can sign in. Add no extra scopes; the default `email` and `profile` are enough.
3. **APIs & Services > Credentials > Create credentials > OAuth client ID**, type **Web application**.
4. **Authorized redirect URIs**: add `https://YOUR-REF.supabase.co/auth/v1/callback`
   (your Supabase Project URL followed by `/auth/v1/callback`).
5. Create it and copy the **Client ID** and **Client secret**.

### 2b. In Supabase
1. **Authentication > Sign In / Providers > Google**: switch it on, paste the Client ID and Client secret, save.
2. **Authentication > Sign In / Providers > Email**: switch it **off**. Otherwise anyone can still create
   an email and password account through the API. The CLI also rejects any session that did not come from Google.

## 3. Redirect URLs

**Authentication > URL Configuration**

| Setting | Value |
| --- | --- |
| Site URL | `https://viper-auth.vercel.app/` |
| Redirect URLs | `https://viper-auth.vercel.app/**` |
| Redirect URLs (local testing only) | `http://localhost:8000/**` |

The page sends users back to itself after Google, with `?state=...&port=...` added, and Supabase rejects
any redirect that is not on this list.

## 4. Host the auth page

The page is `viper-auth/index.html`, deployed to Vercel at `https://viper-auth.vercel.app/`. It needs a real
static host, because it calls Supabase from the browser.

1. Copy `viper-auth/config.example.js` to `viper-auth/config.js`.
2. Fill in `supabaseUrl` and `supabaseAnonKey` (Project Settings > API). The anon or publishable key is public
   by design. Never use the `service_role` or `sb_secret_` key.

## 5. Point the CLI at it

| Variable | Value |
| --- | --- |
| `VIPER_SUPABASE_URL` | `https://YOUR-REF.supabase.co` |
| `VIPER_SUPABASE_ANON_KEY` | the anon or publishable key |
| `VIPER_AUTH_URL` | `https://viper-auth.vercel.app/` |

`auth.py` already has these three values as defaults, so published installs need no environment variables.
The variables only override them.

A `.env` next to `agent.py` that contains an API key skips login entirely (source checkout). Remove the key
from it (or the file) to test the real flow.

## 6. Deploy the sandbox function

```
npx supabase login
npx supabase link --project-ref YOUR-REF
npx supabase functions deploy sandbox-grant
```

## How sign-in works

1. `viper` shows **Sign in with Google / Continue free / Quit**. The CLI starts a one-shot server on
   `127.0.0.1` (random port) and opens `https://viper-auth.vercel.app/?state=...&port=...`.
2. The user clicks **Continue with Google** and picks an account. Supabase handles the Google exchange.
3. Google sends the user back to the page. The page submits the session to
   `http://127.0.0.1:PORT/callback`. The CLI checks the `state`, the `Origin` (when the browser sends one),
   and asks Supabase to confirm the token, that it came from Google, and that the email is verified.
   Then it stores the session and shuts the server down.
4. The browser shows "You're signed in". The CLI prints "Authenticated as ..." and continues to the provider menu.
   Tokens refresh automatically.

If the CLI is not waiting any more (it times out after 5 minutes), run `viper login` again.
There is no PKCE step: PKCE protects an authorization code that is exchanged later, and this handoff
delivers the session directly. The state blocks forged callbacks and the token check blocks anything
Supabase did not issue.

## Limits you set here

- `FREE_LIMIT` in `functions/sandbox-grant/index.ts`: free sandbox sessions per user (3).
- `FREE_TRIAL_QUERIES` in `auth.py`: free queries without signing in (3). Counted on the user's machine,
  so deleting `~/.viper/trial.json` resets it.

## Know the trade-off

The function returns the E2B key to the client, so the limit of 3 stops honest users but not someone who reads
the key from memory. Set a spend cap and alerts in your E2B dashboard. To cut off abuse, rotate the key in the
E2B dashboard, then update the `app_secrets` row. Clients fetch the key fresh for each sandbox session.
