# Viper backend (Supabase)

Accounts (email and password, signed in through your browser) and the free E2B sandbox quota.
Everything here fits the Supabase free tier.

## 1. Database and secrets

1. SQL editor: run `migrations/0001_init.sql`.
2. Table Editor > `app_secrets` > Insert row: `name = E2B_API_KEY`, `value = <your E2B key>`.

## 2. Auth settings (Supabase dashboard)

**Authentication > Sign In / Providers > Email**
- Email provider on, **Confirm email** on. Users cannot sign in until they confirm.
- Minimum password length: 8 (the page also enforces this).

**Authentication > URL Configuration**

| Setting | Value |
| --- | --- |
| Site URL | `https://viper-auth.vercel.app/` |
| Redirect URLs | `https://viper-auth.vercel.app/**` |
| Redirect URLs (local testing only) | `http://localhost:8000/**` |

The trailing `**` is required. The page adds `?mode=...&state=...&port=...` to the confirmation and
password-reset links, and Supabase rejects any redirect that does not match the list.

**Authentication > Email Templates**: the default Confirm signup and Reset password templates work as they are.

**Authentication > SMTP Settings** (required before real users sign up): Supabase's built-in mailer only
sends to your own team members and is heavily rate limited. Add a custom SMTP server. Resend and Brevo have free tiers.

## 3. Host the auth page

The page is `viper-auth/index.html`, deployed to Vercel at `https://viper-auth.vercel.app/`. It cannot run
as a Claude artifact, so it needs a real static host.

1. Copy `viper-auth/config.example.js` to `viper-auth/config.js`.
2. Fill in `supabaseUrl` and `supabaseAnonKey` (Project Settings > API). The anon or publishable key is public
   by design. Never use the `service_role` or `sb_secret_` key.

## 4. Point the CLI at it

| Variable | Value |
| --- | --- |
| `VIPER_SUPABASE_URL` | `https://YOUR-REF.supabase.co` |
| `VIPER_SUPABASE_ANON_KEY` | the anon public key |
| `VIPER_AUTH_URL` | `https://viper-auth.vercel.app/` |

`auth.py` already has these three values as defaults, so published installs need no environment variables.
The variables only override them.
Quick test in PowerShell:

```
$env:VIPER_SUPABASE_URL = "https://YOUR-REF.supabase.co"
$env:VIPER_SUPABASE_ANON_KEY = "your-anon-key"
$env:VIPER_AUTH_URL = "https://YOUR-HOST/auth"
python agent.py login
```

A `.env` next to `agent.py` skips login entirely (source checkout). Rename it to test the real flow.

## 5. Deploy the sandbox function

```
npx supabase login
npx supabase link --project-ref YOUR-REF
npx supabase functions deploy sandbox-grant
```

## How sign-in works

1. `viper` shows **Sign up / Sign in**. The CLI starts a one-shot server on `127.0.0.1` (random port) and opens
   `auth.html?mode=signup|signin&state=...&port=...`.
2. The user signs up or in on the page. Passwords go to Supabase, never to Viper.
3. The page submits the session to `http://127.0.0.1:PORT/callback`. The CLI checks the `state`, the `Origin`
   (when the browser sends one), and asks Supabase to confirm the token and that the email is verified.
   Then it stores the session and shuts the server down.
4. The browser shows "You're signed in". The CLI prints "Authenticated as ...". Tokens refresh automatically.

If the CLI is not waiting any more (it times out after 5 minutes), run `viper login` again.
Confirmation and reset emails carry the `state` so clicking them can finish the same login.
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
