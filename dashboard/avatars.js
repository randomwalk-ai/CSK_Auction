/**
 * Player portraits — unified shell + loading shimmer everywhere.
 * Chain: IPL facecard CDN → proxy → ESPN → initials.
 */
const CSKAvatars = (() => {
    const queue = [];
    let inFlight = 0;
    const MAX_CONCURRENT = 24;
    const CACHE_VERSION = 24;

    const CDN_SOURCES = new Set(['iplt20', 'espncricinfo']);

    function apiBase() {
        return window.CSKDashboard?.API_BASE || 'http://127.0.0.1:8000/api';
    }

    function apiRoot() {
        return apiBase().replace(/\/api\/?$/, '');
    }

    function resolveAssetUrl(pathOrUrl) {
        const raw = String(pathOrUrl || '').trim();
        if (!raw) return '';
        if (raw.startsWith('https://') || raw.startsWith('http://')) return raw;
        return `${apiRoot()}${raw.startsWith('/') ? raw : `/${raw}`}`;
    }

    function initials(name) {
        const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
        if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
        return (parts[0]?.[0] || '?').toUpperCase();
    }

    function facecardProxyUrl(name) {
        return resolveAssetUrl(
            `/api/players/facecard/img?name=${encodeURIComponent(name)}&v=${CACHE_VERSION}`,
        );
    }

    function metaUrl(name, { espnFallback = false } = {}) {
        let url = `${apiBase()}/players/avatar?name=${encodeURIComponent(name)}&v=${CACHE_VERSION}`;
        if (espnFallback) url += '&fallback=espn';
        return url;
    }

    function avatarImgUrl(name) {
        return resolveAssetUrl(`/api/players/avatar/img?name=${encodeURIComponent(name)}&v=${CACHE_VERSION}`);
    }

    function portraitRoot(img) {
        return img?.closest?.('.portrait-shell, .player-avatar, .arena-bubble__face-wrap');
    }

    function bubbleRoot(img) {
        return img?.closest?.('.arena-bubble') || null;
    }

    function setPortraitLoading(img, loading) {
        const root = portraitRoot(img);
        if (!root) return;
        root.classList.toggle('portrait-shell--loading', !!loading);
        root.classList.toggle('portrait-shell--loaded', !loading && img?.dataset?.avatarLoaded === '1');
    }

    function setPortraitState(img, state) {
        const bubble = bubbleRoot(img);
        if (!bubble) return;
        bubble.classList.toggle('arena-bubble--portrait-hit', state === 'hit');
        bubble.classList.toggle('arena-bubble--portrait-miss', state === 'miss');
    }

    function applyLoaded(img, loadedClass, initialsEl) {
        img.dataset.avatarLoaded = '1';
        img.classList.add(loadedClass);
        initialsEl?.classList.add('player-avatar__initials--hide');
        initialsEl?.classList.add('arena-bubble__initials--hide');
        setPortraitLoading(img, false);
        setPortraitState(img, 'hit');
    }

    function showInitialsOnly(img, loadedClass, initialsEl, finish) {
        img.removeAttribute('src');
        img.classList.remove(loadedClass);
        img.classList.remove('arena-bubble__face--loaded');
        initialsEl?.classList.remove('player-avatar__initials--hide');
        initialsEl?.classList.remove('arena-bubble__initials--hide');
        setPortraitLoading(img, false);
        setPortraitState(img, 'miss');
        if (finish) finish();
    }

    function loadDirectUrl(img, url, loadedClass, initialsEl, finish, onError) {
        const done = typeof finish === 'function' ? finish : () => {};
        let settled = false;
        const settle = (fn) => {
            if (settled) return;
            settled = true;
            fn();
        };

        const fail = () => settle(() => {
            if (typeof onError === 'function') {
                onError();
                return;
            }
            showInitialsOnly(img, loadedClass, initialsEl, done);
        });

        const onLoad = () => settle(() => {
            applyLoaded(img, loadedClass, initialsEl);
            done();
        });

        img.addEventListener('load', onLoad, { once: true });
        img.addEventListener('error', fail, { once: true });
        img.referrerPolicy = 'no-referrer';
        img.src = url;
        if (img.complete && img.naturalWidth > 0) {
            onLoad();
        }
    }

    function loadPortraitProxy(img, name, loadedClass, initialsEl, finish) {
        loadDirectUrl(img, avatarImgUrl(name), loadedClass, initialsEl, finish);
    }

    function loadFromMeta(img, name, loadedClass, initialsEl, finish, { espnFallback = false } = {}) {
        fetch(metaUrl(name, { espnFallback }), { cache: 'default' })
            .then(res => (res.ok ? res.json() : null))
            .then(data => {
                const source = (data?.source || '').toLowerCase();
                const url = resolveAssetUrl(data?.url || data?.img_url || '');

                if (url && (CDN_SOURCES.has(source) || url.includes('/facecard/img'))) {
                    loadDirectUrl(img, url, loadedClass, initialsEl, finish, () => {
                        if (!espnFallback && source === 'iplt20') {
                            loadFromMeta(img, name, loadedClass, initialsEl, finish, { espnFallback: true });
                            return;
                        }
                        loadPortraitProxy(img, name, loadedClass, initialsEl, finish);
                    });
                    return;
                }

                if (url) {
                    loadDirectUrl(img, url, loadedClass, initialsEl, finish, () => {
                        showInitialsOnly(img, loadedClass, initialsEl, finish);
                    });
                    return;
                }

                showInitialsOnly(img, loadedClass, initialsEl, finish);
            })
            .catch(() => loadPortraitProxy(img, name, loadedClass, initialsEl, finish));
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
            const hasFacecard = img.dataset.hasFacecard === '1';
            const initialsEl = img.parentElement?.querySelector(
                '.player-avatar__initials, .arena-bubble__initials',
            );
            const loadedClass = img.classList.contains('arena-bubble__face')
                ? 'arena-bubble__face--loaded'
                : 'player-avatar--loaded';

            img.dataset.avatarLoaded = '0';
            setPortraitLoading(img, true);
            setPortraitState(img, 'miss');

            if (presetUrl) {
                const proxyUrl = (img.dataset.facecardProxy || '').trim();
                loadDirectUrl(img, presetUrl, loadedClass, initialsEl, finish, () => {
                    if (hasFacecard && proxyUrl && proxyUrl !== presetUrl) {
                        loadDirectUrl(img, proxyUrl, loadedClass, initialsEl, finish, () => {
                            loadFromMeta(img, name, loadedClass, initialsEl, finish, { espnFallback: true });
                        });
                        return;
                    }
                    loadFromMeta(img, name, loadedClass, initialsEl, finish, { espnFallback: true });
                });
                continue;
            }

            loadFromMeta(img, name, loadedClass, initialsEl, finish);
        }
    }

    /**
     * Unified circular portrait markup (Scout, Squad, Arena scout, chips).
     */
    function markup(name, extraClass = '', facecardUrl = '', espnPortraitUrl = '') {
        const safe = String(name || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
        const fc = String(facecardUrl || '').replace(/"/g, '&quot;');
        const espn = String(espnPortraitUrl || '').replace(/"/g, '&quot;');
        const urlAttr = fc ? ` data-avatar-url="${fc}"` : '';
        const espnAttr = espn ? ` data-espn-url="${espn}"` : '';
        const ini = initials(name);
        const shellClass = extraClass
            ? `portrait-shell player-avatar ${extraClass}`
            : 'portrait-shell player-avatar';
        return `
            <div class="${shellClass}">
                <span class="portrait-skeleton" aria-hidden="true"></span>
                <img class="player-avatar__img" data-avatar="${safe}"${urlAttr}${espnAttr} alt="" loading="eager" decoding="async">
                <span class="player-avatar__initials">${ini}</span>
            </div>`;
    }

    /**
     * @param {HTMLImageElement} img
     * @param {string} name
     * @param {{ loadedClass?: string, initialsEl?: HTMLElement|null, eager?: boolean, facecardUrl?: string, espnPortraitUrl?: string }} [opts]
     */
    function bind(img, name, opts = {}) {
        if (!img || !name) return;
        img.dataset.avatarQueued = '0';
        const loadedClass = opts.loadedClass || 'player-avatar--loaded';
        const initialsEl = opts.initialsEl ?? img.parentElement?.querySelector(
            '.player-avatar__initials, .arena-bubble__initials',
        );
        const facecardUrl = (opts.facecardUrl || '').trim();
        const espnPortraitUrl = (opts.espnPortraitUrl || '').trim();

        img.dataset.avatarName = name;
        img.dataset.avatarLoaded = '0';
        img.dataset.avatarQueued = '0';
        if (facecardUrl.startsWith('https://')) {
            img.dataset.avatarUrl = facecardUrl;
            img.dataset.facecardProxy = facecardProxyUrl(name);
            img.dataset.hasFacecard = '1';
        } else if (espnPortraitUrl.startsWith('https://')) {
            img.dataset.avatarUrl = espnPortraitUrl;
            img.dataset.hasFacecard = '0';
        } else {
            delete img.dataset.avatarUrl;
            delete img.dataset.facecardProxy;
            img.dataset.hasFacecard = '0';
        }
        img.decoding = 'async';
        img.loading = opts.eager ? 'eager' : 'lazy';
        img.alt = img.alt || name;

        img.classList.remove(loadedClass);
        img.classList.remove('arena-bubble__face--loaded');
        initialsEl?.classList.remove('player-avatar__initials--hide');
        initialsEl?.classList.remove('arena-bubble__initials--hide');
        setPortraitLoading(img, true);
        setPortraitState(img, 'miss');

        enqueue(img);
    }

    function bindAll(root, selector = '[data-avatar]', opts = {}) {
        if (!root) return;
        root.querySelectorAll(selector).forEach(el => {
            const name = el.getAttribute('data-avatar');
            const facecardUrl = el.getAttribute('data-avatar-url') || '';
            const espnPortraitUrl = el.getAttribute('data-espn-url') || '';
            const loadedClass = el.classList.contains('arena-bubble__face')
                ? 'arena-bubble__face--loaded'
                : 'player-avatar--loaded';
            bind(el, name, { loadedClass, eager: opts.eager, facecardUrl, espnPortraitUrl });
        });
    }

    function clearQueue() {
        queue.length = 0;
    }

    return { bind, bindAll, markup, initials, clearQueue };
})();
