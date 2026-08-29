window.scw = {
  async fetch(path, opts = {}) {
    const headers = { ...opts.headers };
    const requestId = 'scw-' + Date.now() + '-' + Math.random().toString(36).substr(2, 9);
    headers['X-Request-Id'] = requestId;
    
    const csrfMeta = document.querySelector('meta[name="csrf-token"]');
    if (csrfMeta && !headers['X-CSRF-Token']) {
      headers['X-CSRF-Token'] = csrfMeta.content;
    }

    // comodità: opts.json -> body JSON serializzato + Content-Type
    const fetchOpts = { ...opts, headers };
    if (opts.json !== undefined) {
      fetchOpts.body = JSON.stringify(opts.json);
      if (!headers['Content-Type'] && !headers['content-type']) {
        headers['Content-Type'] = 'application/json';
      }
      delete fetchOpts.json;
    }

     try {
       const response = await fetch(path, fetchOpts);
       
       if (response.status === 401) {
         // FIX: Preserve the original URL so users can return after re-authenticating
         var returnTo = window.location.pathname + window.location.search;
         if (returnTo !== "/login") {
           window.location.href = "/login?return_to=" + encodeURIComponent(returnTo);
         } else {
           window.location.href = "/login";
         }
         return null;
       }
      
      return response;
    } catch (error) {
      console.error('Request failed:', error);
      throw error;
    }
  },
  
  confirmForm(formSelector, message) {
    const form = document.querySelector(formSelector);
    if (!form) {
      console.error('Form not found:', formSelector);
      return false;
    }
    
    if (!confirm(message || 'Sei sicuro di voler continuare?')) {
      return false;
    }
    
    form.submit();
    return true;
  },

  escapeHTML(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  },

  theme: {
    KEY: 'scw-theme',
    current: 'light',

    setTheme(theme, persist) {
      this.current = theme === 'dark' ? 'dark' : 'light';
      document.documentElement.dataset.theme = this.current;
      if (persist) {
        try { localStorage.setItem(this.KEY, this.current); } catch (e) {}
      }
      const btn = document.getElementById('themeToggle');
      if (btn) {
        btn.textContent = this.current === 'dark' ? 'Tema chiaro' : 'Tema scuro';
        btn.title = this.current === 'dark' ? 'Passa al tema chiaro' : 'Passa al tema scuro';
      }
    },

    toggleTheme() {
      this.setTheme(this.current === 'dark' ? 'light' : 'dark', true);
    },

    init() {
      let saved = null;
      try { saved = localStorage.getItem(this.KEY); } catch (e) {}
      this.setTheme(saved === 'dark' ? 'dark' : 'light', false);
      const btn = document.getElementById('themeToggle');
      if (btn) btn.addEventListener('click', () => this.toggleTheme());
    }
  },

  search: {
    init() {
      const input = document.getElementById('navSearch');
      if (!input) return;
      input.addEventListener('input', () => this.filter(input.value));
      const helpClose = document.getElementById('helpClose');
      if (helpClose) helpClose.addEventListener('click', () => window.scw.keys.toggleHelp(false));
    },

    filter(q) {
      const links = document.querySelectorAll('[data-nav-item]');
      if (!links.length) return;
      const query = (q || '').trim().toLowerCase();
      links.forEach((link) => {
        let text = link.getAttribute('data-nav-text');
        if (text === null) {
          text = link.textContent || '';
          link.setAttribute('data-nav-text', text);
        }
        const href = (link.getAttribute('href') || '').toLowerCase();
        if (!query) {
          link.style.display = '';
          link.innerHTML = window.scw.escapeHTML(text);
          return;
        }
        const haystack = (text + ' ' + href).toLowerCase();
        if (haystack.indexOf(query) === -1) {
          link.style.display = 'none';
          return;
        }
        link.style.display = '';
        const idx = text.toLowerCase().indexOf(query);
        if (idx === -1) {
          link.innerHTML = window.scw.escapeHTML(text);
          return;
        }
        link.innerHTML = window.scw.escapeHTML(text.slice(0, idx)) +
          '<span class="hl">' + window.scw.escapeHTML(text.slice(idx, idx + query.length)) + '</span>' +
          window.scw.escapeHTML(text.slice(idx + query.length));
      });
    }
  },

  keys: {
    gmap: {
      d: '/',
      l: '/deployments',
      s: '/system',
      u: '/users',
      p: '/policy',
      r: '/profiles',
      c: '/capabilities',
      e: '/expiring',
      h: '/history'
    },
    _pending: null,

    isEditable(el) {
      if (!el || !el.tagName) return false;
      const tag = el.tagName.toLowerCase();
      return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
    },

    toggleHelp(show) {
      const modal = document.getElementById('helpModal');
      if (!modal) return;
      modal.hidden = !show;
      modal.setAttribute('aria-hidden', show ? 'false' : 'true');
    },

    onKeyDown(e) {
      const k = e.key;
      if (this.isEditable(e.target) && k !== 'Escape') return;

      if (k === 'Escape') {
        if (this._pending) { clearTimeout(this._pending); this._pending = null; }
        this.toggleHelp(false);
        const input = document.getElementById('navSearch');
        if (input && input.value) {
          input.value = '';
          window.scw.search.filter('');
        }
        return;
      }

      if (k === '/') {
        e.preventDefault();
        const input = document.getElementById('navSearch');
        if (input) input.focus();
        return;
      }

      if (k === '?') {
        e.preventDefault();
        this.toggleHelp(true);
        return;
      }

      if (this._pending) {
        clearTimeout(this._pending);
        this._pending = null;
        if (k === 'g') return;
        const path = this.gmap[k];
        if (path) window.location.href = path;
        return;
      }

      if (k === 'g') {
        e.preventDefault();
        this._pending = setTimeout(() => { this._pending = null; }, 800);
      }
    },

    init() {
      document.addEventListener('keydown', (e) => this.onKeyDown(e));
      const modal = document.getElementById('helpModal');
      if (modal) modal.addEventListener('click', (e) => { if (e.target === modal) this.toggleHelp(false); });
    }
  },

  init() {
    this.theme.init();
    this.search.init();
    this.keys.init();
  }
};

document.addEventListener('DOMContentLoaded', () => {
  if (window.scw && typeof window.scw.init === 'function') window.scw.init();
});