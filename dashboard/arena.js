/**
 * Auction Arena — floating bubbles (no overlap), bid-sized, API portraits.
 */

const Arena = (() => {
    const IDEAL = { Batter: 6, Bowler: 6, 'All Rounder': 5, 'Wicket Keeper': 2 };
    const MAX_OVERSEAS = 8;
    const MAX_SQUAD = 25;
    const FLEX_SLOTS = MAX_SQUAD - Object.values(IDEAL).reduce((sum, n) => sum + n, 0);
    const CANVAS_BUBBLE_MIN = 26;
    const CANVAS_BUBBLE_MAX = 40;

    let poolPlayers = [];
    let arenaSquad = [];
    /** @type {Map<string, object>} */
    let bubbleStates = new Map();
    let priceRange = { min: 0.5, max: 16 };
    let animFrame = null;
    let poolEl = null;
    let squadEl = null;
    let poolFilterQuery = '';
    /** @type {'fmv'|'bid'|'bubble'} */
    let priceMode = 'fmv';
    let scoutFilter = 'all';
    let poolSort = 'price_desc';
    let warRoomContext = null;
    const fmvCache = new Map();
    const hoverIntelCache = new Map();
    let gapRefreshTimer = null;
    let hoverPreviewGen = 0;
    let hoverDebounceTimer = null;
    let hoverFetchAbort = null;
    /** Block pool/scout hover preview briefly after × release (cursor often over new bubble). */
    let hoverSuppressUntil = 0;
    const HOVER_DEBOUNCE_MS = 100;
    const HOVER_DEBOUNCE_CACHED_MS = 40;
    const HOVER_SUPPRESS_MS = 900;
    /** Player name currently hovered (null = show idle hint only). */
    let activeHoverName = null;
    /** Pinned pool preview — stays until Clear / × on chip / Esc. */
    let lockedPlayerKey = null;
    let lockedPreviewPlayer = null;
    let scoutPanelOpen = false;
    let resizeObserver = null;
    let mounted = false;
    let scoutOutsideClickHandler = null;
    let arenaPreviewKeyHandler = null;
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

    function bubbleShortName(name) {
        const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
        if (parts.length >= 2) return parts[parts.length - 1];
        return parts[0] || '?';
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

    function priceBasisLabel(basis) {
        const b = basis || priceMode;
        if (b === 'fmv') return 'FMV';
        if (b === 'bid') return 'Live bid';
        if (b === 'squad') return 'Squad';
        if (b === 'bubble') return 'Pool';
        return 'Est.';
    }

    function purseBasisSummary() {
        if (!arenaSquad.length) return 'Select pricing mode';
        const bases = new Set(arenaSquad.map(p => p.price_basis || priceMode));
        if (bases.size === 1) return `${priceBasisLabel([...bases][0])} basis`;
        return 'Mixed price basis';
    }

    function resolveSyncPrice(player, basis) {
        const mode = basis || priceMode;
        if (mode === 'bid') {
            const b = Number(player.last_bid_cr || player.bubble_price_cr || 0);
            return b > 0 ? b : 2;
        }
        if (mode === 'bubble') return playerPrice(player);
        if (player.fmv_cr != null && player.fmv_cr > 0) return player.fmv_cr;
        return playerPrice(player);
    }

    async function fetchFmv(playerName) {
        const key = playerName.trim();
        if (fmvCache.has(key)) return fmvCache.get(key);
        const r = await fetch(`${apiBase()}/players/valuation/${encodeURIComponent(key)}`);
        if (!r.ok) throw new Error('FMV unavailable');
        const data = await r.json();
        const fmv = Number(data.estimated_value) || 0;
        fmvCache.set(key, fmv);
        return fmv;
    }

    async function resolveAcquirePrice(player) {
        if (priceMode === 'fmv') {
            try {
                const fmv = await fetchFmv(player.player_name);
                return { price: fmv > 0 ? fmv : playerPrice(player), price_basis: 'fmv', fmv_cr: fmv };
            } catch {
                return { price: playerPrice(player), price_basis: 'bubble', fmv_cr: null };
            }
        }
        if (priceMode === 'bid') {
            return { price: resolveSyncPrice(player, 'bid'), price_basis: 'bid', fmv_cr: player.fmv_cr };
        }
        return { price: playerPrice(player), price_basis: 'bubble', fmv_cr: player.fmv_cr };
    }

    function arenaSquadForWarRoom() {
        return arenaSquad.map(p => ({
            name: p.player_name,
            role: p.role || p.pool_role || 'Player',
            price: p.price || 0,
            country: p.country || 'India',
        }));
    }

    function scheduleGapRefresh() {
        clearTimeout(gapRefreshTimer);
        gapRefreshTimer = setTimeout(() => refreshWarRoomContext(), 400);
    }

    async function refreshWarRoomContext() {
        if (!arenaSquad.length) {
            warRoomContext = null;
            renderGapPanel();
            return;
        }
        const probe = arenaSquad[0].player_name;
        try {
            const d = await window.CSKDashboard?.fetchWarRoomDecision?.(probe, {
                squadOverride: arenaSquadForWarRoom(),
                budget: purseCap(),
            });
            warRoomContext = d?.squad_context || null;
        } catch (e) {
            console.warn('Arena war-room gaps:', e);
            warRoomContext = null;
        }
        renderGapPanel();
    }

    function gapRolesNeeded() {
        const gaps = warRoomContext?.gaps
            || squadGaps().gaps.map(g => ({ role: g.role, need: g.need }));
        return gaps.filter(g => (g.need || 0) > 0).map(g => g.role);
    }

    function sortPoolList(list) {
        const copy = [...list];
        switch (poolSort) {
            case 'price_asc':
                return copy.sort((a, b) => resolveSyncPrice(a) - resolveSyncPrice(b));
            case 'name':
                return copy.sort((a, b) => a.player_name.localeCompare(b.player_name));
            case 'form':
                return copy.sort((a, b) => (b.form_rating || 0) - (a.form_rating || 0));
            case 'price_desc':
            default:
                return copy.sort((a, b) => resolveSyncPrice(b) - resolveSyncPrice(a));
        }
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
        const MIN_PX = 48;
        const MAX_PX = 132;
        const t = priceToUnit(priceCr);
        return Math.round(MIN_PX + t * (MAX_PX - MIN_PX));
    }

    function bubbleTier(priceCr) {
        if (priceCr >= 15) return 'mega';
        if (priceCr >= 8) return 'premium';
        if (priceCr >= 3) return 'mid';
        return 'base';
    }

    function bubbleBaseZIndex(priceCr) {
        return 3 + Math.round(priceToUnit(priceCr) * 14);
    }

    function setBubbleZIndex(el, z) {
        el.style.zIndex = String(z);
    }

    function bindAvatarImg(img, name, initialsEl, facecardUrl, espnPortraitUrl) {
        if (typeof CSKAvatars === 'undefined') return;
        try {
            CSKAvatars.bind(img, name, {
                loadedClass: 'arena-bubble__face--loaded',
                initialsEl,
                eager: true,
                facecardUrl: facecardUrl || '',
                espnPortraitUrl: espnPortraitUrl || '',
            });
        } catch (err) {
            console.warn('Arena avatar bind failed:', name, err);
        }
    }

    function removePoolBubble(name) {
        bubbleStates.delete(name);
        if (!poolEl) return;
        poolEl.querySelectorAll(`.arena-bubble[data-player="${CSS.escape(name)}"]`).forEach(el => el.remove());
    }

    function findPoolPlayer(name) {
        if (window.CSKPolish?.resolvePoolPlayer) {
            return window.CSKPolish.resolvePoolPlayer(name, poolPlayers);
        }
        const key = String(name || '').trim().toLowerCase();
        return poolPlayers.find(p => p.player_name.trim().toLowerCase() === key) || null;
    }

    /** Map Squad tab / API row → Arena squad entry (pool row merged when present). */
    function squadRowToArena(s) {
        const name = (s.name || s.player_name || '').trim();
        if (!name) return null;
        const pool = findPoolPlayer(name);
        const price = Number(s.price) > 0
            ? Number(s.price)
            : (pool ? playerPrice(pool) : 2);
        return {
            ...(pool || {}),
            player_name: name,
            price,
            role: normalizeRole(s.role || pool?.pool_role || pool?.auction_role),
            pool_role: pool?.pool_role || s.role,
            country: s.country || pool?.country || 'India',
            overseas: s.overseas ?? isOverseas(s.country || pool?.country),
            facecard_url: pool?.facecard_url || '',
            espn_portrait_url: pool?.espn_portrait_url || '',
            bubble_price_cr: pool?.bubble_price_cr ?? price,
            price_basis: 'squad',
            squad_source: s.price_source || (pool ? 'auction_pool' : 'csk_squad'),
        };
    }

    async function hydrateArenaFromDashboardSquad(forceApi = false) {
        const dash = window.CSKDashboard;
        if (!dash?.ensureSquadLoaded) return { loaded: false, count: 0 };
        const meta = await dash.ensureSquadLoaded(forceApi);
        const rows = dash.getCurrentSquad?.() || [];
        arenaSquad = rows.map(squadRowToArena).filter(Boolean);
        dedupeArenaSquad();
        arenaSquad.forEach(p => removePoolBubble(p.player_name));
        if (arenaSquad.length > 0) {
            squadEl?.querySelector('.arena-drop-hint')?.remove();
        } else {
            renderSquadDropHint();
        }
        scheduleGapRefresh();
        syncDashboardKpis();
        updatePoolCount();
        return { ...meta, count: arenaSquad.length };
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
        uniqueSquadList().forEach(p => {
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
        uniqueSquadList().forEach(p => {
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
        updateSquadHeader();
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
            let chips = filled.map(p => squadChipHtml(p, rk)).join('');
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
        let flexChips = flex.map(p => squadChipHtml(p, roleKey(p.pool_role || p.role))).join('');
        for (let i = 0; i < flexEmpty; i++) {
            flexChips += '<div class="arena-slot-empty arena-slot-empty--flex">Empty</div>';
        }

        const wrGaps = warRoomContext?.gaps || [];
        const gapChips = wrGaps.length
            ? wrGaps.slice(0, 6).map(gg => `
                <span class="arena-war-gap ${gg.priority === 'Critical' ? 'arena-war-gap--critical' : ''}"
                      title="Need ${gg.need} more">
                    ${escapeHtml(gg.role)} ${gg.have}/${gg.ideal}
                </span>`).join('')
            : '<span class="arena-war-gap arena-war-gap--muted">Build squad for gap analysis</span>';

        el.innerHTML = `
            <div id="arenaImpactPreview" class="arena-impact arena-impact--idle" aria-live="polite"></div>
            <div class="arena-war-gaps" aria-label="Squad gaps">${gapChips}</div>
            <div class="arena-stats-row">
                <div class="arena-stat"><span>Purse left</span><strong>₹${remainingCr().toFixed(1)} Cr</strong><em class="arena-stat__note">${escapeHtml(purseBasisSummary())}</em></div>
                <div class="arena-stat"><span>Squad</span><strong>${arenaSquad.length}/${MAX_SQUAD}</strong></div>
                <div class="arena-stat"><span>Overseas</span><strong>${g.overseas}/${MAX_OVERSEAS}${warRoomContext ? ` · ${warRoomContext.overseas_slots_left ?? ''} left` : ''}</strong></div>
            </div>
            <p class="arena-mix-hint">Role targets (19) + ${FLEX_SLOTS} flex · pricing: ${priceBasisLabel(priceMode)}</p>
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
            btn.addEventListener('click', e => {
                e.stopPropagation();
                e.preventDefault();
                releasePlayer(btn.getAttribute('data-release'));
            });
        });
        el.querySelectorAll('[data-squad-chip]').forEach(chip => {
            const name = chip.getAttribute('data-squad-chip');
            chip.addEventListener('mouseenter', () => {
                if (lockedPlayerKey === squadKey(name)) return;
                const p = resolveArenaPlayer(name);
                if (p) showSquadChipPreview(p);
            });
            chip.addEventListener('mouseleave', e => {
                if (shouldKeepPreviewOnLeave(e.relatedTarget)) return;
                if (lockedPlayerKey) return;
                clearHoverPreview();
            });
            chip.addEventListener('dblclick', e => {
                e.preventDefault();
                e.stopPropagation();
                suppressClickUntil = Date.now() + 450;
                const p = resolveArenaPlayer(name);
                if (p) openBidGraph(p);
            });
            chip.addEventListener('click', e => {
                if (e.detail > 1) return;
                if (e.target.closest('[data-release]')) return;
                e.stopPropagation();
                const p = resolveArenaPlayer(name);
                if (!p) return;
                lockedPreviewPlayer = p;
                lockedPlayerKey = squadKey(p.player_name);
                activeHoverName = lockedPlayerKey;
                renderSquadPlayerPreview(p);
            });
        });
        const impactEl = document.getElementById('arenaImpactPreview');
        if (typeof CSKAvatars !== 'undefined') {
            CSKAvatars.bindAll(el, '[data-avatar]', { eager: true });
        }
        updateSquadHeader();
        if (lockedPreviewPlayer) {
            renderImpactPreview(lockedPreviewPlayer);
        } else if (!activeHoverName) {
            renderImpactPlaceholder();
        }
    }

    function escapeAttr(s) {
        return String(s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
    }

    function escapeHtml(s) {
        return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function poolVisible() {
        const picked = new Set(uniqueSquadList().map(p => squadKey(p.player_name)));
        const list = poolPlayers.filter(p => {
            if (picked.has(squadKey(p.player_name))) return false;
            if (!poolFilterQuery) return true;
            return p.player_name.toLowerCase().includes(poolFilterQuery.toLowerCase());
        });
        return sortPoolList(list);
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
        const picked = new Set(uniqueSquadList().map(p => squadKey(p.player_name)));
        let inPool = poolPlayers.filter(p => !picked.has(squadKey(p.player_name)));
        if (scoutFilter === 'fills_gap') {
            const needRoles = new Set(gapRolesNeeded());
            if (needRoles.size) {
                inPool = inPool.filter(p => needRoles.has(normalizeRole(p.pool_role || p.auction_role)));
            }
        } else if (scoutFilter === 'affordable') {
            const rem = remainingCr();
            inPool = inPool.filter(p => resolveSyncPrice(p) <= rem + 0.01);
        } else if (scoutFilter === 'indian') {
            inPool = inPool.filter(p => !isOverseas(p.country));
        } else if (scoutFilter === 'inform') {
            inPool = inPool.filter(p => (p.form_rating || 0) >= 60);
        }
        const q = (query || '').trim();
        let hits;
        if (!q) hits = inPool.slice(0, 24);
        else if (typeof CSKPolish !== 'undefined') {
            hits = CSKPolish.searchPlayers(q, 24, inPool);
        } else {
            const lower = q.toLowerCase();
            hits = inPool.filter(p =>
                p.player_name.toLowerCase().includes(lower)
                || (p.pool_role || '').toLowerCase().includes(lower)
                || (p.auction_role || '').toLowerCase().includes(lower),
            ).slice(0, 24);
        }
        return sortPoolList(hits).slice(0, 24);
    }

    function createPoolBubble(player) {
        const price = resolveSyncPrice(player);
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
        const baseZ = bubbleBaseZIndex(price);
        el.dataset.baseZIndex = String(baseZ);
        setBubbleZIndex(el, baseZ);
        el.innerHTML = `
            <div class="arena-bubble__face-wrap portrait-shell portrait-shell--bubble">
                <span class="portrait-skeleton" aria-hidden="true"></span>
                <img class="arena-bubble__face" alt="" draggable="false">
                <span class="arena-bubble__initials">${getInitials(fullName)}</span>
                <span class="arena-bubble__short-name">${escapeHtml(bubbleShortName(fullName))}</span>
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
        bindAvatarImg(img, player.player_name, initials, player.facecard_url, player.espn_portrait_url);

        el.addEventListener('mouseenter', () => {
            setBubbleZIndex(el, 500);
            if (!isInArenaSquad(player.player_name) && !isHoverSuppressed()) {
                renderImpactPreview(player);
            }
        });
        el.addEventListener('mouseleave', (e) => {
            if (!el.classList.contains('arena-bubble--dragging')) {
                setBubbleZIndex(el, Number(el.dataset.baseZIndex) || bubbleBaseZIndex(price));
            }
            const next = e.relatedTarget;
            if (shouldKeepPreviewOnLeave(next)) return;
            if (!isInArenaSquad(player.player_name)) clearHoverPreview();
        });

        let suppressClickUntil = 0;
        el.addEventListener('dragstart', e => {
            suppressClickUntil = Date.now() + 500;
            e.dataTransfer.setData('text/plain', player.player_name);
            e.dataTransfer.effectAllowed = 'move';
            el.classList.add('arena-bubble--dragging');
            setBubbleZIndex(el, 600);
            bubbleStates.get(player.player_name)?.pause();
            requestAnimationFrame(() => { el.style.visibility = 'hidden'; });
        });
        el.addEventListener('dragend', () => {
            el.classList.remove('arena-bubble--dragging');
            if (!el.isConnected) return;
            if (isInArenaSquad(player.player_name)) {
                removePoolBubble(player.player_name);
                return;
            }
            el.style.visibility = '';
            setBubbleZIndex(el, Number(el.dataset.baseZIndex) || bubbleBaseZIndex(price));
            bubbleStates.get(player.player_name)?.resume();
        });
        el.addEventListener('dblclick', e => {
            e.preventDefault();
            e.stopPropagation();
            suppressClickUntil = Date.now() + 450;
            openBidGraph(player);
        });

        el.addEventListener('click', e => {
            if (e.detail > 1) return;
            if (Date.now() < suppressClickUntil) return;
            e.stopPropagation();
            lockImpactPreview(player);
        });

        return el;
    }

    function placementGap(sizeA, sizeB) {
        return (sizeA + sizeB) / 2 + 2;
    }

    function isPlacementClear(x, y, size, placed) {
        for (const other of placed) {
            if (Math.hypot(x - other.x, y - other.y) < placementGap(size, other.size)) return false;
        }
        return true;
    }

    function placementBounds(size, W, H) {
        const topPad = 34;
        const pad = 3;
        const minX = size / 2 + pad;
        const maxX = W - size / 2 - pad;
        const minY = topPad + size / 2 + pad;
        const maxY = H - size / 2 - pad;
        return { minX, maxX, minY, maxY, valid: minX < maxX && minY < maxY };
    }

    function tryGridPosition(size, W, H, placed) {
        const { minX, maxX, minY, maxY, valid } = placementBounds(size, W, H);
        if (!valid) return null;
        const step = Math.max(40, size * 0.52);
        const cols = Math.max(1, Math.ceil((maxX - minX) / step));
        const rows = Math.max(1, Math.ceil((maxY - minY) / step));
        const offsets = shuffled(
            Array.from({ length: cols * rows }, (_, i) => ({
                col: i % cols,
                row: Math.floor(i / cols),
            }))
        );
        for (const { col, row } of offsets) {
            const x = minX + (col + 0.5) * ((maxX - minX) / cols);
            const y = minY + (row + 0.5) * ((maxY - minY) / rows);
            if (isPlacementClear(x, y, size, placed)) return { x, y };
        }
        return null;
    }

    function tryRandomPosition(size, W, H, placed) {
        const { minX, maxX, minY, maxY, valid } = placementBounds(size, W, H);
        if (!valid) return null;

        for (let attempt = 0; attempt < 200; attempt++) {
            const x = minX + Math.random() * (maxX - minX);
            const y = minY + Math.random() * (maxY - minY);
            if (isPlacementClear(x, y, size, placed)) return { x, y };
        }
        return tryGridPosition(size, W, H, placed);
    }

    /** How many bubbles to keep on the pool canvas (scales with pane size). */
    function canvasBubbleTarget(W, H) {
        const area = Math.max(1, W * H);
        const scaled = Math.round(area / 9800);
        return Math.min(CANVAS_BUBBLE_MAX, Math.max(CANVAS_BUBBLE_MIN, scaled));
    }

    /** Pick a random subset of players that fit on canvas without overlapping. */
    function layoutCanvasSample(players, W, H, { preferNames = [], targetCount = null } = {}) {
        const byName = new Map(players.map(p => [p.player_name, p]));
        const layout = [];
        const placed = [];
        const used = new Set();
        const cap = targetCount == null ? players.length : Math.max(1, targetCount);

        const tryAdd = player => {
            if (!player || used.has(player.player_name)) return false;
            if (layout.length >= cap) return false;
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
        for (const player of rest) {
            if (layout.length >= cap) break;
            tryAdd(player);
        }

        if (layout.length < cap) {
            const smallFirst = players
                .filter(p => !used.has(p.player_name))
                .sort((a, b) => bubbleSize(playerPrice(a)) - bubbleSize(playerPrice(b)));
            for (const player of smallFirst) {
                if (layout.length >= cap) break;
                tryAdd(player);
            }
        }

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

    function squadKey(name) {
        return String(name || '').trim().toLowerCase();
    }

    /** One row per player — fixes duplicate chips (e.g. same name added twice). */
    function dedupeArenaSquad() {
        const seen = new Set();
        const out = [];
        for (const p of arenaSquad) {
            const k = squadKey(p.player_name);
            if (!k || seen.has(k)) continue;
            seen.add(k);
            out.push(p);
        }
        if (out.length !== arenaSquad.length) {
            arenaSquad = out;
        }
        return arenaSquad;
    }

    function isInArenaSquad(name) {
        const k = squadKey(name);
        return arenaSquad.some(p => squadKey(p.player_name) === k);
    }

    function uniqueSquadList() {
        return dedupeArenaSquad();
    }

    function simulateAddImpact(player) {
        const price = resolveSyncPrice(player);
        const role = normalizeRole(player.pool_role || player.auction_role || player.role);
        const rem = remainingCr();
        const remAfter = rem - price;
        const overseas = isOverseas(player.country);
        const g = squadGaps();
        const counts = { ...g.counts };
        if (counts[role] !== undefined) counts[role] += 1;
        else counts[role] = 1;

        const ideal = IDEAL[role];
        const have = g.counts[role] || 0;
        const fillsGap = ideal != null && have < ideal;
        const gapAfter = ideal != null ? Math.max(0, ideal - (have + 1)) : 0;

        const blocked = [];
        if (isInArenaSquad(player.player_name)) blocked.push('Already in squad');
        if (arenaSquad.length >= MAX_SQUAD) blocked.push('Squad full (25)');
        if (price > rem + 0.01) blocked.push(`Over budget (need ₹${price.toFixed(1)} Cr)`);
        if (overseas && overseasCount() >= MAX_OVERSEAS) blocked.push('Overseas slots full');

        return {
            ok: blocked.length === 0,
            blocked,
            price,
            priceLabel: priceBasisLabel(priceMode),
            role,
            remAfter,
            fillsGap,
            gapAfter,
            gapRole: role,
            overseas,
            squadAfter: arenaSquad.length + (blocked.length ? 0 : 1),
        };
    }

    function isHoverSuppressed() {
        return Date.now() < hoverSuppressUntil;
    }

    function isInsideArenaSquadPane(el) {
        return !!el?.closest?.('#arenaSquadPane, #arenaGapPanel');
    }

    function isInsideArenaPool(el) {
        return !!el?.closest?.('#arenaPoolCanvas, #arenaScoutPanel, .arena-scout-row');
    }

    function shouldKeepPreviewOnLeave(relatedTarget) {
        if (lockedPlayerKey) return true;
        return isInsideArenaSquadPane(relatedTarget);
    }

    function resolveArenaPlayer(name) {
        const k = squadKey(name);
        const squad = arenaSquad.find(p => squadKey(p.player_name) === k);
        const pool = findPoolPlayer(name) || poolPlayers.find(p => squadKey(p.player_name) === k);
        if (squad && pool) return { ...pool, ...squad, player_name: squad.player_name || pool.player_name };
        if (squad) return { ...squad };
        return pool || null;
    }

    function openBidGraph(playerOrName) {
        const player = typeof playerOrName === 'string'
            ? resolveArenaPlayer(playerOrName)
            : playerOrName;
        if (!player?.player_name) {
            flashArenaMessage('Player not found');
            return;
        }
        if (window.ArenaRadial?.open) {
            window.ArenaRadial.open(player);
            return;
        }
        flashArenaMessage('Bid graph not loaded — hard refresh the page (Cmd+Shift+R)');
    }

    function lockImpactPreview(player) {
        if (!player) return;
        lockedPreviewPlayer = player;
        lockedPlayerKey = squadKey(player.player_name);
        activeHoverName = lockedPlayerKey;
        renderImpactPreview(player);
    }

    function unlockImpactPreview() {
        lockedPlayerKey = null;
        lockedPreviewPlayer = null;
        cancelHoverPreview(true);
    }

    async function fetchHoverIntel(playerName, signal) {
        const key = playerName.trim();
        if (hoverIntelCache.has(key)) return hoverIntelCache.get(key);
        const r = await fetch(`${apiBase()}/players/valuation/${encodeURIComponent(key)}`, { signal });
        if (!r.ok) throw new Error('valuation');
        const data = await r.json();
        hoverIntelCache.set(key, data);
        if (data.estimated_value != null) fmvCache.set(key, Number(data.estimated_value));
        return data;
    }

    function fitBand(score) {
        const s = Number(score);
        if (!Number.isFinite(s) || s <= 0) return { label: '', cls: 'arena-pill--muted' };
        if (s >= 70) return { label: 'Strong', cls: 'arena-pill--good' };
        if (s >= 55) return { label: 'Decent', cls: 'arena-pill--mid' };
        return { label: 'Weak', cls: 'arena-pill--low' };
    }

    function poolIntelFallback(player, imp) {
        const price = imp?.price ?? resolveSyncPrice(player);
        return {
            role: normalizeRole(player.pool_role || player.auction_role || 'Player'),
            role_detail: 'Auction pool — limited stats in database',
            form_score: Number(player.form_rating) || 50,
            csk_fit_score: null,
            confidence: null,
            estimated_value: price,
            floor_price: Math.max(0.2, price * 0.7),
            ceiling_price: price * 1.4,
            auction_verdict: '📋 Pool estimate',
        };
    }

    function resolvePreviewIntel(player, imp, intel) {
        if (intel && (intel.csk_fit_score != null || intel.estimated_value != null)) return intel;
        return poolIntelFallback(player, imp);
    }

    function recommendationLine(imp, intel) {
        if (!imp.ok) return '';
        const fit = Number(intel?.csk_fit_score) || 0;
        const fmv = Number(intel?.estimated_value) || imp.price;
        const conf = Number(intel?.confidence) || 0;
        if (conf < 40) return 'Thin IPL sample — treat price as a wide range, not a precise bid.';
        if (fit >= 70 && imp.fillsGap) {
            return `Priority pick: strong CSK fit and fills your ${imp.gapRole} gap. Stay near ₹${fmv.toFixed(1)} Cr FMV.`;
        }
        if (fit >= 55 && imp.fillsGap) {
            return `Solid option for ${imp.gapRole} — bid up to ~₹${fmv.toFixed(1)} Cr unless a war breaks out.`;
        }
        if (fit < 45) return 'CSK fit is low — only add at a discount vs FMV or for squad balance.';
        if (!imp.fillsGap) return 'Role slots full — would sit in flex; only add if price is exceptional.';
        return `Monitor around ₹${fmv.toFixed(1)} Cr FMV; check radial card for full bid intel.`;
    }

    function renderSquadImpactBlock(si) {
        if (!si || !si.summary) return '';
        const mode = si.mode || '';
        const modeCls = mode === 'upgrade' ? 'arena-squad-impact--upgrade'
            : mode === 'fill_gap' ? 'arena-squad-impact--fill'
                : mode === 'blocked' ? 'arena-squad-impact--blocked' : '';
        const target = si.target;
        const d = si.deltas || {};
        let vsLine = '';
        if (mode === 'fill_gap' && si.gaps) {
            vsLine = `<p class="arena-squad-impact__vs">Adds to <strong>${escapeHtml(si.gaps.role || '')}</strong> (${si.gaps.have}/${si.gaps.ideal} → ${(si.gaps.have || 0) + 1})</p>`;
        } else if (target && (mode === 'upgrade' || mode === 'marginal')) {
            const fitD = d.fit != null ? `${d.fit >= 0 ? '+' : ''}${d.fit} CSK fit` : '';
            const priceD = d.price_cr != null ? ` · ${d.price_cr >= 0 ? '+' : ''}₹${Number(d.price_cr).toFixed(1)} Cr` : '';
            vsLine = `<p class="arena-squad-impact__vs">vs <strong>${escapeHtml(target.name)}</strong> (${target.csk_fit}% fit, ₹${Number(target.price_cr).toFixed(1)} Cr) — ${escapeHtml(fitD)}${escapeHtml(priceD)}</p>`;
        } else if (mode === 'flex_add') {
            vsLine = '<p class="arena-squad-impact__vs">No direct replacement — likely <strong>flex / overflow</strong> slot</p>';
        }
        const reasons = (si.reasons || []).slice(0, 3).map(r => `<li>${escapeHtml(r)}</li>`).join('');
        return `
            <div class="arena-squad-impact ${modeCls}">
                <p class="arena-squad-impact__head">Squad impact</p>
                <p class="arena-squad-impact__summary">${escapeHtml(si.summary)}</p>
                ${vsLine}
                ${reasons ? `<ul class="arena-squad-impact__reasons">${reasons}</ul>` : ''}
            </div>`;
    }

    function impactToolbarHtml(player, pinned) {
        return `
            <div class="arena-impact__toolbar">
                <button type="button" class="btn-link-sm arena-impact__pin ${pinned ? 'arena-impact__pin--on' : ''}" data-impact-pin>
                    ${pinned ? 'Unpin' : 'Pin card'}
                </button>
                <button type="button" class="arena-impact__close" data-impact-clear aria-label="Close">×</button>
            </div>`;
    }

    function wireImpactToolbar(el, player) {
        el.querySelector('[data-impact-pin]')?.addEventListener('click', () => {
            if (lockedPlayerKey === squadKey(player.player_name)) {
                lockedPlayerKey = null;
                lockedPreviewPlayer = null;
                activeHoverName = squadKey(player.player_name);
                renderImpactPreview(player);
            } else {
                lockImpactPreview(player);
            }
        });
        el.querySelector('[data-impact-clear]')?.addEventListener('click', () => unlockImpactPreview());
    }

    function renderImpactPreviewContent(player, imp, intel, phase, squadImpact) {
        const el = document.getElementById('arenaImpactPreview');
        if (!el) return;
        const pinned = lockedPlayerKey === squadKey(player.player_name);

        if (!imp.ok) {
            el.hidden = false;
            el.className = `arena-impact arena-impact--blocked${pinned ? ' arena-impact--pinned' : ''}`;
            el.innerHTML = `
                ${impactToolbarHtml(player, pinned)}
                <div class="arena-impact__hero">
                    ${typeof CSKAvatars !== 'undefined' ? CSKAvatars.markup(player.player_name, 'pcard__portrait portrait-shell--sm', player.facecard_url || '', player.espn_portrait_url || '') : ''}
                    <div><strong>${escapeHtml(player.player_name)}</strong>
                    <span>${escapeHtml(normalizeRole(player.pool_role || player.auction_role))}</span></div>
                </div>
                <p class="arena-impact__title">Can't add to squad</p>
                <ul class="arena-impact__list">${imp.blocked.map(b => `<li>${escapeHtml(b)}</li>`).join('')}</ul>`;
            wireImpactToolbar(el, player);
            return;
        }

        const intelResolved = resolvePreviewIntel(player, imp, intel);
        const fit = Number(intelResolved.csk_fit_score);
        const form = Number(intelResolved.form_score);
        const conf = Number(intelResolved.confidence);
        const fmv = Number(intelResolved.estimated_value) || imp.price;
        const hasFit = Number.isFinite(fit) && fit > 0;
        const hasConf = Number.isFinite(conf) && conf > 0;
        const poolOnly = !intel || intelResolved.role_detail?.includes('limited stats');
        const floor = intelResolved.floor_price ?? intel?.floor_price ?? intel?.market_value?.p10;
        const ceil = intelResolved.ceiling_price ?? intel?.ceiling_price ?? intel?.market_value?.p90;
        const verdict = intelResolved.auction_verdict || intel?.auction_verdict || '';
        const fitMeta = fitBand(hasFit ? fit : NaN);
        const gapLine = imp.fillsGap
            ? `Fills <strong>${escapeHtml(imp.gapRole)}</strong> (${imp.gapAfter} more needed after)`
            : '<strong>Flex slot</strong> — core role buckets full';

        el.hidden = false;
        el.className = `arena-impact arena-impact--ok${pinned ? ' arena-impact--pinned' : ''}`;
        el.innerHTML = `
            ${impactToolbarHtml(player, pinned)}
            <div class="arena-impact__hero">
                ${typeof CSKAvatars !== 'undefined' ? CSKAvatars.markup(player.player_name, 'pcard__portrait portrait-shell--sm', player.facecard_url || '', player.espn_portrait_url || '') : ''}
                <div class="arena-impact__hero-text">
                    <strong>${escapeHtml(player.player_name)}</strong>
                    <span>${escapeHtml(intelResolved.role || imp.role)}${intelResolved.role_detail ? ` · ${escapeHtml(intelResolved.role_detail)}` : ''}</span>
                    ${verdict || intelResolved.auction_verdict ? `<em class="arena-impact__verdict">${escapeHtml(verdict || intelResolved.auction_verdict)}</em>` : ''}
                </div>
            </div>
            ${phase === 'loading' ? '<p class="arena-impact__loading">Loading CSK fit & FMV…</p>' : `
            <div class="arena-impact__metrics">
                <div class="arena-impact__metric"><span>CSK fit</span><strong class="arena-pill ${fitMeta.cls}">${hasFit ? `${Math.round(fit)}% ${fitMeta.label}` : 'N/A'}</strong></div>
                <div class="arena-impact__metric"><span>Form</span><strong>${Number.isFinite(form) ? Math.round(form) : '—'}</strong></div>
                <div class="arena-impact__metric"><span>Confidence</span><strong>${hasConf ? `${Math.round(conf)}%` : '—'}</strong></div>
                <div class="arena-impact__metric"><span>FMV</span><strong>₹${fmv.toFixed(1)} Cr</strong></div>
            </div>
            ${poolOnly ? '<p class="arena-impact__note">Not in stats DB yet — price from pool; open <strong>Bid graph</strong> or double-click the bubble.</p>' : ''}
            ${floor != null && ceil != null ? `<p class="arena-impact__band">Typical range <strong>₹${Number(floor).toFixed(1)}–${Number(ceil).toFixed(1)} Cr</strong></p>` : ''}
            `}
            <div class="arena-impact__divider"></div>
            <p class="arena-impact__section-label">If you add them</p>
            <div class="arena-impact__grid">
                <div><span>Your cost</span><strong>₹${imp.price.toFixed(1)} Cr</strong> <em>${escapeHtml(imp.priceLabel)}</em></div>
                <div><span>Purse after</span><strong class="${imp.remAfter < 15 ? 'arena-text-warn' : ''}">₹${imp.remAfter.toFixed(1)} Cr</strong></div>
                <div><span>Squad size</span><strong>${imp.squadAfter}/25</strong></div>
                <div><span>Gap</span><strong>${gapLine}</strong></div>
            </div>
            ${imp.overseas ? '<p class="arena-impact__note">+1 overseas slot</p>' : ''}
            ${phase === 'ready' && intel ? `<p class="arena-impact__rec">${escapeHtml(recommendationLine(imp, intel))}</p>` : ''}
            ${phase === 'ready' ? renderSquadImpactBlock(squadImpact) : ''}
            <div class="arena-impact__actions">
                <button type="button" class="btn-primary btn-sm" data-impact-add>Add to squad</button>
                <button type="button" class="btn-secondary btn-sm" data-impact-radial>Bid graph</button>
            </div>`;

        el.querySelector('[data-impact-add]')?.addEventListener('click', () => {
            acquirePlayer(player.player_name);
            clearHoverPreview();
        });
        el.querySelector('[data-impact-radial]')?.addEventListener('click', () => openBidGraph(player));
        el.querySelector('.arena-impact__hero')?.addEventListener('dblclick', e => {
            e.preventDefault();
            e.stopPropagation();
            openBidGraph(player);
        });
        wireImpactToolbar(el, player);
        if (typeof CSKAvatars !== 'undefined') CSKAvatars.bindAll(el, '[data-avatar]', { eager: true });
    }

    function paintImpactPlaceholder(el) {
        if (!el) return;
        el.hidden = false;
        el.className = 'arena-impact arena-impact--idle';
        el.innerHTML = `
            <p class="arena-impact__idle-title">Preview a player</p>
            <p class="arena-impact__idle-text"><strong>Pool:</strong> click a bubble to pin. <strong>Squad:</strong> double-click a name chip for the bid graph.</p>`;
    }

    function renderImpactPlaceholder() {
        const el = document.getElementById('arenaImpactPreview');
        if (!el) return;
        if (lockedPlayerKey) return;
        activeHoverName = null;
        hoverPreviewGen += 1;
        clearTimeout(hoverDebounceTimer);
        hoverDebounceTimer = null;
        paintImpactPlaceholder(el);
    }

    function cancelHoverPreview(force = false) {
        if (!force && lockedPlayerKey) {
            hoverFetchAbort?.abort();
            hoverFetchAbort = null;
            clearTimeout(hoverDebounceTimer);
            hoverDebounceTimer = null;
            return;
        }
        hoverFetchAbort?.abort();
        hoverFetchAbort = null;
        lockedPlayerKey = null;
        lockedPreviewPlayer = null;
        activeHoverName = null;
        hoverPreviewGen += 1;
        clearTimeout(hoverDebounceTimer);
        hoverDebounceTimer = null;
        const el = document.getElementById('arenaImpactPreview');
        paintImpactPlaceholder(el);
    }

    function clearHoverPreview() {
        if (lockedPlayerKey) return;
        cancelHoverPreview(true);
    }

    function isStillHovered(name) {
        const k = squadKey(name);
        return activeHoverName === k || lockedPlayerKey === k;
    }

    function renderImpactPreview(player) {
        const el = document.getElementById('arenaImpactPreview');
        if (!el || !player || isHoverSuppressed()) return cancelHoverPreview();
        if (lockedPlayerKey && lockedPlayerKey !== squadKey(player.player_name)) return;

        hoverFetchAbort?.abort();
        const ac = new AbortController();
        hoverFetchAbort = ac;

        activeHoverName = squadKey(player.player_name);
        const gen = ++hoverPreviewGen;
        const imp = simulateAddImpact(player);
        const cacheKey = player.player_name.trim();
        const cachedIntel = hoverIntelCache.get(cacheKey);
        renderImpactPreviewContent(
            player,
            imp,
            cachedIntel || null,
            cachedIntel ? 'ready' : 'loading',
            null,
        );

        clearTimeout(hoverDebounceTimer);
        const delay = cachedIntel ? HOVER_DEBOUNCE_CACHED_MS : HOVER_DEBOUNCE_MS;
        hoverDebounceTimer = setTimeout(async () => {
            try {
                const [intel, squadImpact] = await Promise.all([
                    fetchHoverIntel(player.player_name, ac.signal),
                    window.CSKDashboard?.fetchSquadImpact?.(player.player_name, arenaSquad, {
                        budget: purseCap(),
                        candidate_price_cr: resolveSyncPrice(player),
                        signal: ac.signal,
                    }).catch(err => {
                        if (err?.name === 'AbortError') throw err;
                        return null;
                    }),
                ]);
                if (ac.signal.aborted || gen !== hoverPreviewGen || !isStillHovered(player.player_name)) return;
                renderImpactPreviewContent(player, imp, intel, 'ready', squadImpact);
            } catch (err) {
                if (err?.name === 'AbortError') return;
                if (gen !== hoverPreviewGen || !isStillHovered(player.player_name)) return;
                renderImpactPreviewContent(player, imp, cachedIntel || null, 'ready', null);
            }
        }, delay);
    }

    function showSquadChipPreview(p) {
        if (!p || isHoverSuppressed()) return cancelHoverPreview();
        hoverFetchAbort?.abort();
        hoverFetchAbort = null;
        activeHoverName = squadKey(p.player_name);
        hoverPreviewGen += 1;
        clearTimeout(hoverDebounceTimer);
        hoverDebounceTimer = null;
        renderSquadPlayerPreview(p);
    }

    function renderSquadPlayerPreview(p) {
        const el = document.getElementById('arenaImpactPreview');
        if (!el) return;
        const pinned = lockedPlayerKey === squadKey(p.player_name);
        el.hidden = false;
        el.className = `arena-impact arena-impact--in-squad${pinned ? ' arena-impact--pinned' : ''}`;
        el.innerHTML = `
            <div class="arena-impact__toolbar">
                <span class="arena-impact__squad-badge">In your squad</span>
                <button type="button" class="arena-impact__close" data-impact-clear aria-label="Close">×</button>
            </div>
            <div class="arena-impact__hero" data-squad-graph-hero>
                ${typeof CSKAvatars !== 'undefined' ? CSKAvatars.markup(p.player_name, 'pcard__portrait portrait-shell--sm', p.facecard_url || '', p.espn_portrait_url || '') : ''}
                <div><strong>${escapeHtml(p.player_name)}</strong>
                <span>${escapeHtml(normalizeRole(p.role || p.pool_role))}</span></div>
            </div>
            <p class="arena-impact__title">Counted at <strong>₹${Number(p.price || 0).toFixed(1)} Cr</strong> (${priceBasisLabel(p.price_basis)}) toward purse.</p>
            <p class="arena-impact__note">No pool bubble — <strong>double-click</strong> this chip or tap <strong>Open bid graph</strong> for CSK fit & bid intel.</p>
            <div class="arena-impact__actions">
                <button type="button" class="btn-primary btn-sm" data-squad-graph>Open bid graph</button>
            </div>`;
        el.querySelector('[data-squad-graph]')?.addEventListener('click', () => openBidGraph(p));
        el.querySelector('[data-impact-clear]')?.addEventListener('click', () => unlockImpactPreview());
        el.querySelector('[data-squad-graph-hero]')?.addEventListener('dblclick', e => {
            e.preventDefault();
            openBidGraph(p);
        });
    }

    function updateSquadHeader() {
        const purse = document.getElementById('arenaHeadPurse');
        if (purse) {
            purse.textContent = `₹${remainingCr().toFixed(1)} Cr left · ${uniqueSquadList().length}/25 players`;
        }
    }

    function squadChipHtml(p, rk) {
        const full = p.player_name || '';
        const short = full.split(' ').filter(Boolean);
        const label = short.length >= 2 ? `${short[0][0]}. ${short[short.length - 1]}` : full;
        return `
            <div class="arena-slot-chip arena-slot-chip--${rk}" data-squad-chip="${escapeAttr(full)}" title="${escapeAttr(full)}">
                ${typeof CSKAvatars !== 'undefined' ? CSKAvatars.markup(full, 'pcard__portrait portrait-shell--xs', p.facecard_url || '', p.espn_portrait_url || '') : ''}
                <div class="arena-slot-chip__text">
                    <span class="arena-slot-chip__name">${escapeHtml(label)}</span>
                    <span class="arena-slot-chip__price">₹${Number(p.price || 0).toFixed(1)} Cr · ${priceBasisLabel(p.price_basis)}</span>
                </div>
                <button type="button" class="arena-slot-chip__x" data-release="${escapeAttr(full)}" aria-label="Remove ${escapeAttr(full)}">×</button>
            </div>`;
    }

    async function acquirePlayer(name) {
        dedupeArenaSquad();
        if (isInArenaSquad(name)) {
            flashArenaMessage('Already in your squad');
            return;
        }
        if (arenaSquad.length >= MAX_SQUAD) {
            flashArenaMessage('Squad full (25 max)');
            return;
        }
        const player = findPoolPlayer(name) || poolPlayers.find(p => p.player_name === name);
        if (!player) return;
        const est = resolveSyncPrice(player);
        if (est > remainingCr()) {
            flashArenaMessage(`Need ~₹${est.toFixed(1)} Cr · only ₹${remainingCr().toFixed(1)} left`);
            return;
        }
        if (isOverseas(player.country) && overseasCount() >= MAX_OVERSEAS) {
            flashArenaMessage('Overseas slots full (8 max)');
            return;
        }

        const priced = await resolveAcquirePrice(player);
        if (priced.price > remainingCr()) {
            flashArenaMessage(`Need ₹${priced.price.toFixed(1)} Cr (${priceBasisLabel(priced.price_basis)}) · only ₹${remainingCr().toFixed(1)} left`);
            return;
        }

        arenaSquad.push({
            ...player,
            price: priced.price,
            fmv_cr: priced.fmv_cr,
            price_basis: priced.price_basis,
            role: normalizeRole(player.pool_role || player.auction_role),
            overseas: isOverseas(player.country),
        });

        removePoolBubble(player.player_name);
        backfillCanvasToTarget();

        squadEl?.querySelector('.arena-drop-hint')?.remove();
        syncDashboardKpis();
        scheduleGapRefresh();
        updatePoolCount();
        renderScoutPanel(document.getElementById('arenaScoutInput')?.value || '');
        dedupeArenaSquad();
        if (lockedPlayerKey === squadKey(player.player_name)) unlockImpactPreview();
        else cancelHoverPreview(true);
        flashArenaMessage(`✓ ${player.player_name} · ₹${priced.price.toFixed(1)} Cr (${priceBasisLabel(priced.price_basis)})`);
    }

    function releasePlayer(name) {
        const k = squadKey(name);
        if (!arenaSquad.some(p => squadKey(p.player_name) === k)) return;
        arenaSquad = arenaSquad.filter(p => squadKey(p.player_name) !== k);
        hoverSuppressUntil = Date.now() + HOVER_SUPPRESS_MS;
        if (lockedPlayerKey === k) unlockImpactPreview();
        else cancelHoverPreview(true);
        renderGapPanel();
        syncDashboardKpis();
        updatePoolCount();
        renderScoutPanel(document.getElementById('arenaScoutInput')?.value || '');
        scheduleGapRefresh();
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
    }

    function flashArenaMessage(msg) {
        const el = document.getElementById('arenaToast');
        if (!el) return;
        el.textContent = msg;
        el.hidden = false;
        clearTimeout(flashArenaMessage._t);
        flashArenaMessage._t = setTimeout(() => { el.hidden = true; }, 2600);
    }

    function addPlayerToCanvas(player, W, H) {
        const placed = [...bubbleStates.values()].map(s => ({ x: s.x, y: s.y, size: s.size }));
        const size = bubbleSize(playerPrice(player));
        const pos = tryRandomPosition(size, W, H, placed);
        if (!pos) return false;
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
        return true;
    }

    /** After a player leaves the pool canvas, seed new random bubbles up to the target density. */
    function backfillCanvasToTarget() {
        if (!poolEl || poolFilterQuery) return;
        const W = poolEl.clientWidth;
        const H = poolEl.clientHeight;
        const target = canvasBubbleTarget(W, H);
        const need = target - bubbleStates.size;
        if (need <= 0) return;

        const onCanvas = new Set(bubbleStates.keys());
        const candidates = shuffled(poolVisible().filter(p => !onCanvas.has(p.player_name)));
        let added = 0;
        for (const player of candidates) {
            if (added >= need) break;
            if (addPlayerToCanvas(player, W, H)) added++;
        }
        if (added > 0) {
            resolveCollisions(8);
            updatePoolCount();
        }
    }

    function updatePoolCount() {
        const el = document.getElementById('arenaPoolCount');
        if (!el) return;
        const available = poolVisible().length;
        const total = poolPlayers.length - arenaSquad.length;
        const onCanvas = bubbleStates.size;
        if (onCanvas > 0 && onCanvas < available) {
            el.textContent = `${onCanvas} shown · ${total} in pool`;
            el.title = `${total - onCanvas} more — scout (⌕)`;
            return;
        }
        el.textContent = `${total} in pool`;
        el.title = poolSort ? `Sorted: ${poolSort}` : '';
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

    let poolReadyAttempts = 0;

    function renderPoolWhenReady() {
        if (!poolEl || !mounted) return;
        const w = poolEl.clientWidth;
        const h = poolEl.clientHeight;
        if (w > 80 && h > 80) {
            poolReadyAttempts = 0;
            try {
                renderPool();
            } catch (err) {
                console.error('Arena renderPool failed:', err);
                poolEl.innerHTML = '<div class="empty-state">Arena could not render bubbles. Check the browser console.</div>';
            }
            return;
        }
        poolReadyAttempts += 1;
        if (poolReadyAttempts > 120) {
            poolEl.innerHTML = '<div class="empty-state">Arena pool area has no size — widen the window or refresh.</div>';
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

        const target = canvasBubbleTarget(W, H);
        const layout = layoutCanvasSample(visible, W, H, { preferNames, targetCount: target });

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
            const price = resolveSyncPrice(p);
            const basis = priceMode === 'fmv' && fmvCache.has(p.player_name) ? 'fmv' : priceMode;
            const imp = simulateAddImpact(p);
            const hint = imp.ok
                ? `→ ₹${imp.remAfter.toFixed(1)} Cr left${imp.fillsGap ? ` · fills ${imp.gapRole}` : ''}`
                : imp.blocked[0] || 'Cannot add';
            return `
                <div class="pcard pcard--scout-mini arena-scout-row arena-scout-row--${rk}${imp.ok ? '' : ' arena-scout-row--blocked'}"
                     draggable="${imp.ok ? 'true' : 'false'}"
                     data-scout-player="${escapeAttr(p.player_name)}">
                    ${typeof CSKAvatars !== 'undefined' ? CSKAvatars.markup(p.player_name, 'pcard__portrait portrait-shell--sm', p.facecard_url || '', p.espn_portrait_url || '') : ''}
                    <div class="pcard__body arena-scout-row__meta">
                        <strong>${escapeHtml(p.player_name)}</strong>
                        <span>${normalizeRole(p.pool_role || p.auction_role)} · ₹${price.toFixed(1)} Cr (${priceBasisLabel(basis)})</span>
                        <span class="arena-scout-row__impact">${escapeHtml(hint)}</span>
                    </div>
                    <button type="button" class="arena-scout-row__add" data-add="${escapeAttr(p.player_name)}" ${imp.ok ? '' : 'disabled'}>Add</button>
                </div>`;
        }).join('');

        results.querySelectorAll('.arena-scout-row').forEach(row => {
            const name = row.getAttribute('data-scout-player');
            const player = poolPlayers.find(p => p.player_name === name);
            row.addEventListener('mouseenter', () => {
                if (player && !isHoverSuppressed()) renderImpactPreview(player);
            });
            row.addEventListener('mouseleave', (e) => {
                if (shouldKeepPreviewOnLeave(e.relatedTarget)) return;
                clearHoverPreview();
            });
            row.addEventListener('click', e => {
                if (e.target.closest('[data-add]')) return;
                if (player) lockImpactPreview(player);
            });
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
        if (typeof CSKAvatars !== 'undefined') {
            CSKAvatars.bindAll(results, '[data-avatar]', { eager: true });
        }
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
        const cached = window.__cskAuctionPoolCache;
        if (cached?.length) {
            poolPlayers = cached;
            refreshPriceRange(poolPlayers);
            CSKPolish?.syncDatalist?.(poolPlayers);
            return;
        }
        const res = await fetch(
            `${apiBase()}/players/auction-pool?filter=all&year=${auctionYear()}&limit=1000`
        );
        if (!res.ok) throw new Error('Could not load auction pool');
        const data = await res.json();
        poolPlayers = data.players || [];
        window.__cskAuctionPoolCache = poolPlayers;
        refreshPriceRange(poolPlayers);
        CSKPolish?.syncDatalist?.(poolPlayers);
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
        bubbleStates.clear();

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
                            <div class="arena-scout-filters" role="group" aria-label="Scout filters">
                                <button type="button" class="arena-scout-filter arena-scout-filter--active" data-scout-filter="all">All</button>
                                <button type="button" class="arena-scout-filter" data-scout-filter="fills_gap">Fills gap</button>
                                <button type="button" class="arena-scout-filter" data-scout-filter="affordable">≤ purse</button>
                                <button type="button" class="arena-scout-filter" data-scout-filter="indian">Indian</button>
                                <button type="button" class="arena-scout-filter" data-scout-filter="inform">In form</button>
                            </div>
                            <input type="search" id="arenaScoutInput" class="arena-scout-input"
                                   placeholder="Search auction pool by name or role…">
                            <p class="arena-scout-hint">Drag a result into your squad →</p>
                            <div id="arenaScoutResults" class="arena-scout-results"></div>
                        </div>
                        <select id="arenaPoolSort" class="arena-sort" title="Sort pool">
                            <option value="price_desc">Price ↓</option>
                            <option value="price_asc">Price ↑</option>
                            <option value="form">Form</option>
                            <option value="name">Name</option>
                        </select>
                        <select id="arenaPriceMode" class="arena-sort" title="Purse pricing basis">
                            <option value="fmv">FMV</option>
                            <option value="bid">Live bid</option>
                            <option value="bubble">Pool / base</option>
                        </select>
                        <span class="arena-pool-count" id="arenaPoolCount">…</span>
                    </div>
                    <div class="arena-topbar__actions">
                        <button type="button" class="btn-secondary" id="arenaSyncSquadBtn" title="Reload from Squad tab / API">↻ From squad</button>
                        <button type="button" class="btn-secondary" id="arenaResetBtn">Clear arena</button>
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
                        <header class="arena-squad-head" id="arenaSquadHead">
                            <div class="arena-squad-head__row">
                                <h3 class="arena-squad-head__title">Squad builder</h3>
                                <span class="arena-squad-head__purse" id="arenaHeadPurse">—</span>
                            </div>
                            <p class="arena-squad-head__hint">
                                <strong>Pool:</strong> click bubble to pin · double-click for bid graph.
                                <strong>Squad chip:</strong> double-click (or <strong>Open bid graph</strong>) — no bubble once they’re in your squad.
                            </p>
                        </header>
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

        let squadMeta = { loaded: false, count: 0 };
        try {
            squadMeta = await hydrateArenaFromDashboardSquad(false);
            scheduleGapRefresh();
        } catch (err) {
            console.warn('Arena squad hydrate failed:', err);
            renderSquadDropHint();
        }

        document.getElementById('arenaPriceMode')?.addEventListener('change', e => {
            priceMode = e.target.value || 'fmv';
            renderGapPanel();
            flashArenaMessage(`Pricing: ${priceBasisLabel(priceMode)} for new picks`);
        });
        document.getElementById('arenaPoolSort')?.addEventListener('change', e => {
            poolSort = e.target.value || 'price_desc';
            forceCanvasResample = true;
            renderPool();
        });

        document.querySelectorAll('[data-scout-filter]').forEach(btn => {
            btn.addEventListener('click', () => {
                scoutFilter = btn.getAttribute('data-scout-filter') || 'all';
                document.querySelectorAll('[data-scout-filter]').forEach(b => {
                    b.classList.toggle('arena-scout-filter--active', b === btn);
                });
                renderScoutPanel(document.getElementById('arenaScoutInput')?.value || '');
            });
        });

        mounted = true;
        poolReadyAttempts = 0;
        forceCanvasResample = true;
        renderPoolWhenReady();
        syncDashboardKpis();
        const sub = document.getElementById('arenaSub');
        if (sub) {
            const squadLine = squadMeta.count
                ? `${squadMeta.count} from squad (${squadMeta.source || 'loaded'})`
                : 'drag players into squad →';
            sub.textContent = `IPL ${auctionYear()} pool · ${squadLine} · scout (⌕) for all ${poolPlayers.length}`;
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

        document.getElementById('arenaSquadPane')?.addEventListener('mouseleave', e => {
            if (lockedPlayerKey) return;
            if (shouldKeepPreviewOnLeave(e.relatedTarget)) return;
            cancelHoverPreview(true);
        });

        if (arenaPreviewKeyHandler) document.removeEventListener('keydown', arenaPreviewKeyHandler);
        arenaPreviewKeyHandler = e => {
            if (e.key !== 'Escape' || !lockedPlayerKey) return;
            unlockImpactPreview();
        };
        document.addEventListener('keydown', arenaPreviewKeyHandler);

        document.getElementById('arenaPoolSearch')?.addEventListener('input', e => {
            poolFilterQuery = e.target.value.trim();
            forceCanvasResample = true;
            renderPool();
        });

        document.getElementById('arenaScoutBtn')?.addEventListener('click', e => {
            e.stopPropagation();
            toggleScoutPanel();
        });

        const scoutInput = document.getElementById('arenaScoutInput');
        CSKPolish?.wireFuzzySearch?.(scoutInput, {
            players: () => poolPlayers.filter(p => !arenaSquad.some(s => s.player_name === p.player_name)),
            limit: 14,
            onSelect: name => {
                renderScoutPanel(name);
                toggleScoutPanel(true);
            },
            onInput: q => renderScoutPanel(q),
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
            warRoomContext = null;
            forceCanvasResample = true;
            renderPool();
            renderSquadDropHint();
            renderGapPanel();
            syncDashboardKpis();
            renderScoutPanel(document.getElementById('arenaScoutInput')?.value || '');
            flashArenaMessage('Arena cleared (Squad tab unchanged)');
        });

        document.getElementById('arenaSyncSquadBtn')?.addEventListener('click', async () => {
            const meta = await hydrateArenaFromDashboardSquad(true);
            forceCanvasResample = true;
            renderPool();
            const n = meta.count || 0;
            flashArenaMessage(n
                ? `Loaded ${n} players from ${meta.source || 'squad'}`
                : 'No squad loaded — use Squad tab ↻ Sync Squad first');
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
        if (arenaPreviewKeyHandler) {
            document.removeEventListener('keydown', arenaPreviewKeyHandler);
            arenaPreviewKeyHandler = null;
        }
        lockedPlayerKey = null;
        lockedPreviewPlayer = null;
        CSKAvatars.clearQueue();
        document.getElementById('contentArea')?.classList.remove('content-area--arena');
        poolEl = null;
        squadEl = null;
        bubbleStates.clear();
    }

    return {
        mount,
        unmount,
        acquirePlayer,
        releasePlayer,
        getArenaSquad: () => arenaSquad.map(p => ({ ...p })),
        remainingCr,
        flash: flashArenaMessage,
    };
})();

window.CSKArena = Arena;
