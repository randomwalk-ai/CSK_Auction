/**
 * Player portraits — IPL CDN URL when available, else initials.
 * Arena: pass facecard_url from auction-pool (zero extra API calls per bubble).
 */
const CSKAvatars = (() => {
    const queue = [];
    let inFlight = 0;
    const MAX_CONCURRENT = 24;
    const CACHE_VERSION = 17;

    function apiBase() {
        return window.CSKDashboard?.API_BASE || 'http://127.0.0.1:8000/api';
    }

    function initials(name) {
        const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
        if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
        return (parts[0]?.[0] || '?').toUpperCase();
    }

    function metaUrl(name) {
        return `${apiBase()}/players/avatar?name=${encodeURIComponent(name)}&v=${CACHE_VERSION}`;
    }

    function applyLoaded(img, loadedClass, initialsEl) {
        img.classList.add(loadedClass);
        initialsEl?.classList.add('player-avatar__initials--hide');
        initialsEl?.classList.add('arena-bubble__initials--hide');
    }

    function showInitialsOnly(img, loadedClass, initialsEl, finish) {
        img.removeAttribute('src');
        img.classList.remove(loadedClass);
        img.classList.remove('arena-bubble__face--loaded');
        initialsEl?.classList.remove('player-avatar__initials--hide');
        initialsEl?.classList.remove('arena-bubble__initials--hide');
        if (finish) finish();
    }

    function loadDirectUrl(img, url, loadedClass, initialsEl, finish) {
        img.addEventListener('load', () => {
            applyLoaded(img, loadedClass, initialsEl);
            if (finish) finish();
        }, { once: true });
        img.addEventListener('error', () => {
            showInitialsOnly(img, loadedClass, initialsEl, finish);
        }, { once: true });
        img.src = url;
    }

    function enqueue(img) {
        if (!img?.dataset?.avatarName || img.dataset.avatarLoaded === '1') return;
        if (img.dataset.avatarQueued === '1') return;
        img.dataset.avatarQueued = '1';
        queue.push({ img, retries: 0 });
        drainQueue();
    }

    function drainQueue() {
        while (inFlight < MAX_CONCURRENT && queue.length) {
            const job = queue.shift();
            const img = job?.img;
            if (!img?.dataset?.avatarName || img.dataset.avatarLoaded === '1') continue;

            inFlight += 1;
            const finish = () => {
                inFlight -= 1;
                drainQueue();
            };

            const name = img.dataset.avatarName;
            const presetUrl = (img.dataset.avatarUrl || '').trim();
            const initialsEl = img.parentElement?.querySelector(
                '.player-avatar__initials, .arena-bubble__initials',
            );
            const loadedClass = img.classList.contains('arena-bubble__face')
                ? 'arena-bubble__face--loaded'
                : 'player-avatar--loaded';

            if (presetUrl.startsWith('https://')) {
                loadDirectUrl(img, presetUrl, loadedClass, initialsEl, finish);
                continue;
            }

            fetch(metaUrl(name), { cache: 'default' })
                .then(res => (res.ok ? res.json() : null))
                .then(data => {
                    const url = (data?.url || '').trim();
                    const source = (data?.source || '').toLowerCase();
                    if (source === 'iplt20' && url.startsWith('https://')) {
                        loadDirectUrl(img, url, loadedClass, initialsEl, finish);
                        return;
                    }
                    showInitialsOnly(img, loadedClass, initialsEl, finish);
                })
                .catch(() => {
                    if ((job.retries || 0) < 1) {
                        job.retries = (job.retries || 0) + 1;
                        finish();
                        setTimeout(() => {
                            img.dataset.avatarQueued = '0';
                            enqueue(img);
                        }, 600);
                        return;
                    }
                    showInitialsOnly(img, loadedClass, initialsEl, finish);
                });
        }
    }

    /**
     * @param {HTMLImageElement} img
     * @param {string} name
     * @param {{ loadedClass?: string, initialsEl?: HTMLElement|null, eager?: boolean, facecardUrl?: string }} [opts]
     */
    function bind(img, name, opts = {}) {
        if (!img || !name) return;
        const loadedClass = opts.loadedClass || 'player-avatar--loaded';
        const initialsEl = opts.initialsEl ?? img.parentElement?.querySelector(
            '.player-avatar__initials, .arena-bubble__initials',
        );

        img.dataset.avatarName = name;
        if (opts.facecardUrl) {
            img.dataset.avatarUrl = opts.facecardUrl;
        } else {
            delete img.dataset.avatarUrl;
        }
        img.decoding = 'async';
        img.loading = opts.eager ? 'eager' : 'lazy';
        img.alt = img.alt || name;

        img.classList.remove(loadedClass);
        img.classList.remove('arena-bubble__face--loaded');
        initialsEl?.classList.remove('player-avatar__initials--hide');
        initialsEl?.classList.remove('arena-bubble__initials--hide');

        enqueue(img);
    }

    function bindAll(root, selector = '[data-avatar]', opts = {}) {
        if (!root) return;
        root.querySelectorAll(selector).forEach(el => {
            const name = el.getAttribute('data-avatar');
            const facecardUrl = el.getAttribute('data-avatar-url') || '';
            const loadedClass = el.classList.contains('arena-bubble__face')
                ? 'arena-bubble__face--loaded'
                : 'player-avatar--loaded';
            bind(el, name, { loadedClass, eager: opts.eager, facecardUrl });
        });
    }

    /** HTML for a circular avatar with initials fallback. */
    function markup(name, extraClass = '', facecardUrl = '') {
        const safe = String(name || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
        const urlAttr = facecardUrl
            ? ` data-avatar-url="${String(facecardUrl).replace(/"/g, '&quot;')}"`
            : '';
        const ini = initials(name);
        const wrap = extraClass ? `player-avatar ${extraClass}` : 'player-avatar';
        return `
            <div class="${wrap}">
                <img class="player-avatar__img" data-avatar="${safe}"${urlAttr} alt="" loading="eager" decoding="async">
                <span class="player-avatar__initials">${ini}</span>
            </div>`;
    }

    function clearQueue() {
        queue.length = 0;
    }

    return { bind, bindAll, markup, initials, clearQueue };
})();
