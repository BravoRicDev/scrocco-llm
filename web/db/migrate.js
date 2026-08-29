import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import pg from "pg";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATABASE_URL = process.env.DATABASE_URL;
if (!DATABASE_URL) {
  console.error("DATABASE_URL non configurata");
  process.exit(1);
}
const pool = new pg.Pool({ connectionString: DATABASE_URL });

async function migrate() {
  await pool.query(`CREATE TABLE IF NOT EXISTS schema_migrations (
      filename VARCHAR(255) PRIMARY KEY,
      applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`);
  
  // Imposta i parametri di sessione per il bootstrap admin prima del giro di migrazioni
  const bootstrapAdminEmail = process.env.BOOTSTRAP_ADMIN_EMAIL;
  const bootstrapAdminPassword = process.env.BOOTSTRAP_ADMIN_PASSWORD;
  
  // Usa set_config per impostare le GUC in modo sicuro
  if (bootstrapAdminEmail) {
    await pool.query(`SELECT set_config('app.bootstrap_admin_email', $1, false)`, [bootstrapAdminEmail]);
  }
  if (bootstrapAdminPassword) {
    await pool.query(`SELECT set_config('app.bootstrap_admin_password', $1, false)`, [bootstrapAdminPassword]);
  }
  
  const applied = new Set((await pool.query("SELECT filename FROM schema_migrations")).rows.map(r => r.filename));
  const files = fs.readdirSync(__dirname).filter(f => f.endsWith(".sql")).sort();
  for (const file of files) {
    if (applied.has(file)) continue;
    const sql = fs.readFileSync(path.join(__dirname, file), "utf8");
    try {
      await pool.query(sql);
      await pool.query("INSERT INTO schema_migrations (filename) VALUES ($1) ON CONFLICT DO NOTHING", [file]);
      console.log(`Migrato: ${file}`);
    } catch (err) { console.error(`Errore in ${file}:`, err.message); process.exit(1); }
  }
  await pool.end();
  console.log("Migrazioni completate.");
}
migrate();