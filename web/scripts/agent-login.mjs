#!/usr/bin/env node

import fs from "fs";
import os from "os";
import path from "path";

const argv = process.argv.slice(2);
let email = "";
let password = "";
let baseUrl = "";
let magicLink = false;

for (let i = 0; i < argv.length; i++) {
  const arg = argv[i];
  switch (arg) {
    case "--email":
      email = argv[++i] || "";
      break;
    case "--password":
      password = argv[++i] || "";
      break;
    case "--base-url":
      baseUrl = argv[++i] || "";
      break;
    case "--magic-link":
      magicLink = true;
      break;
    default:
      break;
  }
}

const base = baseUrl || process.env.SCROCCO_WEB_URL || "http://127.0.0.1:3000";

if (magicLink) {
  console.log(`Apri ${base}/login, richiedi il magic-link, poi usa POST ${base}/api/agent/verify-otp con {email, token, otp} per ottenere il JWT agent; quindi POST ${base}/api/agent/api-tokens con quel Bearer.`);
  process.exit(0);
}

if (!email || !password) {
  console.error("Uso: node scripts/agent-login.mjs --email a@b --password 'x' [--base-url URL]");
  process.exit(1);
}

async function main() {
  let loginRes;
  try {
    loginRes = await fetch(`${base}/api/agent/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
  } catch (err) {
    console.error(`Errore di connessione: ${err.message}`);
    process.exit(1);
  }

  if (!loginRes.ok) {
    let detail = "";
    try {
      const d = await loginRes.json();
      detail = d.error?.message || JSON.stringify(d);
    } catch {
      detail = await loginRes.text();
    }
    console.error(`Login fallito (${loginRes.status}): ${detail}`);
    process.exit(1);
  }

  const { token } = await loginRes.json();

  const dir = path.join(os.homedir(), ".scrocco-web");
  fs.mkdirSync(dir, { recursive: true });
  const tokenPath = path.join(dir, "agent.token");
  fs.writeFileSync(tokenPath, token, { mode: 0o600 });

  let createRes;
  try {
    createRes = await fetch(`${base}/api/agent/api-tokens`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ name: "agent-login CLI", expires_days: 120 }),
    });
  } catch (err) {
    console.error(`Errore di connessione: ${err.message}`);
    process.exit(1);
  }

  if (!createRes.ok) {
    let detail = "";
    try {
      const d = await createRes.json();
      detail = d.error?.message || JSON.stringify(d);
    } catch {
      detail = await createRes.text();
    }
    console.error(`Creazione token fallita (${createRes.status}): ${detail}`);
    process.exit(1);
  }

  const data = await createRes.json();

  console.log("Token agent creato con successo.");
  console.log(`Valore token (in chiaro, NON piu' recuperabile): ${data.token}`);
  console.log(`Prefix: ${data.prefix}`);
  console.log(`Scade il: ${data.expires_at}`);
  console.log(`JWT agent salvato in: ~/.scrocco-web/agent.token`);
  console.log("Attenzione: il token in chiaro non sara' piu' recuperabile.");
}

main().catch((err) => {
  console.error(`Errore: ${err.message}`);
  process.exit(1);
});
