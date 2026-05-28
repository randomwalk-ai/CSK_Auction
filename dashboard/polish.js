/**
 * UI polish — Fuse search, heroes, routes, auction-night theme.
 */
const CSKPolish = (() => {
    const DATALIST_ID = 'cskPlayerNames';
    const THEME_KEY = 'csk-theme';
    const VALID_TABS = new Set(['squad', 'bidadvisor', 'players', 'arena', 'compare', 'valuation']);

    let getPlayersFn = () => [];
    let fuseIndex = null;
    let fuseList = [];

    function setPlayersProvider(fn) {
        getPlayersFn = typeof fn === 'function' ? fn : () => [];
        rebuildFuse(getPlayers());
    }

    function getPlayers() {
        return getPlayersFn() || [];
    }

    function escapeAttr(str) {
        return String(str || '')
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function escapeHtml(str) {
        return escapeAttr(str);
    }

    function normalizeName(name) {
        return String(name || '').trim();
    }

    /** Stats DB abbreviations → auction-pool full names (keep in sync with api/player_loader.py). */
    const STATS_TO_POOL_ALIASES = {
        'dl chahar': 'deepak chahar',
        'rd chahar': 'rahul chahar',
        'sv samson': 'sanju samson',
        'rd gaikwad': 'ruturaj gaikwad',
        's dube': 'shivam dube',
        'v kohli': 'virat kohli',
        'jr hazlewood': 'josh hazlewood',
    };

    function resolvePoolPlayer(searchName, players) {
        const raw = normalizeName(searchName);
        if (!raw) return null;
        const list = resolvePlayersSource(players);
        if (!list.length) return null;
        const key = raw.toLowerCase();

        const exact = list.find(p => p.player_name.trim().toLowerCase() === key);
        if (exact) return exact;

        const aliasKey = STATS_TO_POOL_ALIASES[key];
        if (aliasKey) {
            const viaAlias = list.find(p => p.player_name.trim().toLowerCase() === aliasKey);
            if (viaAlias) return viaAlias;
        }

        function firstNameMatches(searchFirst, poolFirst) {
            const sf = searchFirst.toLowerCase();
            const pf = poolFirst.toLowerCase();
            if (!sf || !pf) return false;
            if (sf === pf) return true;
            if (sf.length >= 3 && (pf.startsWith(sf) || sf.startsWith(pf))) return true;
            if (sf.length <= 3 && pf.length <= 3 && sf === pf) return true;
            return false;
        }

        const parts = key.split(/\s+/).filter(Boolean);
        if (parts.length >= 2) {
            const last = parts[parts.length - 1];
            const first = parts[0];
            const bySurname = list.filter(p => {
                const np = p.player_name.trim().toLowerCase().split(/\s+/);
                if (np[np.length - 1] !== last) return false;
                return firstNameMatches(first, np[0]);
            });
            if (bySurname.length === 1) return bySurname[0];
        }

        if (key.length >= 3) {
            const contains = list.filter(p => p.player_name.toLowerCase().includes(key));
            if (contains.length === 1) return contains[0];
        }

        return null;
    }

    function poolMeta(name, players) {
        const key = normalizeName(name);
        const list = players || getPlayers();
        const p = resolvePoolPlayer(key, list) || list.find(x => x.player_name === key) || {};
        return {
            facecard_url: p.facecard_url || '',
            espn_portrait_url: p.espn_portrait_url || '',
            pool_role: p.pool_role || p.auction_role || '',
        };
    }

    function heroMarkup(name, players, portraitClass = 'player-hero__portrait') {
        if (!name || typeof CSKAvatars === 'undefined') return '';
        const m = poolMeta(name, players);
        return `<div class="player-hero">${CSKAvatars.markup(name, portraitClass, m.facecard_url, m.espn_portrait_url)}</div>`;
    }

    function loadingShell(message = 'Loading…') {
        return `
            <div class="async-shell" role="status" aria-live="polite">
                <div class="async-shell__shimmer" aria-hidden="true"></div>
                <div class="pp-spinner"></div>
                <p>${escapeHtml(message)}</p>
            </div>`;
    }

    function rebuildFuse(players) {
        fuseList = players || [];
        if (typeof Fuse === 'undefined' || !fuseList.length) {
            fuseIndex = null;
            return;
        }
        fuseIndex = new Fuse(fuseList, {
            keys: [
                { name: 'player_name', weight: 0.65 },
                { name: 'pool_role', weight: 0.15 },
                { name: 'auction_role', weight: 0.1 },
                { name: 'country', weight: 0.1 },
            ],
            threshold: 0.38,
            ignoreLocation: true,
            minMatchCharLength: 1,
        });
    }

    function resolvePlayersSource(source) {
        if (typeof source === 'function') return source() || [];
        if (Array.isArray(source)) return source;
        return getPlayers();
    }

    function searchPlayers(query, limit = 8, source) {
        const q = String(query || '').trim();
        const list = resolvePlayersSource(source);
        if (!q) return list.slice(0, limit);
        if (!list.length) return [];
        // Fast prefix / substring match for 1–2 chars (Fuse can be strict on short queries)
        if (q.length <= 2) {
            const lower = q.toLowerCase();
            const hits = list.filter(p => {
                const n = (p.player_name || '').toLowerCase();
                return n.includes(lower)
                    || n.split(/\s+/).some(w => w.startsWith(lower));
            });
            return hits.slice(0, limit);
        }
        if (fuseIndex && list === fuseList) {
            return fuseIndex.search(q, { limit }).map(r => r.item);
        }
        const fuse = typeof Fuse !== 'undefined'
            ? new Fuse(list, {
                keys: ['player_name', 'pool_role', 'auction_role', 'country'],
                threshold: 0.4,
                ignoreLocation: true,
            })
            : null;
        if (fuse) return fuse.search(q, { limit }).map(r => r.item);
        const lower = q.toLowerCase();
        return list.filter(p => {
            const n = (p.player_name || '').toLowerCase();
            return n.includes(lower)
                || (p.pool_role || '').toLowerCase().includes(lower)
                || (p.auction_role || '').toLowerCase().includes(lower);
        }).slice(0, limit);
    }

    function syncDatalist(players) {
        rebuildFuse(players || getPlayers());
        const names = [...new Set(fuseList.map(p => p.player_name).filter(Boolean))].sort(
            (a, b) => a.localeCompare(b),
        );
        let dl = document.getElementById(DATALIST_ID);
        if (!dl) {
            dl = document.createElement('datalist');
            dl.id = DATALIST_ID;
            document.body.appendChild(dl);
        }
        dl.innerHTML = names.map(n => `<option value="${escapeAttr(n)}"></option>`).join('');
        return dl;
    }

    function ensureFuzzyWrap(inputEl) {
        let wrap = inputEl.closest('.fuzzy-search');
        if (!wrap) {
            wrap = document.createElement('div');
            wrap.className = 'fuzzy-search';
            inputEl.parentNode?.insertBefore(wrap, inputEl);
            wrap.appendChild(inputEl);
        }
        let list = wrap.querySelector('.fuzzy-search__list');
        if (!list) {
            list = document.createElement('ul');
            list.className = 'fuzzy-search__list';
            list.setAttribute('role', 'listbox');
            list.hidden = true;
            wrap.appendChild(list);
        }
        return { wrap, list };
    }

    function wireFuzzySearch(inputEl, opts = {}) {
        if (!inputEl) return;
        const { list } = ensureFuzzyWrap(inputEl);
        const limit = opts.limit || 8;
        const source = opts.players ?? opts.source ?? null;
        const onSelect = typeof opts.onSelect === 'function' ? opts.onSelect : null;
        const onInput = typeof opts.onInput === 'function' ? opts.onInput : null;

        inputEl.setAttribute('autocomplete', 'off');
        inputEl.setAttribute('spellcheck', 'false');
        inputEl.setAttribute('autocapitalize', 'words');
        inputEl.removeAttribute('list');

        const hideList = () => {
            list.hidden = true;
            list.innerHTML = '';
        };

        const pick = (player) => {
            const name = player?.player_name || player;
            inputEl.value = name;
            inputEl.dataset.poolPick = name;
            hideList();
            if (onSelect) onSelect(name, player);
            else inputEl.dispatchEvent(new Event('change', { bubbles: true }));
        };

        const render = () => {
            const q = inputEl.value.trim();
            if (onInput) onInput(q);
            if (q.length < 1) {
                hideList();
                return;
            }
            const hits = searchPlayers(q, limit, source);
            if (!hits.length) {
                list.innerHTML = '<li class="fuzzy-search__empty">No matches</li>';
                list.hidden = false;
                return;
            }
            list.innerHTML = hits.map((p, i) => {
                const role = p.pool_role || p.auction_role || '';
                return `<li class="fuzzy-search__item" role="option" data-idx="${i}" tabindex="-1">
                    <span class="fuzzy-search__name">${escapeHtml(p.player_name)}</span>
                    <span class="fuzzy-search__meta">${escapeHtml(role)}</span>
                </li>`;
            }).join('');
            list.hidden = false;
            list._hits = hits;
        };

        inputEl.addEventListener('input', () => {
            delete inputEl.dataset.poolPick;
            render();
        });
        inputEl.addEventListener('focus', () => {
            if (inputEl.value.trim()) render();
        });

        list.addEventListener('mousedown', e => {
            e.preventDefault();
            const item = e.target.closest('.fuzzy-search__item');
            if (!item || !list._hits) return;
            const hit = list._hits[Number(item.dataset.idx)];
            if (hit) pick(hit);
        });

        inputEl.addEventListener('keydown', e => {
            const items = [...list.querySelectorAll('.fuzzy-search__item')];
            if (!items.length || list.hidden) return;
            const active = list.querySelector('.fuzzy-search__item--active');
            let idx = active ? items.indexOf(active) : -1;
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                idx = Math.min(idx + 1, items.length - 1);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                idx = Math.max(idx - 1, 0);
            } else if (e.key === 'Enter' && idx >= 0 && list._hits?.[idx]) {
                e.preventDefault();
                pick(list._hits[idx]);
                return;
            } else if (e.key === 'Escape') {
                hideList();
                return;
            } else {
                return;
            }
            items.forEach(el => el.classList.remove('fuzzy-search__item--active'));
            items[idx]?.classList.add('fuzzy-search__item--active');
        });

        document.addEventListener('click', e => {
            if (!inputEl.closest('.fuzzy-search')?.contains(e.target)) hideList();
        });
    }

    function playersSourceForWire(players) {
        if (typeof players === 'function') return players;
        if (Array.isArray(players) && players.length) return () => players;
        return getPlayers;
    }

    function wireAutocomplete(inputEl, players) {
        const source = playersSourceForWire(players);
        syncDatalist(resolvePlayersSource(source));
        if (typeof Fuse !== 'undefined') {
            wireFuzzySearch(inputEl, { players: source, limit: 12 });
            return;
        }
        inputEl.setAttribute('list', DATALIST_ID);
        inputEl.setAttribute('spellcheck', 'false');
        inputEl.setAttribute('autocapitalize', 'words');
    }

    function wireAutocompleteIds(ids, players) {
        syncDatalist(players);
        ids.forEach(id => wireAutocomplete(document.getElementById(id), players));
    }

    function bindHero(root) {
        if (!root || typeof CSKAvatars === 'undefined') return;
        CSKAvatars.bindAll(root, '[data-avatar]', { eager: true });
    }

    function parseRoute() {
        const params = new URLSearchParams(window.location.search);
        const tab = (params.get('tab') || '').trim().toLowerCase();
        const player = (params.get('player') || '').trim();
        const player1 = (params.get('player1') || player || '').trim();
        const player2 = (params.get('player2') || '').trim();
        return {
            tab: VALID_TABS.has(tab) ? tab : '',
            player,
            player1,
            player2,
        };
    }

    function updateRoute({ tab, player, player1, player2 } = {}) {
        const params = new URLSearchParams();
        if (tab && VALID_TABS.has(tab)) params.set('tab', tab);
        if (tab === 'compare') {
            const p1 = player1 || player;
            if (p1) params.set('player1', p1);
            if (player2) params.set('player2', player2);
        } else if (player || player1) {
            params.set('player', player || player1);
        }
        const qs = params.toString();
        const path = window.location.pathname || '/';
        const url = qs ? `${path}?${qs}` : path;
        window.history.replaceState({ tab, player, player1, player2 }, '', url);
    }

    function initTheme() {
        const saved = localStorage.getItem(THEME_KEY);
        const theme = saved === 'auction' ? 'auction' : 'day';
        document.documentElement.dataset.theme = theme;
        syncThemeToggle(theme);
    }

    function syncThemeToggle(theme) {
        const btn = document.getElementById('themeToggle');
        if (!btn) return;
        const on = theme === 'auction';
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        btn.textContent = on ? 'Day mode' : 'Auction night';
        btn.classList.toggle('meta-chip--theme-on', on);
    }

    function toggleTheme() {
        const next = document.documentElement.dataset.theme === 'auction' ? 'day' : 'auction';
        document.documentElement.dataset.theme = next;
        localStorage.setItem(THEME_KEY, next);
        syncThemeToggle(next);
    }

    return {
        setPlayersProvider,
        getPlayers,
        poolMeta,
        resolvePoolPlayer,
        heroMarkup,
        loadingShell,
        rebuildFuse,
        searchPlayers,
        syncDatalist,
        wireFuzzySearch,
        wireAutocomplete,
        wireAutocompleteIds,
        bindHero,
        parseRoute,
        updateRoute,
        initTheme,
        toggleTheme,
        VALID_TABS,
    };
})();
