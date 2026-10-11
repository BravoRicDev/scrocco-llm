// Test unitari PURI di web/src/services/csv-merge.js (Fase 4 della roadmap).
// Nessuna dipendenza da database o servizi: eseguibili con `node --test`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { computeCsvHash, isKeyColumn, parseCsvRows, mergeCsvSecrets } from "../src/services/csv-merge.js";

const BASE = "modello,provider,endpoint,scrocco-llm-x\nm1,groq,https://a/v1,REALKEY1\nm2,openai,https://b/v1,REALKEY2\n";
const SENT = "modello,provider,endpoint,scrocco-llm-x\nm1,groq,https://a/v1,__UNCHANGED_SECRET__\nm2,openai,https://b/v1,NUOVACHIAVE\n";
const MASK = "\u2026"; // ellissi U+2026, come le usa il gateway

test("Test 1: sostituzione della sentinella con la chiave originale per indice", () => {
  const r = mergeCsvSecrets(SENT, BASE);
  assert.equal(r.ok, true);
  assert.ok(r.mergedRaw.includes("REALKEY1"), "la chiave reale della riga 1 deve sopravvivere");
  assert.ok(r.mergedRaw.includes("NUOVACHIAVE"), "la chiave nuova digitata deve essere scritta");
  assert.ok(!r.mergedRaw.includes("__UNCHANGED_SECRET__"), "nessuna sentinella nel CSV finale");
  assert.ok(!r.mergedRaw.includes("REALKEY2"), "la chiave della riga 2 non deve migrare sulla riga 1");
});

test("Test 2: cella chiave svuotata dal client resta vuota (non risorge la vecchia)", () => {
  const r = mergeCsvSecrets(`modello,provider,endpoint,scrocco-llm-x\nm1,groq,https://a/v1,\nm2,openai,https://b/v1,__UNCHANGED_SECRET__\n`, BASE);
  assert.equal(r.ok, true);
  assert.equal(r.mergedRaw.split("\n")[1], "m1,groq,https://a/v1,");
  assert.ok(r.mergedRaw.includes("REALKEY2"));
});

test("Test 3: rifiuto universale (400) di valori mascherati in qualsiasi cella", () => {
  for (const masked of [`sk-a${MASK}xyz`, "***", "sk-...x"]) {
    const r = mergeCsvSecrets(`modello,provider,endpoint,scrocco-llm-x\nm1,groq,${masked},__UNCHANGED_SECRET__\n`, BASE);
    assert.equal(r.ok, false, `non rifiutato: ${masked}`);
    assert.equal(r.status, 400);
    assert.match(r.error, /mascherato/);
  }
});

test("Test 4: sentinella in colonna non-chiave -> 400", () => {
  const r = mergeCsvSecrets("modello,provider,endpoint,scrocco-llm-x\n__UNCHANGED_SECRET__,groq,https://a/v1,K\n", BASE);
  assert.equal(r.ok, false);
  assert.equal(r.status, 400);
  assert.match(r.error, /non-chiave/i);
});

test("Test 5: header o numero di righe divergenti in presenza di sentinelle -> 400", () => {
  const h = mergeCsvSecrets("modello,provider,endpoint,scrocco-llm-y\nm1,groq,https://a/v1,__UNCHANGED_SECRET__\nm2,openai,https://b/v1,__UNCHANGED_SECRET__\n", BASE);
  assert.equal(h.ok, false);
  assert.equal(h.status, 400);
  assert.match(h.error, /Header CSV disallineato/);

  const c = mergeCsvSecrets("modello,provider,endpoint,scrocco-llm-x\nm1,groq,https://a/v1,__UNCHANGED_SECRET__\nm2,openai,https://b/v1,__UNCHANGED_SECRET__\nm3,x,y,__UNCHANGED_SECRET__\n", BASE);
  assert.equal(c.ok, false);
  assert.equal(c.status, 400);
  assert.match(c.error, /Numero di righe modificato/);

  const e = mergeCsvSecrets("", BASE);
  assert.equal(e.ok, false);
  assert.equal(e.status, 400);
});

test("Test 6: hash SHA256 esatto sui byte forniti e coerente col mergedRaw", () => {
  assert.equal(computeCsvHash("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  assert.equal(computeCsvHash(""), computeCsvHash(null));
  const r = mergeCsvSecrets(SENT, BASE);
  assert.equal(r.hash, computeCsvHash(r.mergedRaw));
});

test("extra: senza sentinelle il file passa invariato, e il parser regge quoting/newline", () => {
  const p = mergeCsvSecrets("modello,provider,endpoint,scrocco-llm-x\nm9,groq,https://z/v1,PLAINKEY\n", BASE);
  assert.equal(p.ok, true);
  assert.ok(p.mergedRaw.includes("PLAINKEY"));
  assert.ok(!p.mergedRaw.includes("REALKEY1"));

  const q = mergeCsvSecrets(
    'modello,commento,provider,endpoint,scrocco-llm-x\n"m,1","a ""b""",groq,https://a/v1,"__UNCHANGED_SECRET__"\n',
    'modello,commento,provider,endpoint,scrocco-llm-x\n"m,1","a ""b""",groq,https://a/v1,KEEP\n'
  );
  assert.equal(q.ok, true);
  assert.ok(q.mergedRaw.includes("KEEP"));
  assert.ok(q.mergedRaw.includes('"a ""b"""'));

  const pre = parseCsvRows("# commento di preambolo\n\nmodello,endpoint\nm1,x\n\n");
  assert.deepEqual(pre.header, ["modello", "endpoint"]);
  assert.equal(pre.rows.length, 1);

  assert.equal(isKeyColumn("scrocco-llm-x"), true);
  assert.equal(isKeyColumn("api_key"), true);
  assert.equal(isKeyColumn("modello"), false);
});
