/**
 * Auction Arena — floating bubbles (no overlap), bid-sized, API portraits.
 */

const Arena = (() => {
    const IDEAL = { Batter: 6, Bowler: 6, 'All Rounder': 5, 'Wicket Keeper': 2 };
    const MAX_OVERSEAS = 8;
    const MAX_SQUAD = 25;
    const FLEX_SLOTS = MAX_SQUAD - Object.values(IDEAL).reduce((sum, n) => sum + n, 0);

    let poolPlayers = [];
    let arenaSquad = [];
    /** @type {Map<string, object>} */
    let bubbleStates = new Map();
    let priceRange = { min: 0.5, max: 16 };
    let animFrame = null;
    let poolEl = null;
    let squadEl = null;
    let poolFilterQuery = '';
    let scoutPanelOpen = false;
    let resizeObserver = null;
    let mounted = false;
    let scoutOutsideClickHandler = null;
    let forceCanvasResample = true;

    function apiBase() {
        return window.CSKDashboard?.API_BASE || 'http://127.0.0.1:8000/api';
    }

    function purseCap() {
        return window.CSKDashboard?.IPL_PURSE_CR || 125;
    }

    function auctionYear() {
        return window.CSKDashboard?.IPL_AUCTION_YEAR || 2026;
    }

    function getInitials(name) {
        const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
        if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
        return (parts[0]?.[0] || '?').toUpperCase();
    }

    function normalizeRole(role) {
        const r = (role || '').trim();
        if (/WK|Wicket/i.test(r)) return 'Wicket Keeper';
        if (/All/i.test(r)) return 'All Rounder';
        if (/Bowl/i.test(r)) return 'Bowler';
        if (/Bat/i.test(r)) return 'Batter';
        return r || 'Unknown';
    }

    function roleKey(role) {
        const n = normalizeRole(role);
        if (n === 'Wicket Keeper') return 'wk';
        if (n === 'All Rounder') return 'ar';
        if (n === 'Bowler') return 'bowl';
        return 'bat';
    }

    function playerPrice(player) {
        return player.bubble_price_cr || player.last_bid_cr || 2;
    }

    function refreshPriceRange(players) {
        const prices = players.map(playerPrice).filter(p => p > 0);
        if (!prices.length) return;
        priceRange.min = Math.min(...prices);
        priceRange.max = Math.max(...prices);
    }

    function priceToUnit(priceCr) {
        const floor = 0.08;
        const p = Math.max(priceRange.min, Math.min(priceRange.max, priceCr || priceRange.min));
        const lo = Math.log(Math.max(floor, priceRange.min + floor));
        const hi = Math.log(priceRange.max + floor);
        const span = hi - lo;
        if (span <= 0) return 0.5;
        const linear = (Math.log(p + floor) - lo) / span;
        const clamped = Math.max(0, Math.min(1, linear));
        return Math.pow(clamped, 1.45);
    }

    function bubbleSize(priceCr) {
        const MIN_PX = 56;
        const MAX_PX = 152;
        const t = priceToUnit(priceCr);
        return Math.round(MIN_PX + t * (MAX_PX - MIN_PX));
    }

    function bubbleTier(priceCr) {
        if (priceCr >= 15) return 'mega';
        if (priceCr >= 8) return 'premium';
        if (priceCr >= 3) return 'mid';
        return 'base';
    }

    function bindAvatarImg(img, name, initialsEl, facecardUrl) {
        CSKAvatars.bind(img, name, {
            loadedClass: 'arena-bubble__face--loaded',
            initialsEl,
            eager: true,
            facecardUrl: facecardUrl || '',
        });
    }

    function removePoolBubble(name) {
        bubbleStates.delete(name);
        if (!poolEl) return;
        poolEl.querySelectorAll(`.arena-bubble[data-player="${CSS.escape(name)}"]`).forEach(el => el.remove());
    }

    function spentCr() {
        return arenaSquad.reduce((s, p) => s + (p.price || 0), 0);
    }

    function remainingCr() {
        return Math.max(0, purseCap() - spentCr());
    }

    function overseasCount() {
        return arenaSquad.filter(p => p.overseas).length;
    }

    function squadSlotAssignment() {
        const buckets = { Batter: [], Bowler: [], 'All Rounder': [], 'Wicket Keeper': [] };
        const flex = [];
        arenaSquad.forEach(p => {
            const role = normalizeRole(p.pool_role || p.role);
            if (buckets[role] !== undefined && buckets[role].length < IDEAL[role]) {
                buckets[role].push(p);
            } else {
                flex.push(p);
            }
        });
        return { buckets, flex };
    }

    function squadGaps() {
        const counts = { Batter: 0, Bowler: 0, 'All Rounder': 0, 'Wicket Keeper': 0 };
        arenaSquad.forEach(p => {
            const r = normalizeRole(p.pool_role || p.role);
            if (counts[r] !== undefined) counts[r]++;
        });
        const gaps = [];
        Object.entries(IDEAL).forEach(([role, ideal]) => {
            const have = counts[role] || 0;
            const need = ideal - have;
            if (need > 0) gaps.push({ role, have, ideal, need, critical: need >= 2 });
        });
        return { counts, gaps, overseas: overseasCount() };
    }

    function syncDashboardKpis() {
        const spent = spentCr();
        const rem = remainingCr();
        if (typeof updateExecutiveKpis === 'function') {
            updateExecutiveKpis(spent, rem, arenaSquad.length, 0);
        }
        const spentEl = document.getElementById('spentAmount');
        const remainEl = document.getElementById('remainingAmount');
        if (spentEl) spentEl.textContent = `₹${spent.toFixed(2)} Cr`;
        if (remainEl) remainEl.textContent = `₹${rem.toFixed(2)} Cr`;
        const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
        set('kpiSquadSize', `${arenaSquad.length} / ${MAX_SQUAD}`);
        const g = squadGaps();
        const pills = document.getElementById('kpiRolePills');
        if (pills) {
            pills.innerHTML = `
                <span class="role-pill role-pill--bat">BAT ${g.counts.Batter || 0}</span>
                <span class="role-pill role-pill--bowl">BOWL ${g.counts.Bowler || 0}</span>
                <span class="role-pill role-pill--ar">AR ${g.counts['All Rounder'] || 0}</span>
                <span class="role-pill role-pill--wk">WK ${g.counts['Wicket Keeper'] || 0}</span>`;
        }
        set('kpiOverseas', `${g.overseas} / ${MAX_OVERSEAS}`);
    }

    function renderGapPanel() {
        const el = document.getElementById('arenaGapPanel');
        if (!el) return;
        const g = squadGaps();
        const { buckets, flex } = squadSlotAssignment();

        const slotHtml = Object.entries(IDEAL).map(([role, ideal]) => {
            const filled = buckets[role] || [];
            const empty = ideal - filled.length;
            const rk = roleKey(role);
            let chips = filled.map(p => `
                <div class="arena-slot-chip arena-slot-chip--${rk}" title="${escapeAttr(p.player_name)}">
                    <img class="arena-slot-chip__img" data-avatar="${escapeAttr(p.player_name)}" data-avatar-url="${escapeAttr(p.facecard_url || '')}" alt="">
                    <span>${p.player_name.split(' ').pop()}</span>
                    <button type="button" class="arena-slot-chip__x" data-release="${escapeAttr(p.player_name)}" aria-label="Release">×</button>
                </div>`).join('');
            for (let i = 0; i < empty; i++) {
                chips += `<div class="arena-slot-empty arena-slot-empty--${rk}">Empty</div>`;
            }
            return `
                <div class="arena-slot-block arena-slot-block--${rk}">
                    <div class="arena-slot-head">
                        <span>${role}</span>
                        <strong>${filled.length}/${ideal}</strong>
                    </div>
                    <div class="arena-slot-row">${chips}</div>
                </div>`;
        }).join('');

        const flexEmpty = Math.max(0, FLEX_SLOTS - flex.length);
        let flexChips = flex.map(p => {
            const rk = roleKey(p.pool_role || p.role);
            return `
                <div class="arena-slot-chip arena-slot-chip--${rk}" title="${escapeAttr(p.player_name)}">
                    <img class="arena-slot-chip__img" data-avatar="${escapeAttr(p.player_name)}" data-avatar-url="${escapeAttr(p.facecard_url || '')}" alt="">
                    <span>${p.player_name.split(' ').pop()}</span>
                    <button type="button" class="arena-slot-chip__x" data-release="${escapeAttr(p.player_name)}" aria-label="Release">×</button>
                </div>`;
        }).join('');
        for (let i = 0; i < flexEmpty; i++) {
            flexChips += '<div class="arena-slot-empty arena-slot-empty--flex">Empty</div>';
        }

        el.innerHTML = `
            <div class="arena-stats-row">
                <div class="arena-stat"><span>Purse left</span><strong>₹${remainingCr().toFixed(1)} Cr</strong></div>
                <div class="arena-stat"><span>Squad</span><strong>${arenaSquad.length}/${MAX_SQUAD}</strong></div>
                <div class="arena-stat"><span>Overseas</span><strong>${g.overseas}/${MAX_OVERSEAS}</strong></div>
            </div>
            <p class="arena-mix-hint">Role targets (19) + ${FLEX_SLOTS} flex slots = ${MAX_SQUAD} max squad</p>
            <div class="arena-slots">${slotHtml}
                <div class="arena-slot-block arena-slot-block--flex">
                    <div class="arena-slot-head">
                        <span>Flex / overflow</span>
                        <strong>${flex.length}/${FLEX_SLOTS}</strong>
                    </div>
                    <div class="arena-slot-row">${flexChips}</div>
                </div>
            </div>`;

        el.querySelectorAll('[data-release]').forEach(btn => {
            btn.addEventListener('click', () => releasePlayer(btn.getAttribute('data-release')));
        });
        el.querySelectorAll('[data-avatar]').forEach(img => {
            CSKAvatars.bind(img, img.getAttribute('data-avatar'), { loadedClass: 'player-avatar--loaded' });
        });
    }

    function escapeAttr(s) {
        return String(s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
    }

    function escapeHtml(s) {
        return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function poolVisible() {
        const picked = new Set(arenaSquad.map(p => p.player_name));
        return poolPlayers.filter(p => {
            if (picked.has(p.player_name)) return false;
            if (!poolFilterQuery) return true;
            return p.player_name.toLowerCase().includes(poolFilterQuery.toLowerCase());
        });
    }

    function shuffled(items) {
        const list = [...items];
        for (let i = list.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [list[i], list[j]] = [list[j], list[i]];
        }
        return list;
    }

    function scoutCandidates(query) {
        const q = (query || '').trim().toLowerCase();
        const picked = new Set(arenaSquad.map(p => p.player_name));
        return poolPlayers.filter(p => {
            if (picked.has(p.player_name)) return false;
            if (!q) return true;
            return p.player_name.toLowerCase().includes(q)
                || (p.pool_role || '').toLowerCase().includes(q)
                || (p.auction_role || '').toLowerCase().includes(q);
        }).slice(0, 20);
    }

    function createPoolBubble(player) {
        const price = playerPrice(player);
        const size = bubbleSize(price);
        const tier = bubbleTier(price);
        const roleSrc = player.pool_role || player.auction_role || player.role;
        const rk = roleKey(roleSrc);
        const fullName = player.player_name;
        const el = document.createElement('div');
        el.className = `arena-bubble arena-bubble--${rk} arena-bubble--tier-${tier}`;
        el.dataset.player = fullName;
        el.dataset.fullName = fullName;
        el.dataset.priceCr = String(price);
        el.setAttribute('data-full-name', fullName);
        el.setAttribute('aria-label', fullName);
        el.draggable = true;
        el.style.width = `${size}px`;
        el.style.height = `${size}px`;
        el.style.zIndex = String(3 + Math.round(priceToUnit(price) * 14));
        el.innerHTML = `
            <div class="arena-bubble__face-wrap">
                <img class="arena-bubble__face" alt="" draggable="false">
                <span class="arena-bubble__initials">${getInitials(fullName)}</span>
                <div class="arena-bubble__overlay">
                    <div class="arena-bubble__foot">
                        <span class="arena-bubble__price">₹${Number(price).toFixed(1)}<span class="arena-bubble__price-unit"> Cr</span></span>
                    </div>
                </div>
            </div>
            <div class="arena-bubble__head" aria-hidden="true">
                <span class="arena-bubble__hover-name">${escapeHtml(fullName)}</span>
            </div>`;

        const img = el.querySelector('.arena-bubble__face');
        const initials = el.querySelector('.arena-bubble__initials');
        bindAvatarImg(img, player.player_name, initials, player.facecard_url);

        el.addEventListener('dragstart', e => {
            e.dataTransfer.setData('text/plain', player.player_name);
            e.dataTransfer.effectAllowed = 'move';
            el.classList.add('arena-bubble--dragging');
            bubbleStates.get(player.player_name)?.pause();
            requestAnimationFrame(() => { el.style.visibility = 'hidden'; });
        });
        el.addEventListener('dragend', () => {
            el.classList.remove('arena-bubble--dragging');
            if (!el.isConnected) return;
            if (arenaSquad.some(p => p.player_name === player.player_name)) {
                removePoolBubble(player.player_name);
                return;
            }
            el.style.visibility = '';
            bubbleStates.get(player.player_name)?.resume();
        });
        el.addEventListener('dblclick', () => {
            window.CSKDashboard?.openPlayerPreview?.(player.player_name);
        });

        return el;
    }

    function placementGap(sizeA, sizeB) {
        return (sizeA + sizeB) / 2 + 8;
    }

    function isPlacementClear(x, y, size, placed) {
        for (const other of placed) {
            if (Math.hypot(x - other.x, y - other.y) < placementGap(size, other.size)) return false;
        }
        return true;
    }

    function tryRandomPosition(size, W, H, placed) {
        const topPad = 34;
        const pad = 6;
        const minX = size / 2 + pad;
        const maxX = W - size / 2 - pad;
        const minY = topPad + size / 2 + pad;
        const maxY = H - size / 2 - pad;
        if (minX >= maxX || minY >= maxY) return null;

        for (let attempt = 0; attempt < 140; attempt++) {
            const x = minX + Math.random() * (maxX - minX);
            const y = minY + Math.random() * (maxY - minY);
            if (isPlacementClear(x, y, size, placed)) return { x, y };
        }
        return null;
    }

    /** Pick a random subset of players that fit on canvas without overlapping. */
    function layoutCanvasSample(players, W, H, { preferNames = [] } = {}) {
        const byName = new Map(players.map(p => [p.player_name, p]));
        const layout = [];
        const placed = [];
        const used = new Set();

        const tryAdd = player => {
            if (!player || used.has(player.player_name)) return false;
            const size = bubbleSize(playerPrice(player));
            const pos = tryRandomPosition(size, W, H, placed);
            if (!pos) return false;
            layout.push({ player, size, x: pos.x, y: pos.y });
            placed.push({ x: pos.x, y: pos.y, size });
            used.add(player.player_name);
            return true;
        };

        preferNames.forEach(name => tryAdd(byName.get(name)));

        const rest = shuffled(players.filter(p => !used.has(p.player_name)));
        for (const player of rest) tryAdd(player);

        return layout;
    }

    function applyBubblePos(state) {
        const half = state.size / 2;
        state.el.style.left = `${state.x - half}px`;
        state.el.style.top = `${state.y - half}px`;
    }

    function resolveCollisions(iterations = 4) {
        const list = [...bubbleStates.values()].filter(s => s.el?.isConnected && !s.paused);
        for (let pass = 0; pass < iterations; pass++) {
            for (let i = 0; i < list.length; i++) {
                for (let j = i + 1; j < list.length; j++) {
                    const a = list[i];
                    const b = list[j];
                    let dx = b.x - a.x;
                    let dy = b.y - a.y;
                    const dist = Math.hypot(dx, dy) || 0.001;
                    const minDist = placementGap(a.size, b.size);
                    if (dist >= minDist) continue;
                    const push = (minDist - dist) / 2;
                    dx /= dist;
                    dy /= dist;
                    a.x -= dx * push;
                    a.y -= dy * push;
                    b.x += dx * push;
                    b.y += dy * push;
                    if (!a.paused) {
                        a.vx -= dx * 0.04;
                        a.vy -= dy * 0.04;
                    }
                    if (!b.paused) {
                        b.vx += dx * 0.04;
                        b.vy += dy * 0.04;
                    }
                }
            }
        }
    }

    function clampToPool(state, W, H) {
        const pad = state.size / 2 + 5;
        const topPad = 32;
        if (state.x < pad) { state.x = pad; state.vx = Math.abs(state.vx) * 0.9; }
        if (state.x > W - pad) { state.x = W - pad; state.vx = -Math.abs(state.vx) * 0.9; }
        if (state.y < topPad + pad) { state.y = topPad + pad; state.vy = Math.abs(state.vy) * 0.9; }
        if (state.y > H - pad) { state.y = H - pad; state.vy = -Math.abs(state.vy) * 0.9; }
    }

    function tickPhysics() {
        if (!poolEl || !mounted) return;
        const W = poolEl.clientWidth;
        const H = poolEl.clientHeight;

        bubbleStates.forEach(state => {
            if (state.paused || !state.el.isConnected) return;
            state.x += state.vx;
            state.y += state.vy;
            state.vx += (Math.random() - 0.5) * 0.008;
            state.vy += (Math.random() - 0.5) * 0.008;
            const speed = Math.hypot(state.vx, state.vy);
            const maxSpeed = 0.38;
            if (speed > maxSpeed) {
                state.vx = (state.vx / speed) * maxSpeed;
                state.vy = (state.vy / speed) * maxSpeed;
            }
            clampToPool(state, W, H);
        });

        resolveCollisions(5);
        bubbleStates.forEach(state => {
            if (state.paused || !state.el.isConnected) return;
            clampToPool(state, W, H);
            applyBubblePos(state);
        });

        animFrame = requestAnimationFrame(tickPhysics);
    }

    function setupDropZone(el, onDrop) {
        el.addEventListener('dragover', e => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            el.classList.add('arena-pane--dragover');
        });
        el.addEventListener('dragleave', e => {
            if (!el.contains(e.relatedTarget)) el.classList.remove('arena-pane--dragover');
        });
        el.addEventListener('drop', e => {
            e.preventDefault();
            el.classList.remove('arena-pane--dragover');
            const name = e.dataTransfer.getData('text/plain');
            if (name) onDrop(name);
        });
    }

    function isOverseas(country) {
        const c = (country || 'India').toLowerCase();
        return c !== '' && c !== 'india' && c !== 'indian' && c !== 'ind';
    }

    function acquirePlayer(name) {
        if (arenaSquad.some(p => p.player_name === name)) return;
        if (arenaSquad.length >= MAX_SQUAD) {
            flashArenaMessage('Squad full (25 max)');
            return;
        }
        const player = poolPlayers.find(p => p.player_name === name);
        if (!player) return;
        const price = playerPrice(player);
        if (price > remainingCr()) {
            flashArenaMessage(`Need ₹${price.toFixed(1)} Cr · only ₹${remainingCr().toFixed(1)} left`);
            return;
        }
        if (isOverseas(player.country) && overseasCount() >= MAX_OVERSEAS) {
            flashArenaMessage('Overseas slots full (8 max)');
            return;
        }

        arenaSquad.push({
            ...player,
            price,
            role: normalizeRole(player.pool_role || player.auction_role),
            overseas: isOverseas(player.country),
        });

        removePoolBubble(name);
        backfillCanvasBubble();

        squadEl?.querySelector('.arena-drop-hint')?.remove();
        syncDashboardKpis();
        renderGapPanel();
        updatePoolCount();
        renderScoutPanel(document.getElementById('arenaScoutInput')?.value || '');
        flashArenaMessage(`✓ ${name} · ₹${price.toFixed(1)} Cr`);
    }

    function releasePlayer(name) {
        if (!arenaSquad.some(p => p.player_name === name)) return;
        arenaSquad = arenaSquad.filter(p => p.player_name !== name);
        const player = poolPlayers.find(p => p.player_name === name);
        if (player && poolEl && !bubbleStates.has(name)) {
            const W = poolEl.clientWidth;
            const H = poolEl.clientHeight;
            const placed = [...bubbleStates.values()].map(s => ({ x: s.x, y: s.y, size: s.size }));
            const size = bubbleSize(playerPrice(player));
            const pos = tryRandomPosition(size, W, H, placed);
            if (pos) {
                const el = createPoolBubble(player);
                poolEl.appendChild(el);
                bubbleStates.set(player.player_name, {
                    name: player.player_name,
                    size,
                    x: pos.x,
                    y: pos.y,
                    vx: (Math.random() - 0.5) * 0.32,
                    vy: (Math.random() - 0.5) * 0.32,
                    paused: false,
                    el,
                    pause() { this.paused = true; },
                    resume() { this.paused = false; },
                });
                applyBubblePos(bubbleStates.get(player.player_name));
                resolveCollisions(8);
            }
        }
        if (arenaSquad.length === 0) renderSquadDropHint();
        syncDashboardKpis();
        renderGapPanel();
        updatePoolCount();
        renderScoutPanel(document.getElementById('arenaScoutInput')?.value || '');
    }

    function flashArenaMessage(msg) {
        const el = document.getElementById('arenaToast');
        if (!el) return;
        el.textContent = msg;
        el.hidden = false;
        clearTimeout(flashArenaMessage._t);
        flashArenaMessage._t = setTimeout(() => { el.hidden = true; }, 2600);
    }

    function backfillCanvasBubble() {
        if (!poolEl || poolFilterQuery) return;
        const W = poolEl.clientWidth;
        const H = poolEl.clientHeight;
        const onCanvas = new Set(bubbleStates.keys());
        const candidates = shuffled(poolVisible().filter(p => !onCanvas.has(p.player_name)));
        for (const player of candidates) {
            const placed = [...bubbleStates.values()].map(s => ({ x: s.x, y: s.y, size: s.size }));
            const size = bubbleSize(playerPrice(player));
            const pos = tryRandomPosition(size, W, H, placed);
            if (!pos) break;
            const el = createPoolBubble(player);
            poolEl.appendChild(el);
            bubbleStates.set(player.player_name, {
                name: player.player_name,
                size,
                x: pos.x,
                y: pos.y,
                vx: (Math.random() - 0.5) * 0.32,
                vy: (Math.random() - 0.5) * 0.32,
                paused: false,
                el,
                pause() { this.paused = true; },
                resume() { this.paused = false; },
            });
            applyBubblePos(bubbleStates.get(player.player_name));
            resolveCollisions(6);
            updatePoolCount();
            break;
        }
    }

    function updatePoolCount() {
        const el = document.getElementById('arenaPoolCount');
        if (!el) return;
        const available = poolVisible().length;
        const onCanvas = bubbleStates.size;
        if (onCanvas > 0 && onCanvas < available) {
            el.textContent = `${onCanvas} on canvas · ${available} in pool`;
            el.title = `${available - onCanvas} more — use scout (⌕)`;
            return;
        }
        el.textContent = `${available} available`;
        el.title = '';
    }

    function renderSquadDropHint() {
        if (!squadEl || arenaSquad.length > 0) return;
        squadEl.innerHTML = `
            <div class="arena-drop-hint">
                <span class="arena-drop-icon">←</span>
                <p>Drag player bubbles here</p>
                <span>Purse starts at ₹${purseCap()} Cr</span>
            </div>`;
    }

    function renderPoolWhenReady() {
        if (!poolEl) return;
        const w = poolEl.clientWidth;
        const h = poolEl.clientHeight;
        if (w > 80 && h > 80) {
            renderPool();
            return;
        }
        requestAnimationFrame(renderPoolWhenReady);
    }

    function renderPool() {
        if (!poolEl) return;
        const existing = new Map(bubbleStates);
        poolEl.innerHTML = '<div class="arena-pool-label">Player pool</div>';
        bubbleStates.clear();

        const visible = poolVisible();
        const W = poolEl.clientWidth || 400;
        const H = poolEl.clientHeight || 520;

        const preferNames = forceCanvasResample
            ? []
            : [...existing.keys()].filter(name => visible.some(p => p.player_name === name));

        if (forceCanvasResample) forceCanvasResample = false;

        const layout = layoutCanvasSample(visible, W, H, { preferNames });

        layout.forEach(({ player, size, x, y }) => {
            const el = createPoolBubble(player);
            poolEl.appendChild(el);
            const prev = existing.get(player.player_name);
            const state = {
                name: player.player_name,
                size,
                x: prev && !poolFilterQuery
                    ? Math.min(W - size / 2, Math.max(size / 2, prev.x))
                    : x,
                y: prev && !poolFilterQuery
                    ? Math.min(H - size / 2, Math.max(size / 2 + 36, prev.y))
                    : y,
                vx: prev?.vx ?? (Math.random() - 0.5) * 0.32,
                vy: prev?.vy ?? (Math.random() - 0.5) * 0.32,
                paused: false,
                el,
                pause() { this.paused = true; },
                resume() { this.paused = false; },
            };
            bubbleStates.set(player.player_name, state);
            applyBubblePos(state);
        });

        resolveCollisions(10);
        bubbleStates.forEach(applyBubblePos);
        updatePoolCount();
    }

    function renderScoutPanel(query = '') {
        const panel = document.getElementById('arenaScoutPanel');
        const results = document.getElementById('arenaScoutResults');
        if (!panel || !results) return;

        const hits = scoutCandidates(query);
        if (!hits.length) {
            results.innerHTML = `<p class="arena-scout-empty">${query ? 'No players match — try another name' : 'Type to find players not visible in the pool'}</p>`;
            return;
        }

        results.innerHTML = hits.map(p => {
            const rk = roleKey(p.pool_role || p.auction_role);
            const price = playerPrice(p);
            return `
                <div class="arena-scout-row arena-scout-row--${rk}"
                     draggable="true"
                     data-scout-player="${escapeAttr(p.player_name)}">
                    <img class="arena-scout-row__img" data-avatar="${escapeAttr(p.player_name)}" data-avatar-url="${escapeAttr(p.facecard_url || '')}" alt="" draggable="false">
                    <div class="arena-scout-row__meta">
                        <strong>${p.player_name}</strong>
                        <span>${normalizeRole(p.pool_role || p.auction_role)} · ₹${price.toFixed(1)} Cr</span>
                    </div>
                    <button type="button" class="arena-scout-row__add" data-add="${escapeAttr(p.player_name)}">Add</button>
                </div>`;
        }).join('');

        results.querySelectorAll('.arena-scout-row').forEach(row => {
            const name = row.getAttribute('data-scout-player');
            row.addEventListener('dragstart', e => {
                e.dataTransfer.setData('text/plain', name);
                e.dataTransfer.effectAllowed = 'move';
                row.classList.add('arena-scout-row--dragging');
            });
            row.addEventListener('dragend', () => row.classList.remove('arena-scout-row--dragging'));
        });
        results.querySelectorAll('[data-add]').forEach(btn => {
            btn.addEventListener('click', e => {
                e.stopPropagation();
                acquirePlayer(btn.getAttribute('data-add'));
                renderScoutPanel(document.getElementById('arenaScoutInput')?.value || '');
            });
        });
        results.querySelectorAll('[data-avatar]').forEach(img => {
            CSKAvatars.bind(img, img.getAttribute('data-avatar'), { loadedClass: 'player-avatar--loaded' });
        });
    }

    function toggleScoutPanel(force) {
        scoutPanelOpen = typeof force === 'boolean' ? force : !scoutPanelOpen;
        const panel = document.getElementById('arenaScoutPanel');
        const btn = document.getElementById('arenaScoutBtn');
        if (!panel) return;
        panel.hidden = !scoutPanelOpen;
        btn?.classList.toggle('arena-scout-btn--active', scoutPanelOpen);
        if (scoutPanelOpen) {
            renderScoutPanel(document.getElementById('arenaScoutInput')?.value || '');
            document.getElementById('arenaScoutInput')?.focus();
        }
    }

    async function loadPool() {
        const res = await fetch(
            `${apiBase()}/players/auction-pool?filter=all&year=${auctionYear()}&limit=1000`
        );
        if (!res.ok) throw new Error('Could not load auction pool');
        const data = await res.json();
        poolPlayers = data.players || [];
        refreshPriceRange(poolPlayers);
    }

    function stopAnimation() {
        mounted = false;
        if (animFrame) {
            cancelAnimationFrame(animFrame);
            animFrame = null;
        }
        resizeObserver?.disconnect();
        resizeObserver = null;
    }

    async function mount(container) {
        stopAnimation();
        arenaSquad = [];
        bubbleStates.clear();
        syncDashboardKpis();

        container.classList.add('content-area--arena');
        container.innerHTML = `
            <div class="arena-shell">
                <header class="arena-topbar">
                    <div class="arena-topbar__left">
                        <h2 class="arena-title">Auction Arena</h2>
                        <p class="arena-sub" id="arenaSub">IPL ${auctionYear()} pool · floating bubbles · scout (⌕) for full pool</p>
                    </div>
                    <div class="arena-topbar__search">
                        <div class="arena-search-wrap">
                            <span class="arena-search-icon" aria-hidden="true">⌕</span>
                            <input type="search" id="arenaPoolSearch" class="arena-search"
                                   placeholder="Filter pool bubbles…">
                        </div>
                        <button type="button" id="arenaScoutBtn" class="arena-scout-btn"
                                title="Find player to drag into squad" aria-label="Find player">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                <circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2"/>
                                <path d="M20 20l-4-4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                            </svg>
                        </button>
                        <div id="arenaScoutPanel" class="arena-scout-panel" hidden>
                            <input type="search" id="arenaScoutInput" class="arena-scout-input"
                                   placeholder="Search auction pool by name or role…">
                            <p class="arena-scout-hint">Drag a result into your squad →</p>
                            <div id="arenaScoutResults" class="arena-scout-results"></div>
                        </div>
                        <span class="arena-pool-count" id="arenaPoolCount">…</span>
                    </div>
                    <div class="arena-topbar__actions">
                        <button type="button" class="btn-secondary" id="arenaResetBtn">Reset</button>
                        <button type="button" class="btn-primary" id="arenaApplyBtn">Apply to Squad</button>
                    </div>
                </header>

                <div class="arena-legend">
                    <span class="arena-legend-item arena-legend-item--bat">Batter</span>
                    <span class="arena-legend-item arena-legend-item--bowl">Bowler</span>
                    <span class="arena-legend-item arena-legend-item--ar">All-rounder</span>
                    <span class="arena-legend-item arena-legend-item--wk">WK</span>
                </div>

                <div class="arena-split">
                    <section class="arena-pane arena-pane--pool">
                        <div class="arena-pool-canvas" id="arenaPoolCanvas"></div>
                    </section>
                    <section class="arena-pane arena-pane--squad" id="arenaSquadPane">
                        <div class="arena-squad-head">Your squad</div>
                        <div class="arena-squad-meta" id="arenaGapPanel"></div>
                        <div class="arena-squad-canvas" id="arenaSquadCanvas"></div>
                    </section>
                </div>
                <div id="arenaToast" class="arena-toast" hidden></div>
            </div>`;

        poolEl = document.getElementById('arenaPoolCanvas');
        squadEl = document.getElementById('arenaSquadCanvas');

        try {
            await loadPool();
        } catch {
            container.classList.remove('content-area--arena');
            container.innerHTML = '<div class="empty-state">Start the API and reload — Arena needs /api/players/auction-pool</div>';
            return;
        }

        mounted = true;
        forceCanvasResample = true;
        renderPoolWhenReady();
        renderSquadDropHint();
        renderGapPanel();
        syncDashboardKpis();
        const sub = document.getElementById('arenaSub');
        if (sub) {
            sub.textContent = `IPL ${auctionYear()} pool · random sample on canvas · scout (⌕) for all ${poolPlayers.length} players`;
        }

        resizeObserver = new ResizeObserver(() => {
            clearTimeout(resizeObserver._t);
            resizeObserver._t = setTimeout(() => renderPool(), 150);
        });
        resizeObserver.observe(poolEl);

        setupDropZone(document.getElementById('arenaSquadPane'), name => acquirePlayer(name));
        setupDropZone(poolEl, name => {
            if (arenaSquad.some(p => p.player_name === name)) releasePlayer(name);
        });

        document.getElementById('arenaPoolSearch')?.addEventListener('input', e => {
            poolFilterQuery = e.target.value.trim();
            forceCanvasResample = true;
            renderPool();
        });

        document.getElementById('arenaScoutBtn')?.addEventListener('click', e => {
            e.stopPropagation();
            toggleScoutPanel();
        });

        document.getElementById('arenaScoutInput')?.addEventListener('input', e => {
            renderScoutPanel(e.target.value.trim());
        });

        if (scoutOutsideClickHandler) {
            document.removeEventListener('click', scoutOutsideClickHandler);
        }
        scoutOutsideClickHandler = e => {
            const panel = document.getElementById('arenaScoutPanel');
            const btn = document.getElementById('arenaScoutBtn');
            if (!scoutPanelOpen || !panel) return;
            if (panel.contains(e.target) || btn?.contains(e.target)) return;
            toggleScoutPanel(false);
        };
        document.addEventListener('click', scoutOutsideClickHandler);

        document.getElementById('arenaResetBtn')?.addEventListener('click', () => {
            arenaSquad = [];
            forceCanvasResample = true;
            renderPool();
            renderSquadDropHint();
            renderGapPanel();
            syncDashboardKpis();
            renderScoutPanel(document.getElementById('arenaScoutInput')?.value || '');
        });

        document.getElementById('arenaApplyBtn')?.addEventListener('click', () => {
            window.CSKDashboard?.applyArenaSquad?.(arenaSquad);
            flashArenaMessage('Synced to Squad tab');
        });

        animFrame = requestAnimationFrame(tickPhysics);
    }

    function unmount() {
        stopAnimation();
        if (scoutOutsideClickHandler) {
            document.removeEventListener('click', scoutOutsideClickHandler);
            scoutOutsideClickHandler = null;
        }
        CSKAvatars.clearQueue();
        document.getElementById('contentArea')?.classList.remove('content-area--arena');
        poolEl = null;
        squadEl = null;
        bubbleStates.clear();
    }

    return { mount, unmount };
})();
