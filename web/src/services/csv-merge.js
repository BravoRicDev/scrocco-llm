// Fase 4 (FRONTEND-ROADMAP-OPERATIONAL.md): riconciliazione delle credenziali
// protette fra il CSV inviato dal client e quello persistito sul gateway.
//
// Il gateway maschera le colonne chiave (`csv_store.py`) e riscrive il file con
// csv.writer: il client non puo' quindi rimandare indietro il valore mascherato
// senza distruggere la chiave reale. Il client sostituisce le celle chiave non
// toccate con la sentinella `__UNCHANGED_SECRET__`, e questo modulo le rimpiazza
// con i valori reali letti dal gateway, per indice di riga ordinale.
import crypto from "node:crypto";

export function isKeyColumn(headerName) {
  const h = String(headerName || "").trim().toLowerCase();
  return /key|api_key/i.test(h) || h.startsWith("scrocco-llm-");
}

export function computeCsvHash(raw) {
  return crypto.createHash("sha256").update(String(raw || "")).digest("hex");
}

/**
 * Parser CSV minimale conforme RFC 4180 con supporto campi quotati e newline.
 * Salta commenti preambolo e righe vuote prima dell'header.
 */
export function parseCsvRows(raw) {
  const lines = [];
  let row = [];
  let inQuotes = false;
  let field = "";
  const text = String(raw || "");

  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    const next = text[i + 1];

    if (c === '"') {
      if (inQuotes && next === '"') {
        field += '"';
        i++;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (c === "," && !inQuotes) {
      row.push(field);
      field = "";
    } else if ((c === "\r" || c === "\n") && !inQuotes) {
      if (c === "\r" && next === "\n") i++;
      row.push(field);
      field = "";
      lines.push(row);
      row = [];
    } else {
      field += c;
    }
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    lines.push(row);
  }

  // Trova la prima riga non-commento e non-vuota (Header)
  let headerIdx = -1;
  for (let i = 0; i < lines.length; i++) {
    const firstCell = (lines[i][0] || "").trim();
    if (firstCell && !firstCell.startsWith("#")) {
      headerIdx = i;
      break;
    }
  }

  if (headerIdx === -1) return { header: [], rows: [] };

  const header = lines[headerIdx].map((h) => h.trim());
  const rows = [];
  for (let i = headerIdx + 1; i < lines.length; i++) {
    const r = lines[i];
    if (r.length === 1 && r[0].trim() === "") continue; // salta righe vuote
    rows.push(r);
  }

  return { header, rows };
}

/**
 * Merge sicuro delle sentinelle e controllo anti-mascheramento.
 * Ritorna { ok: true, mergedRaw, hash } oppure { ok: false, error: string, status: number }
 */
export function mergeCsvSecrets(sentRaw, baseRaw) {
  const sent = parseCsvRows(sentRaw);
  const base = parseCsvRows(baseRaw);

  if (sent.header.length === 0) {
    return { ok: false, error: "CSV inviato privo di header valido", status: 400 };
  }

  const keyColIndices = new Set(
    sent.header.map((h, idx) => (isKeyColumn(h) ? idx : -1)).filter((idx) => idx >= 0)
  );

  // Rifiuto categorico universale di caratteri di mascheramento in QUALSIASI cella di QUALSIASI colonna
  for (let r = 0; r < sent.rows.length; r++) {
    for (let c = 0; c < sent.rows[r].length; c++) {
      const val = sent.rows[r][c] || "";
      if (val.includes("…") || val.includes("***") || val.includes("...")) {
        return {
          ok: false,
          error: "Rilevato valore mascherato non consentito nella riga " + (r + 1) + ", colonna " + (sent.header[c] || c + 1) + ". Ricaricare la pagina o inserire un valore in chiaro.",
          status: 400
        };
      }
    }
  }

  // Rifiuto sentinella in colonne non-chiave
  for (let r = 0; r < sent.rows.length; r++) {
    for (let colIdx = 0; colIdx < sent.header.length; colIdx++) {
      if (!keyColIndices.has(colIdx) && (sent.rows[r][colIdx] || "") === "__UNCHANGED_SECRET__") {
        return {
          ok: false,
          error: "Sentinella di protezione non consentita in colonna non-chiave.",
          status: 400
        };
      }
    }
  }

  // Verifica invarianti per merge posizionale per indice
  const hasSentinel = sent.rows.some((r) => Array.from(keyColIndices).some((c) => r[c] === "__UNCHANGED_SECRET__"));
  if (hasSentinel) {
    if (sent.header.join(",") !== base.header.join(",")) {
      return { ok: false, error: "Header CSV disallineato dal server. Impossibile riconciliare le credenziali protette.", status: 400 };
    }
    if (sent.rows.length !== base.rows.length) {
      return { ok: false, error: "Numero di righe modificato. Impossibile riconciliare le credenziali protette per indice.", status: 400 };
    }

    // Sostituzione delle sentinelle con le chiavi reali dal baseRaw
    for (let r = 0; r < sent.rows.length; r++) {
      for (const colIdx of keyColIndices) {
        if (sent.rows[r][colIdx] === "__UNCHANGED_SECRET__") {
          sent.rows[r][colIdx] = base.rows[r][colIdx] || "";
        }
      }
    }
  }

  // Ricostruzione CSV
  const serializeRow = (cells) =>
    cells.map((cell) => {
      const s = String(cell ?? "");
      if (s.includes(",") || s.includes('"') || s.includes("\n") || s.includes("\r")) {
        return '"' + s.replace(/"/g, '""') + '"';
      }
      return s;
    }).join(",");

  const outLines = [serializeRow(sent.header)];
  sent.rows.forEach((r) => outLines.push(serializeRow(r)));
  const mergedRaw = outLines.join("\n") + "\n";
  const newHash = computeCsvHash(mergedRaw);

  return { ok: true, mergedRaw, hash: newHash };
}
