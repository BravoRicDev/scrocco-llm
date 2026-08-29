-- Migrazione: 003_bootstrap_admin.sql
-- Inserisce un utente admin iniziale se le GUC app.bootstrap_admin_email/password sono settate
-- e la tabella users è vuota.

-- Assicurati che pgcrypto sia disponibile per crypt()/gen_salt()
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
DECLARE
  v_email TEXT;
  v_pwd TEXT;
BEGIN
  -- Leggi le impostazioni dai parametri di sessione (se presenti)
  v_email := current_setting('app.bootstrap_admin_email', true);
  v_pwd   := current_setting('app.bootstrap_admin_password', true);

  -- Se email presente e non vuota e nessun utente esiste, crea admin bootstrap
  IF v_email IS NOT NULL AND v_email <> '' AND NOT EXISTS (SELECT 1 FROM users) THEN
    INSERT INTO users (email, name, password_hash, role, status, token_version, mfa_enabled, created_at, updated_at)
    VALUES (
      v_email,
      'Bootstrap Admin',
      crypt(v_pwd, gen_salt('bf')),
      'admin',
      'active',
      0,
      FALSE,
      NOW(),
      NOW()
    );
    RAISE NOTICE 'admin bootstrap creato: %', v_email;
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'bootstrap saltato: %', SQLERRM;
END $$;