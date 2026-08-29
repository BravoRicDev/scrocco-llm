const defaultTranslations = {
  'it': {
    'nav.dashboard': 'Dashboard',
    'nav.logout': 'Logout',
    'error.title': 'Errore',
    'error.heading': 'Si è verificato un errore',
    'error.default': 'Si è verificato un errore.',
    'error.dashboard': 'Torna alla dashboard',
    'auth.login.title': 'Accedi',
    'auth.login.subtitle': 'Inserisci la tua email per accedere',
    'auth.login.email_label': 'Email',
    'auth.login.submit': 'Accedi',
    'auth.login.sending': 'Invio in corso...',
    'auth.login.check_email_fallback': 'Controlla la tua email per il link di accesso',
    'auth.login.connection_error': 'Errore di connessione',
    'auth.login.magic_link': 'Link magico disponibile se SMTP è configurato',
    'auth.verify.title': 'Verifica',
    'auth.verify.subtitle': 'Inserisci il codice OTP di 6 cifre',
    'auth.verify.otp_label': 'Codice OTP',
    'auth.verify.submit': 'Verifica',
    'auth.verify.invalid_code_fallback': 'Codice non valido o scaduto',
    'auth.verify.request_new_code': 'Richiedi nuovo codice',
    'auth.verify.connection_error': 'Errore di connessione',
    'auth.verify.resend_token': 'Invia nuovo token',
    'lang.select_label': 'Lingua'
  },
  'en': {
    'nav.dashboard': 'Dashboard',
    'nav.logout': 'Logout',
    'error.title': 'Error',
    'error.heading': 'An error occurred',
    'error.default': 'An error has occurred.',
    'error.dashboard': 'Go to dashboard',
    'auth.login.title': 'Log in',
    'auth.login.subtitle': 'Enter your email to log in',
    'auth.login.email_label': 'Email',
    'auth.login.submit': 'Log in',
    'auth.login.sending': 'Sending...',
    'auth.login.check_email_fallback': 'Check your email for the magic link',
    'auth.login.connection_error': 'Connection error',
    'auth.login.magic_link': 'Magic link available if SMTP is configured',
    'auth.verify.title': 'Verify',
    'auth.verify.subtitle': 'Enter the 6-digit OTP',
    'auth.verify.otp_label': 'OTP Code',
    'auth.verify.submit': 'Verify',
    'auth.verify.invalid_code_fallback': 'Invalid or expired code',
    'auth.verify.request_new_code': 'Request new code',
    'auth.verify.connection_error': 'Connection error',
    'auth.verify.resend_token': 'Send new token',
    'lang.select_label': 'Language'
  }
};

export function translate(lang, key) {
  lang = lang || 'it';
  const translations = defaultTranslations[lang] || defaultTranslations.it;
  return translations[key] || key;
}

export default { translate };