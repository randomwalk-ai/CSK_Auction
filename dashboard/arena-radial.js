/**
 * Arena — radial bid-intelligence graph (hub = player, spokes = weighted metrics).
 */
const ArenaRadial = (() => {
    const METRIC_DEFS = [
        {
            id: 'csk_fit',
            label: 'CSK Fit',
            weight: 1.0,
            color: '#004a99',
            value: v => num(v.csk_fit_score, 0, 100),
            detail: v => `${Math.round(num(v.csk_fit_score))} fit`,
            explain:
                'How well the player suits Chennai’s squad — role, batting/bowling balance, and phase needs. Higher = stronger CSK match.',
        },
        {
            id: 'form',
            label: 'Form',
            weight: 0.95,
            color: '#f59e0b',
            value: v => num(v.form_score, 0, 100),
            detail: v => `${Math.round(num(v.form_score))} score`,
            explain:
                'Recent IPL performance score (runs, wickets, strike rate / economy). Higher = in better nick right now.',
        },
        {
            id: 'recent',
            label: 'Recent',
            weight: 0.9,
            color: '#10b981',
            value: v => {
                const runs = num(v.last_10_runs, 0, 400) / 4;
                const wkts = num(v.last_10_wickets, 0, 30) * 3.3;
                return clamp(runs + wkts, 0, 100);
            },
            detail: v => `${v.last_10_runs || 0}r · ${v.last_10_wickets || 0}w`,
            explain:
                'Last ~10 IPL matches: runs and wickets combined into one momentum score. Higher = hotter short-term output.',
        },
        {
            id: 'experience',
            label: 'IPL XP',
            weight: 0.78,
            color: '#06b6d4',
            value: v => num((v.experience_factor || 0) * 100, 0, 100),
            detail: v => `${Math.round(num((v.experience_factor || 0) * 100))}%`,
            explain:
                'IPL experience from matches played — proven at auction level vs raw talent only. Higher = more IPL games on record.',
        },
        {
            id: 'fmv',
            label: 'FMV',
            weight: 0.72,
            color: '#ec4899',
            value: v => clamp((num(v.estimated_value, 0, 25) / 22) * 100, 8, 100),
            detail: v => `₹${num(v.estimated_value).toFixed(1)} Cr`,
            explain:
                'Fair market value from the franchise engine (₹ Cr). Bubble size reflects value vs other metrics on this card.',
        },
        {
            id: 'scarcity',
            label: 'Scarcity',
            weight: 0.68,
            color: '#f97316',
            value: v => clamp(((num(v.scarcity_bonus, 1, 2) - 1) / 0.5) * 100, 5, 100),
            detail: v => v.scarcity_bonus ? `×${num(v.scarcity_bonus).toFixed(2)}` : '—',
            explain:
                'How rare this skill is in the pool (e.g. elite death bowler, finisher). Higher scarcity can justify paying up.',
        },
        {
            id: 'risk',
            label: 'Risk',
            weight: 0.62,
            color: '#64748b',
            value: v => {
                const r = String(v.injury_risk || 'Medium').toLowerCase();
                if (r === 'low') return 88;
                if (r === 'high') return 28;
                return 55;
            },
            detail: v => v.injury_risk || 'Medium',
            explain:
                'Injury / availability risk (Low, Medium, High). Higher score = safer pick; lower = more injury concern.',
        },
    ];

    const CONFIDENCE_META = {
        id: 'confidence',
        label: 'Confidence',
        color: '#8b5cf6',
        value: v => num(v.confidence, 0, 100),
        detail: v => `${Math.round(num(v.confidence))}%`,
        explain:
            'How sure the model is about this price and role (more IPL data = higher). Low = thin sample or unclear profile.',
    };

    function confidenceBand(valuation) {
        const pct = Math.round(CONFIDENCE_META.value(valuation));
        const level = pct >= 65 ? 'high' : pct >= 40 ? 'mid' : 'low';
        return `
            <div class="arena-radial-confidence arena-radial-confidence--${level}" role="status">
                <div class="arena-radial-confidence__main">
                    <span class="arena-radial-confidence__label">Model confidence</span>
                    <span class="arena-radial-confidence__value">${pct}%</span>
                </div>
                <p class="arena-radial-confidence__hint">${escapeHtml(CONFIDENCE_META.explain)}</p>
            </div>
        `;
    }

    function bidPriorityLabel(n) {
        if (n.distClass === 'near') return 'High bid priority — closer to player on chart';
        if (n.distClass === 'far') return 'Lower bid weight — farther from player on chart';
        return 'Moderate bid weight';
    }

    function renderMetricBriefPanel(n) {
        return `
            <div class="arena-radial-metric-brief arena-radial-metric-brief--active" role="status" aria-live="polite">
                <div class="arena-radial-metric-brief__head">
                    <span class="arena-radial-metric-brief__tag" style="border-color:${n.color};color:${n.color}">${escapeHtml(n.label)}</span>
                    <strong class="arena-radial-metric-brief__score">${escapeHtml(String(Math.round(n.rawValue)))}</strong>
                    <span class="arena-radial-metric-brief__detail">${escapeHtml(n.detail || '')}</span>
                </div>
                <p class="arena-radial-metric-brief__text">${escapeHtml(n.explain || '')}</p>
                <p class="arena-radial-metric-brief__meta">${escapeHtml(bidPriorityLabel(n))}</p>
            </div>`;
    }

    function metricBriefIdleHtml() {
        return `<p class="arena-radial-metric-brief__idle">Click any metric bubble for a quick explanation.</p>`;
    }

    function getGraphNodes(valuation) {
        const W = 520;
        const H = 500;
        const cx = W / 2;
        const cy = H / 2 - 6;
        const hubR = 48;
        const minOrbit = hubR + 88;
        const maxOrbit = Math.min(W, H) / 2 - 44;
        return layoutNodes(buildNodes(valuation), cx, cy, hubR, minOrbit, maxOrbit);
    }

    function attachMetricBriefInteractions(rootEl, valuation) {
        const wrap = rootEl.querySelector('.arena-radial-wrap');
        const briefEl = rootEl.querySelector('#arenaRadialMetricBrief');
        if (!wrap || !briefEl) return;

        const nodes = getGraphNodes(valuation);
        const nodeById = new Map(nodes.map(n => [n.id, n]));
        const nodeEls = wrap.querySelectorAll('.arena-radial-node');
        let activeId = null;

        briefEl.innerHTML = metricBriefIdleHtml();

        function clearSelection() {
            activeId = null;
            nodeEls.forEach(el => el.classList.remove('arena-radial-node--selected'));
            briefEl.innerHTML = metricBriefIdleHtml();
        }

        function selectMetric(id) {
            const n = nodeById.get(id);
            if (!n) return;
            if (activeId === id) {
                clearSelection();
                return;
            }
            activeId = id;
            nodeEls.forEach(el => {
                el.classList.toggle('arena-radial-node--selected', el.dataset.metricId === id);
            });
            briefEl.innerHTML = renderMetricBriefPanel(n);
        }

        nodeEls.forEach(el => {
            const id = el.dataset.metricId;
            if (!id || !nodeById.has(id)) return;
            el.addEventListener('click', e => {
                e.stopPropagation();
                selectMetric(id);
            });
            el.addEventListener('keydown', e => {
                if (e.key !== 'Enter' && e.key !== ' ') return;
                e.preventDefault();
                e.stopPropagation();
                selectMetric(id);
            });
        });

        wrap.querySelector('.arena-radial-graph')?.addEventListener('click', e => {
            if (e.target.closest('.arena-radial-node')) return;
            clearSelection();
        });
    }

    function renderMetricGuide(nodes) {
        const rows = (nodes || METRIC_DEFS).map(n => `
            <li class="arena-radial-guide__item">
                <span class="arena-radial-guide__tag" style="border-color:${n.color};color:${n.color}">${escapeHtml(n.label)}</span>
                <span class="arena-radial-guide__text">${escapeHtml(n.explain || '')}</span>
                <span class="arena-radial-guide__meta">Now: <strong>${escapeHtml(n.detail || String(Math.round(n.rawValue || 0)))}</strong></span>
            </li>
        `).join('');

        return `
            <details class="arena-radial-guide">
                <summary>What each parameter means</summary>
                <p class="arena-radial-guide__intro">
                    <strong>Model confidence</strong> is shown at the top (not on the chart).
                    <strong>Closer to the player</strong> = more important when bidding.
                    <strong>Larger circle</strong> = stronger on that metric.
                </p>
                <ul class="arena-radial-guide__list">${rows}</ul>
            </details>
        `;
    }
    let overlayEl = null;
    let escHandler = null;

    function apiBase() {
        return window.CSKDashboard?.API_BASE || 'http://127.0.0.1:8000/api';
    }

    function num(x, lo, hi) {
        const n = Number(x);
        if (!Number.isFinite(n)) return 0;
        if (lo != null && n < lo) return lo;
        if (hi != null && n > hi) return hi;
        return n;
    }

    function clamp(x, a, b) {
        return Math.max(a, Math.min(b, x));
    }

    function escapeHtml(s) {
        return String(s || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function escapeAttr(s) {
        return escapeHtml(s).replace(/'/g, '&#39;');
    }

    function initials(name) {
        const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
        if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
        return (parts[0]?.[0] || '?').toUpperCase();
    }

    /** 0–1 with contrast — low values stay small, highs pop. */
    function valueScale(raw, minV, maxV) {
        if (maxV <= minV) return 0.5;
        const t = (raw - minV) / (maxV - minV);
        return clamp(Math.pow(t, 0.72), 0, 1);
    }

    /** 0 = closest (high bid weight), 1 = farthest. */
    function weightDistanceNorm(weight, minW, maxW) {
        if (maxW <= minW) return 0;
        const t = (weight - minW) / (maxW - minW);
        return clamp(Math.pow(1 - t, 1.65), 0, 1);
    }

    function nodeDiameter(valueT) {
        return Math.round(44 + valueT * 46);
    }

    function buildNodes(valuation) {
        const raw = METRIC_DEFS.map(def => ({
            ...def,
            rawValue: def.value(valuation),
            detail: def.detail(valuation),
        }));
        const minV = Math.min(...raw.map(n => n.rawValue));
        const maxV = Math.max(...raw.map(n => n.rawValue), minV + 1);
        return raw.map(n => ({
            ...n,
            explain: n.explain,
            valueT: valueScale(n.rawValue, minV, maxV),
        }));
    }

    function layoutNodes(nodes, cx, cy, hubR, minOrbit, maxOrbit) {
        const weights = nodes.map(n => n.weight);
        const minW = Math.min(...weights);
        const maxW = Math.max(...weights, minW + 0.01);
        const count = nodes.length;
        const gap = maxOrbit - minOrbit;

        const placed = nodes.map((node, i) => {
            const angle = (i / count) * Math.PI * 2 - Math.PI / 2;
            let distT = weightDistanceNorm(node.weight, minW, maxW);
            let orbit = minOrbit + distT * gap;

            const sinA = Math.sin(angle);
            if (sinA > 0.55) orbit += 28 + (sinA - 0.55) * 40;
            if (sinA > 0.25 && sinA <= 0.55) orbit += 12;

            const sizePx = nodeDiameter(node.valueT);
            return {
                ...node,
                angle,
                distT,
                orbit,
                sizePx,
                x: cx + Math.cos(angle) * orbit,
                y: cy + Math.sin(angle) * orbit,
            };
        });

        for (let pass = 0; pass < 3; pass++) {
            for (let i = 0; i < placed.length; i++) {
                for (let j = i + 1; j < placed.length; j++) {
                    const a = placed[i];
                    const b = placed[j];
                    const dx = b.x - a.x;
                    const dy = b.y - a.y;
                    const dist = Math.hypot(dx, dy) || 1;
                    const need = (a.sizePx + b.sizePx) / 2 + 52;
                    if (dist >= need) continue;
                    const push = (need - dist) / 2;
                    const ux = dx / dist;
                    const uy = dy / dist;
                    a.x -= ux * push;
                    a.y -= uy * push;
                    b.x += ux * push;
                    b.y += uy * push;
                    a.orbit = Math.hypot(a.x - cx, a.y - cy);
                    b.orbit = Math.hypot(b.x - cx, b.y - cy);
                }
            }
        }

        const maxR = maxOrbit + 36;
        return placed.map(n => {
            let orbit = Math.hypot(n.x - cx, n.y - cy);
            orbit = clamp(orbit, minOrbit, maxR);
            const angle = Math.atan2(n.y - cy, n.x - cx);
            return {
                ...n,
                angle,
                orbit,
                x: cx + Math.cos(angle) * orbit,
                y: cy + Math.sin(angle) * orbit,
                sizeClass: n.valueT >= 0.72 ? 'xl' : n.valueT >= 0.48 ? 'lg' : n.valueT >= 0.28 ? 'md' : 'sm',
                distClass: n.distT <= 0.28 ? 'near' : n.distT >= 0.72 ? 'far' : 'mid',
            };
        });
    }

    /** Place metric label outside the disc, away from the hub (no overlap with line/disc). */
    function chipOffset(angle, sizePx) {
        const pad = sizePx / 2 + 22;
        return {
            dx: Math.cos(angle) * pad,
            dy: Math.sin(angle) * pad,
        };
    }

    function renderGraph(player, valuation) {
        const W = 520;
        const H = 500;
        const cx = W / 2;
        const cy = H / 2 - 6;
        const hubR = 48;
        const minOrbit = hubR + 88;
        const maxOrbit = Math.min(W, H) / 2 - 44;

        const nodes = getGraphNodes(valuation);
        const faceUrl = (player.facecard_url || player.espn_portrait_url || '').trim();
        const name = player.player_name || valuation.player_name || 'Player';
        const role = valuation.role || player.pool_role || player.auction_role || '';
        const verdict = valuation.auction_verdict || '';

        const midOrbit = (minOrbit + maxOrbit) / 2;
        const guideRings = `
            <circle cx="${cx}" cy="${cy}" r="${minOrbit}" fill="none" stroke="rgba(0,74,153,0.07)" stroke-width="1"/>
            <circle cx="${cx}" cy="${cy}" r="${midOrbit}" fill="none" stroke="rgba(0,74,153,0.05)" stroke-width="1" stroke-dasharray="5 7"/>
            <circle cx="${cx}" cy="${cy}" r="${maxOrbit - 8}" fill="none" stroke="rgba(0,74,153,0.04)" stroke-width="1"/>
        `;

        const edges = nodes.map(n => {
            const rim = hubR + 6;
            const x1 = cx + Math.cos(n.angle) * rim;
            const y1 = cy + Math.sin(n.angle) * rim;
            const trim = n.sizePx / 2 + 8;
            const x2 = n.x - Math.cos(n.angle) * trim;
            const y2 = n.y - Math.sin(n.angle) * trim;
            const strokeW = 1.6 + (1 - n.distT) * 2.4;
            const dash = n.distT <= 0.35 ? 'none' : '6 7';
            return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${n.color}" stroke-width="${strokeW.toFixed(1)}" stroke-dasharray="${dash}" stroke-opacity="${(0.38 + (1 - n.distT) * 0.45).toFixed(2)}" stroke-linecap="round"/>`;
        }).join('');

        const nodeHtml = nodes.map(n => {
            const chip = chipOffset(n.angle, n.sizePx);
            return `
            <button type="button" class="arena-radial-node arena-radial-node--${n.sizeClass} arena-radial-node--${n.distClass}"
                data-metric-id="${escapeAttr(n.id)}"
                style="left:${n.x}px;top:${n.y}px;--node-accent:${n.color};--disc-size:${n.sizePx}px;--chip-dx:${chip.dx.toFixed(1)}px;--chip-dy:${chip.dy.toFixed(1)}px"
                aria-label="${escapeAttr(n.label)}: ${escapeAttr(n.detail)}. Click for explanation.">
                <span class="arena-radial-node__disc">
                    <span class="arena-radial-node__value">${escapeHtml(String(Math.round(n.rawValue)))}</span>
                </span>
                <span class="arena-radial-node__chip">${escapeHtml(n.label)}</span>
            </button>
        `;
        }).join('');

        const players = window.CSKDashboard?.getAllPlayers?.() || [];
        let hubInner = '';
        if (typeof CSKPolish !== 'undefined' && typeof CSKAvatars !== 'undefined') {
            const m = CSKPolish.poolMeta(name, players);
            hubInner = CSKAvatars.markup(name, 'arena-radial-hub__face player-hero__portrait', m.facecard_url, m.espn_portrait_url);
        } else if (faceUrl) {
            hubInner = `<img class="arena-radial-hub__img" src="${escapeAttr(faceUrl)}" alt="" loading="eager" decoding="async">`;
        } else {
            hubInner = `<span class="arena-radial-hub__initials">${initials(name)}</span>`;
        }

        return `
            <div class="arena-radial-wrap">
                ${confidenceBand(valuation)}
                <div class="arena-radial-graph" style="width:${W}px;height:${H}px">
                    <svg class="arena-radial-edges" width="${W}" height="${H}" aria-hidden="true">${guideRings}${edges}</svg>
                    <div class="arena-radial-hub" style="left:${cx}px;top:${cy}px;width:${hubR * 2}px;height:${hubR * 2}px">
                        ${hubInner}
                    </div>
                    ${nodeHtml}
                </div>
                <div id="arenaRadialMetricBrief" class="arena-radial-metric-brief-slot">
                    ${metricBriefIdleHtml()}
                </div>
                <div class="arena-radial-player-meta">
                    <strong>${escapeHtml(name)}</strong>
                    ${role ? `<span>${escapeHtml(role)}</span>` : ''}
                    ${verdict ? `<em>${escapeHtml(verdict)}</em>` : ''}
                </div>
                ${renderMetricGuide(nodes)}
            </div>
        `;
    }

    function renderLoading(player) {
        const name = player.player_name || 'Player';
        const shell = typeof CSKPolish !== 'undefined' ? CSKPolish.loadingShell(`Loading bid profile for ${name}…`) : `
            <div class="arena-radial-loading">
                <div class="pp-spinner"></div>
                <p>Loading bid profile for <strong>${escapeHtml(name)}</strong>…</p>
            </div>`;
        return shell;
    }

    function renderError(player, msg) {
        return `
            <div class="arena-radial-error">
                <p>${escapeHtml(msg)}</p>
                <button type="button" class="btn-secondary" data-radial-retry>Retry</button>
            </div>
        `;
    }

    function close() {
        if (escHandler) {
            document.removeEventListener('keydown', escHandler);
            escHandler = null;
        }
        overlayEl?.remove();
        overlayEl = null;
    }

    function valuationFromPoolPlayer(player) {
        const matches = int(player.matches_played);
        const price = float(player.bubble_price_cr || player.last_bid_cr || 2);
        return {
            player_name: player.player_name,
            role: player.pool_role || player.auction_role || 'Player',
            role_detail: 'Auction pool — limited stats in database',
            form_score: float(player.form_rating) || 50,
            csk_fit_score: 50,
            confidence: Math.min(55, matches * 2),
            estimated_value: price,
            floor_price: Math.max(0.2, price * 0.7),
            ceiling_price: price * 1.4,
            auction_verdict: '📋 Monitor',
            scarcity_bonus: 1,
            experience_factor: Math.min(1, matches / 50),
            last_10_runs: int(player.last_10_matches_runs),
            last_10_wickets: int(player.last_10_matches_wickets),
            injury_risk: 'Medium',
        };
    }

    function int(v) {
        return parseInt(v, 10) || 0;
    }

    function float(v) {
        return parseFloat(v) || 0;
    }

    async function fetchValuation(playerName, poolPlayer) {
        const r = await fetch(`${apiBase()}/players/valuation/${encodeURIComponent(playerName)}`);
        if (!r.ok) {
            if (poolPlayer) return valuationFromPoolPlayer(poolPlayer);
            const err = await r.json().catch(() => ({}));
            throw new Error(err.detail || `Player not in stats database — pool-only profile`);
        }
        return r.json();
    }

    function renderDecisionStrip(decision) {
        if (!decision?.quick_decision) return '';
        const q = decision.quick_decision;
        const cls = q.should_bid === 'YES' ? 'arena-radial-verdict--yes'
            : q.should_bid === 'MAYBE' ? 'arena-radial-verdict--maybe' : 'arena-radial-verdict--no';
        return `
            <div class="arena-radial-verdict ${cls}">
                <strong>${escapeHtml(q.should_bid || '—')}</strong>
                <span>Walk-away <b>₹${Number(q.walk_away_cr || 0).toFixed(1)} Cr</b></span>
                <span>FMV ₹${Number(q.fair_market_value_cr || 0).toFixed(1)} Cr</span>
                <p>${escapeHtml(q.one_liner || '')}</p>
            </div>`;
    }

    async function loadBody(player, bodyEl, footerEl) {
        let valuation = null;
        let decision = null;
        try {
            valuation = await fetchValuation(player.player_name, player);
            try {
                const squad = window.CSKArena?.getArenaSquad?.() || [];
                decision = await window.CSKDashboard?.fetchWarRoomDecision?.(player.player_name, {
                    squadOverride: squad.length ? squad : undefined,
                    budget: window.CSKDashboard?.IPL_PURSE_CR || 125,
                });
            } catch (wrErr) {
                console.warn('Radial war-room:', wrErr);
            }
            bodyEl.innerHTML = renderDecisionStrip(decision) + renderGraph(player, valuation);
            attachMetricBriefInteractions(bodyEl, valuation);
            if (typeof CSKPolish !== 'undefined') CSKPolish.bindHero(bodyEl);

            if (footerEl && decision?.quick_decision) {
                const wa = Number(decision.quick_decision.walk_away_cr || 0).toFixed(1);
                footerEl.querySelector('[data-radial-walkaway]')?.setAttribute('data-walkaway-cr', wa);
                const waBtn = footerEl.querySelector('[data-radial-walkaway]');
                if (waBtn) waBtn.textContent = `Walk-away ₹${wa} Cr`;
            }
        } catch (e) {
            const fallback = valuationFromPoolPlayer(player);
            bodyEl.innerHTML = `
                <p class="arena-radial-pool-note">${escapeHtml(e.message || 'Limited data')} — showing pool estimate.</p>
                ${renderGraph(player, fallback)}`;
            attachMetricBriefInteractions(bodyEl, fallback);
            if (typeof CSKPolish !== 'undefined') CSKPolish.bindHero(bodyEl);
        }
    }

    function open(player) {
        if (!player?.player_name) return;
        close();

        const name = player.player_name;
        overlayEl = document.createElement('div');
        overlayEl.className = 'arena-radial-overlay';
        overlayEl.innerHTML = `
            <div class="arena-radial-backdrop" data-radial-close></div>
            <div class="arena-radial-modal" role="dialog" aria-labelledby="arenaRadialTitle" aria-modal="true">
                <button type="button" class="pp-close arena-radial-close" data-radial-close aria-label="Close">×</button>
                <header class="arena-radial-header">
                    <h2 id="arenaRadialTitle">Bid intelligence</h2>
                    <p class="arena-radial-sub">Weighted factors for <strong>${escapeHtml(name)}</strong></p>
                </header>
                <div class="arena-radial-body" id="arenaRadialBody"></div>
                <footer class="arena-radial-footer">
                    <button type="button" class="btn-primary" data-radial-add>Add to squad</button>
                    <button type="button" class="btn-secondary" data-radial-walkaway>Walk-away</button>
                    <button type="button" class="btn-secondary" data-radial-pass>Pass</button>
                    <button type="button" class="btn-link-sm" data-radial-valuation>Valuation</button>
                    <button type="button" class="btn-link-sm" data-radial-advisor>Bid advisor</button>
                    <button type="button" class="btn-link-sm" data-radial-close>Close</button>
                </footer>
            </div>
        `;

        document.body.appendChild(overlayEl);

        overlayEl.querySelectorAll('[data-radial-close]').forEach(el => {
            el.addEventListener('click', close);
        });
        overlayEl.querySelector('[data-radial-valuation]')?.addEventListener('click', () => {
            close();
            window.CSKDashboard?.openValuation?.(name);
        });
        overlayEl.querySelector('[data-radial-advisor]')?.addEventListener('click', () => {
            close();
            window.CSKDashboard?.openBidAdvisor?.(name);
        });
        overlayEl.querySelector('[data-radial-add]')?.addEventListener('click', async () => {
            if (window.CSKArena?.acquirePlayer) {
                await window.CSKArena.acquirePlayer(name);
            }
            close();
        });
        overlayEl.querySelector('[data-radial-pass]')?.addEventListener('click', () => {
            window.CSKArena?.flash?.(`Passed on ${name}`);
            close();
        });
        overlayEl.querySelector('[data-radial-walkaway]')?.addEventListener('click', () => {
            const wa = overlayEl.querySelector('[data-radial-walkaway]')?.getAttribute('data-walkaway-cr');
            window.CSKArena?.flash?.(wa ? `Walk-away ₹${wa} Cr — ${name}` : `Walk-away — ${name}`);
            close();
        });

        escHandler = e => {
            if (e.key === 'Escape') close();
        };
        document.addEventListener('keydown', escHandler);

        const bodyEl = overlayEl.querySelector('#arenaRadialBody');
        const footerEl = overlayEl.querySelector('.arena-radial-footer');
        bodyEl.innerHTML = renderLoading(player);
        loadBody(player, bodyEl, footerEl);
    }

    const api = { open, close };
    window.ArenaRadial = api;
    return api;
})();
