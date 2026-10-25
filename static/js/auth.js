/**
 * Login for changes (auth.py). Loaded first on every page: wraps fetch so an
 * action refused with 401 {login_required} opens a small login box and, once
 * logged in, is sent again. Reading never needs a login. The password is
 * never stored in the browser; the server keeps a session cookie.
 */
const Auth = {
    pending: null,

    install() {
        if (window.__authFetch) return;
        const original = window.fetch.bind(window);
        window.__authFetch = original;
        window.fetch = async (input, init) => {
            const resp = await original(input, init);
            if (resp.status !== 401) return resp;
            let body = null;
            try { body = await resp.clone().json(); } catch (e) { return resp; }
            if (!body || !body.login_required) return resp;
            const ok = await this.prompt(body.error);
            return ok ? original(input, init) : resp;
        };
    },

    /** Show the login box; resolves true once logged in, false on cancel. */
    prompt(message) {
        if (this.pending) return this.pending;
        this.pending = new Promise(resolve => {
            const box = document.createElement('div');
            box.className = 'auth-overlay';
            box.innerHTML = `<form class="auth-box" autocomplete="off">
                <h3>Log in</h3>
                <p class="form-hint">${this.esc(message || 'Log in to make changes')}. Reading works without a login.</p>
                <input type="password" class="form-input" name="password" placeholder="Password" required>
                <p class="auth-error" hidden></p>
                <div class="auth-actions">
                    <button type="submit" class="btn btn-primary">Log in</button>
                    <button type="button" class="btn btn-secondary" data-cancel>Cancel</button>
                </div></form>`;
            document.body.appendChild(box);
            const form = box.querySelector('form');
            const input = form.querySelector('input');
            const err = form.querySelector('.auth-error');
            const done = (ok) => { box.remove(); this.pending = null; resolve(ok); };
            form.querySelector('[data-cancel]').addEventListener('click', () => done(false));
            box.addEventListener('keydown', e => { if (e.key === 'Escape') done(false); });
            form.addEventListener('submit', async (e) => {
                e.preventDefault();
                const r = await window.__authFetch('/api/auth/login', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password: input.value })
                });
                const d = await r.json().catch(() => ({}));
                if (r.ok) {
                    document.dispatchEvent(new CustomEvent('auth-changed', { detail: d }));
                    done(true);
                } else {
                    err.textContent = d.error || 'Login failed';
                    err.hidden = false;
                    input.select();
                }
            });
            input.focus();
        });
        return this.pending;
    },

    async status() {
        try { return await (await fetch('/api/auth/status')).json(); } catch (e) { return null; }
    },

    async logout() {
        const r = await fetch('/api/auth/logout', { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        document.dispatchEvent(new CustomEvent('auth-changed', { detail: d }));
        return d;
    },

    esc(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
};

Auth.install();
