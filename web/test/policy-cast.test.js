// Test unitari PURI di web/src/services/policy-cast.js (Fase 5 della roadmap).
// Importano solo il modulo puro: nessun DATABASE_URL richiesto.
import { test } from "node:test";
import assert from "node:assert/strict";
import { castValue, META, LISTS, MAPS } from "../src/services/policy-cast.js";

test("Test 1-3: campo numerico vuoto, spazi o null -> 'valore numerico atteso' (fix del bug)", () => {
  for (const raw of ["", "   ", null, undefined]) {
    const r = castValue("server.port", raw);
    assert.equal(r.ok, false, `accettato per ${JSON.stringify(raw)}`);
    assert.equal(r.msg, "valore numerico atteso");
  }
  for (const raw of ["", "   ", null]) {
    const r = castValue("adaptive.confidence", raw);
    assert.equal(r.ok, false, `num accettato per ${JSON.stringify(raw)}`);
    assert.equal(r.msg, "valore numerico atteso");
  }
});

test("Test 4: server.port '8080' -> 8080", () => {
  assert.deepEqual(castValue("server.port", "8080"), { ok: true, value: 8080 });
  assert.deepEqual(castValue("server.port", 8080), { ok: true, value: 8080 });
  assert.deepEqual(castValue("server.port", "8080.7"), { ok: true, value: 8080 });
  assert.equal(castValue("server.port", "abc").ok, false);
});

test("Test 5: adaptive.confidence '0.95' -> 0.95", () => {
  assert.deepEqual(castValue("adaptive.confidence", "0.95"), { ok: true, value: 0.95 });
});

test("Test 6: stringa con caratteri speciali restituita GREZZA, senza entità HTML", () => {
  assert.deepEqual(castValue("server.host", "a & b < c >"), { ok: true, value: "a & b < c >" });
  assert.deepEqual(castValue("server.host", "foo & bar < baz >"), { ok: true, value: "foo & bar < baz >" });
});

test("META/LISTS/MAPS espongono le chiavi attese dai route", () => {
  assert.equal(META.scalar["server.port"], "int");
  assert.equal(META.scalar["server.host"], "str");
  assert.equal(META.scalar["debug"], "bool");
  assert.equal(LISTS["routing.allowed_providers"], "list");
  assert.equal(LISTS["routing.models"], "list");
  assert.equal(MAPS["capability_routing.model_capabilities"], "map");
});

test("bool, liste e mappe conservano il comportamento preesistente", () => {
  assert.deepEqual(castValue("debug", "true"), { ok: true, value: true });
  assert.deepEqual(castValue("debug", ""), { ok: true, value: false });
  assert.deepEqual(castValue("routing.models", "a, b\nc"), { ok: true, value: ["a", "b", "c"] });
  assert.deepEqual(castValue("routing.models", ["x", "y"]), { ok: true, value: ["x", "y"] });
  assert.deepEqual(castValue("capability_routing.model_capabilities", '{"a":true}'), { ok: true, value: { a: true } });
  assert.equal(castValue("capability_routing.model_capabilities", "nope").ok, false);
});
