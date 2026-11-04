// Stores each signed-in user's API keys (OpenRouter, Hugging Face, E2B, ...), encrypted.
// POST { action: "list" | "get" | "set" | "delete", name?, value? }, with the user's access token as Bearer.
// Deploy: supabase functions deploy user-keys
// Secret: supabase secrets set KEYS_ENCRYPTION_SECRET=<base64 of 32 random bytes>
//
// AES-256-GCM, a fresh random IV per write, and the row identity (user id + key name) bound in as
// additional authenticated data, so a ciphertext copied to another row or user will not decrypt.
// Losing or changing KEYS_ENCRYPTION_SECRET makes every stored key unreadable (users just paste them again).
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.4";

const NAME_RE = /^[A-Z][A-Z0-9_]{0,63}$/;
const MAX_VALUE_CHARS = 4096;
const MAX_KEYS_PER_USER = 20;

const enc = new TextEncoder();
const dec = new TextDecoder();

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });

const fromB64 = (s: string) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
const toB64 = (b: Uint8Array) => btoa(Array.from(b, (x) => String.fromCharCode(x)).join(""));

async function aesKey(): Promise<CryptoKey | null> {
  const raw = Deno.env.get("KEYS_ENCRYPTION_SECRET");
  if (!raw) return null;
  try {
    const bytes = fromB64(raw.trim());
    if (bytes.length !== 32) return null;
    return await crypto.subtle.importKey("raw", bytes, "AES-GCM", false, ["encrypt", "decrypt"]);
  } catch {
    return null;
  }
}

async function seal(key: CryptoKey, aad: string, plain: string): Promise<string> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(
    await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: enc.encode(aad) }, key, enc.encode(plain)),
  );
  const out = new Uint8Array(iv.length + ct.length);
  out.set(iv);
  out.set(ct, iv.length);
  return toB64(out);
}

async function open(key: CryptoKey, aad: string, sealed: string): Promise<string> {
  const b = fromB64(sealed);
  const pt = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: b.slice(0, 12), additionalData: enc.encode(aad) },
    key,
    b.slice(12),
  );
  return dec.decode(pt);
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  let body: { action?: string; name?: string; value?: string };
  try {
    body = await req.json();
  } catch {
    return json({ error: "bad_request" }, 400);
  }

  const url = Deno.env.get("SUPABASE_URL")!;
  const asUser = createClient(url, Deno.env.get("SUPABASE_ANON_KEY")!, {
    global: { headers: { Authorization: req.headers.get("Authorization") ?? "" } },
  });
  const { data: { user }, error: authError } = await asUser.auth.getUser();
  if (authError || !user) return json({ error: "unauthorized" }, 401);

  const admin = createClient(url, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const table = () => admin.from("user_keys");

  if (body.action === "list") {
    const { data, error } = await table().select("name").eq("user_id", user.id).order("name");
    if (error) return json({ error: "db_error" }, 500);
    return json({ names: data.map((r) => r.name) });
  }

  const name = body.name ?? "";
  if (!NAME_RE.test(name)) return json({ error: "bad_name" }, 400);
  const aad = `${user.id}:${name}`;

  if (body.action === "delete") {
    const { error } = await table().delete().eq("user_id", user.id).eq("name", name);
    return error ? json({ error: "db_error" }, 500) : json({ ok: true });
  }

  const key = await aesKey();
  if (!key) return json({ error: "keys_not_configured" }, 503);

  if (body.action === "get") {
    const { data, error } = await table().select("value_enc").eq("user_id", user.id).eq("name", name).maybeSingle();
    if (error) return json({ error: "db_error" }, 500);
    if (!data) return json({ error: "not_found" }, 404);
    try {
      return json({ value: await open(key, aad, data.value_enc) });
    } catch {
      return json({ error: "undecryptable" }, 500);   // secret rotated or row tampered with
    }
  }

  if (body.action === "set") {
    const value = typeof body.value === "string" ? body.value.trim() : "";
    if (!value || value.length > MAX_VALUE_CHARS) return json({ error: "bad_value" }, 400);

    const { data: existing } = await table().select("name").eq("user_id", user.id);
    if (existing && existing.length >= MAX_KEYS_PER_USER && !existing.some((r) => r.name === name)) {
      return json({ error: "too_many_keys" }, 400);
    }
    const { error } = await table().upsert({
      user_id: user.id,
      name,
      value_enc: await seal(key, aad, value),
      updated_at: new Date().toISOString(),
    });
    return error ? json({ error: "db_error" }, 500) : json({ ok: true });
  }

  return json({ error: "bad_action" }, 400);
});
