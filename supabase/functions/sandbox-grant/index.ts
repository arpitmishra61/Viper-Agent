// Hands the shared E2B key to a signed-in user while they still have free sandbox sessions.
// Deploy: supabase functions deploy sandbox-grant
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.4";

const FREE_LIMIT = 3;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const url = Deno.env.get("SUPABASE_URL")!;
  const asUser = createClient(url, Deno.env.get("SUPABASE_ANON_KEY")!, {
    global: { headers: { Authorization: req.headers.get("Authorization") ?? "" } },
  });
  const { data: { user }, error: authError } = await asUser.auth.getUser();
  if (authError || !user) return json({ error: "unauthorized" }, 401);

  const admin = createClient(url, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);

  // Read the key first so a missing secret never costs the user a free session.
  const { data: secret, error: secretError } = await admin
    .from("app_secrets").select("value").eq("name", "E2B_API_KEY").single();
  if (secretError || !secret) return json({ error: "sandbox_not_configured" }, 503);

  const { data, error } = await admin.rpc("claim_sandbox_grant", {
    uid: user.id,
    max_grants: FREE_LIMIT,
  });
  if (error || !data?.length) return json({ error: "quota_check_failed" }, 500);

  const { granted, used } = data[0];
  if (!granted) return json({ error: "quota_exhausted", used, limit: FREE_LIMIT }, 403);

  return json({ key: secret.value, used, limit: FREE_LIMIT });
});
