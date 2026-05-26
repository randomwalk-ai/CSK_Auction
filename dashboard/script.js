// API — use localhost:8000; warn if opened as file:// (blocks fetch)
const API_BASE = 'http://127.0.0.1:8000/api';
const IPL_PURSE_CR = 125;
const IPL_AUCTION_YEAR = 2026;

function verdictFromApi(verdict) {
    if (!verdict) return { cls: 'verdict-monitor', text: 'Monitor' };
    if (verdict.includes('MUST')) return { cls: 'verdict-mustbuy', text: verdict };
    if (verdict.includes('Strong')) return { cls: 'verdict-target', text: verdict };
    if (verdict.includes('Development')) return { cls: 'verdict-development', text: verdict };
    if (verdict.includes('Value')) return { cls: 'verdict-value', text: verdict };
    if (verdict.includes('Overpriced')) return { cls: 'verdict-avoid', text: verdict };
    if (verdict.includes('Avoid')) return { cls: 'verdict-avoid', text: verdict };
    if (verdict.includes('Uncertainty')) return { cls: 'verdict-monitor', text: verdict };
    return { cls: 'verdict-monitor', text: verdict };
}

function renderMarketBand(marketValue, median) {
    if (!marketValue || marketValue.p90 == null) return '';
    const p10 = marketValue.p10 ?? 0;
    const p90 = marketValue.p90 ?? median;
    const span = Math.max(p90 - p10, 0.1);
    const pct = (v) => Math.min(100, Math.max(0, ((v - p10) / span) * 100));
    const m = median ?? marketValue.p50;
    return `
        <div class="price-band-block">
            <div class="price-band-labels">
                <span>p10 ₹${p10} Cr</span>
                <span>p50 ₹${marketValue.p50 ?? m} Cr</span>
                <span>p90 ₹${p90} Cr</span>
            </div>
            <div class="price-band-track">
                <div class="price-band-fill"></div>
                <div class="price-band-marker price-band-marker--p50" style="left:${pct(marketValue.p50 ?? m)}%"></div>
                <div class="price-band-marker price-band-marker--est" style="left:${pct(m)}%" title="Estimate"></div>
            </div>
        </div>`;
}

function renderConfidenceWarning(v) {
    const low = (v.confidence != null && v.confidence < 50) || (v.matches_played != null && v.matches_played < 25);
    if (!low) return '';
    return `
        <div class="confidence-warning">
            Limited IPL sample (${v.matches_played ?? '?'} matches, ${v.confidence ?? '?'}% confidence).
            Treat valuation as indicative — engine: ${v.engine_version || 'franchise_v2'}.
        </div>`;
}

// Global state
let currentSquad = [];
let allPlayers = [];
let _bidAdvisorPlayer = '';
let _valuationPrefill = '';
let _poolPreviewCache = null;
let _poolFilter = 'batters';
let _activeTab = 'squad';
let _initialRouteDone = false;
let squadXai = null;
let squadProvenance = {};

const TAB_HINTS = {
    squad: 'Official IPL 2026 CSK squad (25). Roster ≠ 2025 DB. Form/SR/Econ = historical stats only.',
    bidadvisor: 'Should we bid, walk-away price, and rivals — uses your squad plus bid history.',
    players: 'Search any player — click for FMV, then Bid Advisor. Browse defaults to IPL 2026 auction pool.',
    arena: 'Drag bubbles from the 2026 auction pool into your squad — purse & gaps update live.',
    compare: 'Compare two players side by side.',
    valuation: 'Fair price and CSK fit for one player.',
};

// Official IPL 2026 CSK roster — used to strip stale DB/cache players (Jadeja, Pathirana, etc.)
const OFFICIAL_2026_NAMES = new Set([
    'Ruturaj Gaikwad','Sanju Samson','MS Dhoni','Dewald Brevis','Ayush Mhatre','Urvil Patel',
    'Shivam Dube','Jamie Overton','Ramakrishna Ghosh','Noor Ahmad','Khaleel Ahmed','Anshul Kamboj',
    'Gurjapneet Singh','Shreyas Gopal','Mukesh Choudhary','Nathan Ellis','Kartik Sharma',
    'Prashant Veer','Rahul Chahar','Akeal Hosein','Matt Henry','Matthew Short','Aman Khan',
    'Sarfaraz Khan','Zakary Foulkes',
].map(n => n.toLowerCase()));

function filterToOfficial2026Roster(squad) {
    return dedupeSquad(squad).filter(p => OFFICIAL_2026_NAMES.has(p.name.trim().toLowerCase()));
}

const SQUAD_TARGET = 25;

// Initialize dashboard — always fetch fresh squad from API
document.addEventListener('DOMContentLoaded', async () => {
    setupEventListeners();
    showFileProtocolWarning();
    const apiOk = await checkApiHealth();
    if (apiOk) {
        await loadSquad(true);
        if (currentSquad.length < SQUAD_TARGET) {
            await reloadSquadFromApi({ rerender: false });
        }
        await loadLastUpdate();
    }
    if (!_initialRouteDone) {
        await showTab('squad');
    }
});

function showFileProtocolWarning() {
    const el = document.getElementById('fileProtocolWarn');
    if (!el || window.location.protocol !== 'file:') return;
    el.hidden = false;
    el.innerHTML = `
        <strong>Open the dashboard via a local server</strong> (not by double-clicking the HTML file).
        From repo root run <code>./start.sh</code> or:
        <code>cd dashboard && python3 -m http.server 8080</code> then open
        <a href="http://127.0.0.1:8080">http://127.0.0.1:8080</a>`;
}

async function checkApiHealth() {
    const el = document.getElementById('apiStatusWarn');
    try {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 4000);
        const res = await fetch(`${API_BASE}/health`, { signal: ctrl.signal });
        clearTimeout(timer);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        if (el) {
            el.hidden = true;
            el.textContent = '';
        }
        return true;
    } catch (_) {
        if (el) {
            el.hidden = false;
            el.innerHTML = `
                <strong>API offline</strong> — start the backend on port 8000.
                From repo root: <code>./start.sh</code> or
                <code>cd api && python3 -m uvicorn app:app --reload --port 8000</code>
                then <button type="button" class="btn-link-inline" onclick="checkApiHealth().then(ok => ok && location.reload())">Retry</button>`;
        }
        return false;
    }
}

function computeOverseas(p) {
    if (p.overseas === true) return true;
    if (p.overseas === false) return false;
    const c = String(p.country || p.nationality || 'India').trim().toLowerCase();
    return c !== '' && c !== 'india' && c !== 'indian' && c !== 'ind' && c !== 'overseas';
}

function dedupeSquad(squad) {
    const byName = new Map();
    for (const raw of squad) {
        const p = normalizeSquadPlayer(raw);
        const key = p.name.trim().toLowerCase();
        if (!key) continue;
        if (!byName.has(key)) byName.set(key, p);
    }
    return Array.from(byName.values());
}

function buildProvenanceMap(list) {
    const map = {};
    if (!Array.isArray(list)) return map;
    for (const row of list) {
        if (!row?.name) continue;
        map[row.name.trim().toLowerCase()] = row;
    }
    return map;
}

function getPlayerXai(player) {
    return squadProvenance[player.name.trim().toLowerCase()] || null;
}

function playerStatusBadge(player) {
    if (player.acquisition === 'trade') {
        return '<span class="retained-badge retained-badge--trade">Traded</span>';
    }
    if (player.retained) {
        return '<span class="retained-badge">Retained</span>';
    }
    return '<span class="retained-badge retained-badge--new">Acquired</span>';
}

function renderXaiTrustBadge(xai, player) {
    if (player?.price_verified) {
        const isHammer = player.price_source === 'cricbuzz_scrape_csv'
            || player.price_source === 'bid_history_db';
        const label = isHammer ? 'Bid ✓' : 'Official ✓';
        const tip = (player.price_note || (isHammer ? '2026 Cricbuzz hammer' : 'IPL 2026 official squad price')).replace(/"/g, '&quot;');
        const cls = isHammer ? 'xai-badge--verified' : 'xai-badge--official';
        return `<span class="xai-badge ${cls}" title="${tip}">${label}</span>`;
    }
    if (player?.price_estimated) {
        const isGroq = player.price_source === 'groq_public';
        const label = isGroq ? 'Groq ~' : 'Press ~';
        const tip = (player.price_note || (isGroq ? 'Groq IPL 2026 public price' : 'IPL 2026 squad list')).replace(/"/g, '&quot;');
        const cls = isGroq ? 'xai-badge--groq' : 'xai-badge--press';
        return `<span class="xai-badge ${cls}" title="${tip}">${label}</span>`;
    }
    return '<span class="xai-badge xai-badge--warn" title="Set GROQ_API_KEY or run scrape">No price</span>';
}

function renderSquadXaiPanel() {
    if (!squadXai) return '';

    const g = squadXai.grounding || {};
    const bidVerified = g.verified_auction_prices ?? g.verified_in_db ??
        currentSquad.filter(p => p.price_verified).length;
    const squadSize = g.squad_size ?? currentSquad.length;
    const groqEst = g.groq_estimated_prices ?? currentSquad.filter(p => p.price_source === 'groq_public').length;
    const pressEst = g.press_estimated_prices ??
        currentSquad.filter(p => p.price_estimated && p.price_source === 'press_catalog').length;
    const avgConf = g.avg_confidence_pct ??
        (squadSize ? Math.round(currentSquad.reduce((s, p) => {
            if (p.price_verified) return s + 95;
            if (p.price_source === 'groq_public') return s + 70;
            if (p.price_estimated) return s + 55;
            return s + (p.price ? 50 : 0);
        }, 0) / squadSize) : 0);
    const risk = g.hallucination_risk || 'unknown';
    const riskCls = risk === 'low' ? 'xai-risk--low' : risk === 'medium' ? 'xai-risk--med' : 'xai-risk--high';
    const trace = (squadXai.decision_trace || []).map(t => `<li>${t}</li>`).join('');
    const flags = (squadXai.hallucination_flags || []).slice(0, 6);
    const flagHtml = flags.length
        ? `<ul class="xai-flags">${flags.map(f => `<li>${f}</li>`).join('')}</ul>`
        : '<p class="xai-flags-empty">Roster matches official IPL 2026 CSK squad list.</p>';

    return `
    <section class="xai-panel" aria-label="Explainable AI squad validation">
        <div class="xai-panel-head">
            <div>
                <h2 class="xai-title">IPL 2026 squad source</h2>
                <p class="xai-sub"><strong>Bid ✓</strong> = 2026 scrape. <strong>Groq ~</strong> / <strong>Press ~</strong> = estimated IPL 2026 prices. Never uses 2025 DB retained amounts.</p>
            </div>
            <span class="xai-risk ${riskCls}">Hallucination risk: ${risk.toUpperCase()}</span>
        </div>
        <div class="xai-metrics">
            <div class="xai-metric">
                <span class="xai-metric-label">Bid ✓ prices</span>
                <span class="xai-metric-value">${bidVerified} / ${squadSize}</span>
            </div>
            <div class="xai-metric">
                <span class="xai-metric-label">Est ~ (Groq / Press)</span>
                <span class="xai-metric-value">${groqEst} / ${pressEst}</span>
            </div>
            <div class="xai-metric">
                <span class="xai-metric-label">Avg confidence</span>
                <span class="xai-metric-value">${avgConf}%</span>
            </div>
            <div class="xai-metric">
                <span class="xai-metric-label">Missing price</span>
                <span class="xai-metric-value">${g.prices_missing ?? currentSquad.filter(p => !p.price).length}</span>
            </div>
        </div>
        <div class="xai-columns">
            <div class="xai-col">
                <h3>Decision trace</h3>
                <ul class="xai-trace">${trace}</ul>
                <p class="xai-policy">${squadXai.anti_hallucination_policy || ''}</p>
            </div>
            <div class="xai-col">
                <h3>Flagged / unverified</h3>
                ${flagHtml}
            </div>
        </div>
    </section>`;
}

function normalizeSquadPlayer(p) {
    const country = p.country || p.nationality || 'India';
    const hasPrice = p.price != null && Number(p.price) > 0;
    const priceVerified = p.price_verified === true && hasPrice;
    const priceEstimated = p.price_estimated === true && hasPrice && !priceVerified;
    const normalized = {
        name: p.name,
        role: p.role || 'Player',
        price: hasPrice ? Number(p.price) : null,
        price_verified: priceVerified,
        price_estimated: priceEstimated,
        price_source: p.price_source || null,
        price_confidence: p.price_confidence || null,
        price_note: p.price_note || null,
        country,
        retained: p.retained === true,
        acquisition: p.acquisition || (p.retained === true ? 'retained' : 'auction'),
        style: p.style,
        overseas: computeOverseas({ ...p, country }),
    };
    return normalized;
}

function formatPlayerPrice(player) {
    if (player.price_verified && player.price != null) {
        return `₹${Number(player.price).toFixed(1)}<span class="price-unit"> Cr</span>`;
    }
    if (player?.price_estimated && player.price != null) {
        const src = player.price_source === 'groq_public' ? 'Groq' : 'Press';
        const tip = (player.price_note || `${src} IPL 2026 estimate`).replace(/"/g, '&quot;');
        return `₹${Number(player.price).toFixed(1)}<span class="price-unit"> Cr</span> <span class="price-groq" title="${tip}">~</span>`;
    }
    const tip = (player.price_note || 'Add GROQ_API_KEY or run Cricbuzz scrape').replace(/"/g, '&quot;');
    return `<span class="price-tbc" title="${tip}">TBC</span>`;
}

function pricedPurseTotal() {
    return currentSquad.reduce((sum, p) => sum + (p.price ? p.price : 0), 0);
}

function verifiedPurseTotal() {
    return currentSquad.reduce((sum, p) => sum + (p.price_verified && p.price ? p.price : 0), 0);
}

// Load squad — API first for real-time full roster; localStorage only if API down
async function loadSquad(forceApi = false) {
    let loaded = false;
    let loadMeta = { source: null, note: null };

    async function applyApiData(data) {
        currentSquad = filterToOfficial2026Roster(data.squad || []);
        squadXai = data.xai || null;
        squadProvenance = buildProvenanceMap(data.player_provenance);
        saveSquad();
        loaded = true;
        loadMeta.source = data.source || 'api';
        loadMeta.note = data.note || null;
        loadMeta.xai = squadXai;
    }

    if (forceApi || currentSquad.length < SQUAD_TARGET) {
        try {
            const src = forceApi ? 'auto' : 'auto';
            const response = await fetch(`${API_BASE}/csk-squad?source=${src}&year=2026`);
            const data = await response.json().catch(() => ({}));
            if (response.ok && data.squad && data.squad.length > 0) {
                await applyApiData(data);
            } else {
                console.warn('CSK squad API:', response.status, data.detail || data);
                loadMeta.error = data.detail || `HTTP ${response.status}`;
            }
        } catch (e) {
            console.warn('Could not fetch CSK squad (is API on :8000?)', e);
            loadMeta.error = 'Cannot reach API on localhost:8000 — run: cd api && python app.py';
        }
    }

    if (!loaded && !forceApi) {
        try {
            const saved = localStorage.getItem('csk_squad');
            if (saved) {
                const parsed = JSON.parse(saved);
                if (Array.isArray(parsed) && parsed.length > 0) {
                    currentSquad = filterToOfficial2026Roster(parsed);
                    loaded = true;
                    loadMeta.source = 'browser_storage';
                    try {
                        const xaiSaved = localStorage.getItem('csk_squad_xai');
                        if (xaiSaved) {
                            const x = JSON.parse(xaiSaved);
                            squadXai = x.xai || null;
                            squadProvenance = x.provenance || {};
                        }
                    } catch (_) { /* ignore stale xai cache */ }
                }
            }
        } catch (e) {
            console.warn('Invalid csk_squad in localStorage', e);
        }
    }

    updatePurseDisplay();
    return { loaded, ...loadMeta };
}

async function reloadSquadFromApi(opts = {}) {
    const { rerender = true } = opts;
    const backup = [...currentSquad];
    localStorage.removeItem('csk_squad');
    currentSquad = [];
    squadXai = null;
    squadProvenance = {};

    const result = await loadSquad(true);
    updatePurseDisplay();

    if (result.loaded) {
        const src = result.source === 'groq_validated'
            ? 'Groq + local DB validation (XAI)'
            : result.source === 'official_catalog_2026'
                ? 'official IPL 2026 CSK roster (Sportstar/Hindu + DB prices)'
                : result.source === 'local_db'
                ? 'local database (retained + auction wins)'
                : result.source === 'live_api'
                    ? 'live CSK squad API'
                    : result.source || 'API';
        if (rerender && _activeTab === 'squad') {
            await showTab('squad');
        }
        window._squadLoadNotice = `Squad loaded (${currentSquad.length} players) from ${src}.`;
        if (result.note) window._squadLoadNotice += ' ' + result.note;
    } else {
        if (backup.length > 0) {
            currentSquad = backup;
            saveSquad();
        }
        const msg = result.error
            || 'Could not load squad. Restart API: cd api && python app.py';
        window._squadLoadNotice = null;
        alert(msg);
        if (rerender && _activeTab === 'squad') {
            await showTab('squad');
        }
    }
}

async function resetSquadStorage() {
    localStorage.removeItem('csk_squad');
    localStorage.removeItem('csk_squad_xai');
    currentSquad = [];
    squadXai = null;
    squadProvenance = {};
    await loadSquad(true);
    updatePurseDisplay();
    window._squadLoadNotice = currentSquad.length
        ? `Squad reset to ${currentSquad.length} players from official data.`
        : 'Could not load squad from API.';
    if (_activeTab === 'squad') {
        await showTab('squad');
    }
}

function saveSquad() {
    if (currentSquad.length > 0) {
        localStorage.setItem('csk_squad', JSON.stringify(currentSquad));
    }
    if (squadXai) {
        localStorage.setItem('csk_squad_xai', JSON.stringify({ xai: squadXai, provenance: squadProvenance }));
    }
}

function openBidAdvisor(playerName) {
    if (playerName) _bidAdvisorPlayer = playerName.trim();
    showTab('bidadvisor');
}

function openValuation(playerName) {
    if (playerName) _valuationPrefill = playerName.trim();
    showTab('valuation');
}

function updateTabHint(tabName) {
    const el = document.getElementById('tabHint');
    if (el) el.textContent = TAB_HINTS[tabName] || '';
}

function updatePurseDisplay() {
    const totalSpent = pricedPurseTotal();
    const verifiedSpent = verifiedPurseTotal();
    const pricedCount = currentSquad.filter(p => p.price).length;
    const estCount = currentSquad.filter(p => p.price_estimated).length;
    const remaining = IPL_PURSE_CR - totalSpent;
    const spentEl = document.getElementById('spentAmount');
    const remainEl = document.getElementById('remainingAmount');
    if (spentEl) {
        spentEl.innerHTML = estCount > 0
            ? `₹${totalSpent.toFixed(2)} Cr <span class="purse-sub">(${verifiedSpent.toFixed(1)} Bid ✓ + ${estCount} est ~)</span>`
            : `₹${totalSpent.toFixed(2)} Cr`;
    }
    if (remainEl) {
        remainEl.innerHTML = pricedCount < currentSquad.length
            ? `≥ ₹${remaining.toFixed(2)} Cr <span class="purse-sub">(${currentSquad.length - pricedCount} TBC)</span>`
            : `₹${remaining.toFixed(2)} Cr`;
    }
    updateExecutiveKpis(totalSpent, remaining, pricedCount, estCount);
}

function updateExecutiveKpis(spent, remaining, pricedCount, groqCount = 0) {
    const rc = squadRoleCounts();
    const overseas = currentSquad.filter(p => p.overseas === true).length;
    const pct = Math.min(100, Math.round((spent / IPL_PURSE_CR) * 100));
    const allPriced = pricedCount >= currentSquad.length;

    const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
    set('kpiSquadSize', `${currentSquad.length} / 25`);
    if (currentSquad.length > 25) {
        set('kpiSquadSize', `${currentSquad.length} / 25 ⚠`);
    }
    set('kpiPlayingXi', `Playing XI: ${Math.min(11, currentSquad.length)}`);
    set('kpiPursePct', allPriced && !groqCount ? `${pct}%` : `${pct}%*`);
    set('kpiOverseas', `${overseas} / 8`);

    const bar = document.getElementById('budgetBarFill');
    if (bar) {
        bar.style.width = `${pct}%`;
        bar.classList.toggle('budget-bar-fill--warn', pct > 85);
    }

    const pills = document.getElementById('kpiRolePills');
    if (pills) {
        pills.innerHTML = `
            <span class="role-pill role-pill--bat">BAT ${rc.Batter}</span>
            <span class="role-pill role-pill--bowl">BOWL ${rc.Bowler}</span>
            <span class="role-pill role-pill--ar">AR ${rc['All Rounder']}</span>
            <span class="role-pill role-pill--wk">WK ${rc['Wicket Keeper']}</span>`;
    }
}

async function loadLastUpdate() {
    try {
        await fetch(`${API_BASE}/stats/summary`);
        document.getElementById('lastUpdate').innerHTML = `Updated: ${new Date().toLocaleDateString()}`;
    } catch {
        document.getElementById('lastUpdate').innerHTML = 'Live data connected';
    }
}

function getRoleClass(role) {
    return { 'Batter': 'role-batter', 'Bowler': 'role-bowler',
             'All Rounder': 'role-allrounder', 'Wicket Keeper': 'role-keeper' }[role] || 'role-default';
}
function getFormClass(score) {
    return score >= 70 ? 'form-score-high' : score >= 50 ? 'form-score-mid' : 'form-score-low';
}
function getProgressClass(score) {
    return score >= 70 ? 'progress-high' : score >= 50 ? 'progress-mid' : 'progress-low';
}

async function fetchPlayerStats(playerName) {
    try {
        const r = await fetch(`${API_BASE}/players/valuation/${encodeURIComponent(playerName)}`);
        if (r.ok) return await r.json();
    } catch { /* offline */ }
    try {
        const r = await fetch(`${API_BASE}/players/search?name=${encodeURIComponent(playerName)}&limit=1`);
        if (r.ok) {
            const rows = await r.json();
            const row = rows?.[0];
            if (row) {
                return {
                    form_score: row.form_rating,
                    form_rating: row.form_rating,
                    last_10_sr: row.last_10_matches_sr ?? row.strike_rate,
                    career_sr: row.strike_rate,
                    last_10_econ: row.last_10_matches_economy ?? row.economy_rate,
                    career_econ: row.economy_rate,
                };
            }
        }
    } catch { /* offline */ }
    return null;
}

function escapeAttr(str) {
    return String(str || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function closePlayerPreview() {
    document.getElementById('playerPreviewOverlay')?.remove();
    _poolPreviewCache = null;
    document.removeEventListener('keydown', _playerPreviewEscHandler);
}

function _playerPreviewEscHandler(e) {
    if (e.key === 'Escape') closePlayerPreview();
}

function renderPlayerPreviewBody(v, listPlayer, inSquad) {
    if (!v) {
        return `
            <div class="pp-loading">
                <div class="pp-spinner"></div>
                <p>Loading FMV, bid range & CSK fit…</p>
            </div>`;
    }
    if (v.detail) {
        return `<div class="empty-state">${escapeAttr(v.detail)}</div>`;
    }

    const { cls: verdictClass, text: verdictText } = verdictFromApi(v.auction_verdict);
    const formScore = v.form_score ?? v.form_rating ?? listPlayer?.form_rating ?? 0;
    const formRaw = v.form_score_raw ?? formScore;
    const formBucket = v.form_role_bucket || '';
    const fClass = getFormClass(formScore);
    const pClass = getProgressClass(formScore);
    const fmv = v.estimated_value ?? '—';
    const floor = v.floor_price ?? '—';
    const ceiling = v.ceiling_price ?? '—';
    const histLine = v.historical_auction_price
        ? `<div class="pp-hist">Last IPL auction: <strong>₹${v.historical_auction_price} Cr</strong></div>`
        : '';
    const jsSafe = String(v.player_name).replace(/\\/g, '\\\\').replace(/'/g, "\\'");

    return `
        <div class="pp-header">
            <div>
                <h2 class="pp-name">${v.player_name}</h2>
                <p class="pp-meta">${v.role || 'Player'}${v.country ? ` · ${v.country}` : ''} · Form ${Math.round(formScore)}${formBucket ? ` (${formBucket})` : ''} · CSK fit ${v.csk_fit_score ?? '—'}%</p>
            </div>
            <div class="pp-fmv-block">
                <span class="pp-fmv-label">Fair market value</span>
                <span class="pp-fmv-value">₹${fmv} Cr</span>
                <span class="pp-range">Bid range ₹${floor} – ₹${ceiling} Cr</span>
            </div>
        </div>

        ${renderConfidenceWarning(v)}

        <div class="pp-verdict-row">
            <span class="verdict-badge ${verdictClass}">${verdictText}</span>
            ${inSquad ? '<span class="pp-in-squad">Already in squad</span>' : ''}
        </div>

        ${renderMarketBand(v.market_value, v.estimated_value)}
        ${histLine}

        <div class="pp-metrics">
            <div class="pp-metric"><span>Form (role)</span><strong>${Math.round(formScore)}</strong></div>
            <div class="pp-metric"><span>Form raw</span><strong>${Math.round(formRaw)}</strong></div>
            <div class="pp-metric"><span>CSK fit</span><strong>${v.csk_fit_score ?? '—'}</strong></div>
            <div class="pp-metric"><span>FMV</span><strong>₹${fmv} Cr</strong></div>
        </div>

        <div class="progress-bar" style="margin: 12px 0 0">
            <div class="progress-fill ${pClass}" style="width:${Math.min(100, formScore)}%"></div>
        </div>

        ${(v.csk_fit_reasons || []).length ? `
        <div class="pp-reasons">
            <strong>CSK fit</strong>
            <ul>${(v.csk_fit_reasons || []).slice(0, 3).map(r => `<li>${r}</li>`).join('')}</ul>
        </div>` : ''}

        <div class="pp-actions">
            <button type="button" class="btn-primary" onclick="openBidAdvisorFromPreview('${jsSafe}')">
                Open Bid Advisor →
            </button>
            ${!inSquad ? `
            <button type="button" class="btn-secondary" onclick="addToSquadFromPreview()">
                Add to squad @ ₹${fmv} Cr
            </button>` : ''}
            <button type="button" class="btn-link-sm" onclick="openValuationFromPreview('${jsSafe}')">Full valuation</button>
        </div>
        <p class="pp-hint">Bid Advisor merges FMV with your squad gaps, purse left & bid-war history.</p>`;
}

function mountPlayerPreviewModal(playerName, bodyHtml) {
    closePlayerPreview();
    const overlay = document.createElement('div');
    overlay.id = 'playerPreviewOverlay';
    overlay.className = 'pp-overlay';
    overlay.innerHTML = `
        <div class="pp-backdrop" onclick="closePlayerPreview()" aria-hidden="true"></div>
        <div class="pp-modal" role="dialog" aria-labelledby="ppTitle" aria-modal="true">
            <button type="button" class="pp-close" onclick="closePlayerPreview()" aria-label="Close">×</button>
            <div id="ppTitle" class="visually-hidden">Player preview: ${escapeAttr(playerName)}</div>
            <div id="playerPreviewBody">${bodyHtml}</div>
        </div>`;
    document.body.appendChild(overlay);
    document.addEventListener('keydown', _playerPreviewEscHandler);
}

async function openPlayerPreview(playerName) {
    const name = (playerName || '').trim();
    if (!name) return;
    const inSquad = currentSquad.some(p => p.name === name);
    const listPlayer = allPlayers.find(p => p.player_name === name) || {};
    mountPlayerPreviewModal(name, renderPlayerPreviewBody(null, listPlayer, inSquad));

    const stats = await fetchPlayerStats(name);
    _poolPreviewCache = stats;
    const body = document.getElementById('playerPreviewBody');
    if (body) body.innerHTML = renderPlayerPreviewBody(stats, listPlayer, inSquad);
}

function openBidAdvisorFromPreview(playerName) {
    closePlayerPreview();
    openBidAdvisor(playerName);
}

function openValuationFromPreview(playerName) {
    closePlayerPreview();
    openValuation(playerName);
}

function addToSquadFromPreview() {
    const v = _poolPreviewCache;
    if (!v?.player_name) return;
    const price = v.estimated_value || 0.5;
    if (!confirm(`Add ${v.player_name} to squad at FMV ₹${price} Cr?\n\nUse Bid Advisor for entry / walk-away limits.`)) return;
    closePlayerPreview();
    addToSquad(v.player_name, price);
}

// ─────────────────────────────────────────────────────────────────────
// SQUAD TAB — vertical list layout
// ─────────────────────────────────────────────────────────────────────

async function renderSquadTab() {
    const tabAtStart = _activeTab;
    const playing11 = currentSquad.slice(0, 11);
    const bench     = currentSquad.slice(11);

    // Fetch all stats in parallel
    const [xi_stats, bench_stats] = await Promise.all([
        Promise.all(playing11.map(p => fetchPlayerStats(p.name))),
        Promise.all(bench.map(p     => fetchPlayerStats(p.name))),
    ]);

    function rowHtml(player, idx, stats, slotLabel) {
        const form   = stats?.form_score ?? stats?.form_rating ?? null;
        const sr     = stats?.last_10_sr ?? stats?.career_sr ?? null;
        const econ   = stats?.last_10_econ ?? stats?.career_econ ?? null;
        const pct    = Math.min(100, form ?? 0);
        const fClass = getFormClass(form ?? 0);
        const pClass = getProgressClass(form ?? 0);

        return `
        <div class="squad-row ${slotLabel === 'PLAYING' ? 'squad-row--xi' : 'squad-row--bench'}">
            <div class="sr-slot">${slotLabel === 'PLAYING' ? idx : idx}</div>

            <div class="sr-identity">
                <span class="player-name">${player.name}</span>
                ${renderXaiTrustBadge(getPlayerXai(player), player)}
                ${playerStatusBadge(player)}
                <span class="role-badge ${getRoleClass(player.role)}">${player.role}</span>
            </div>

            <div class="sr-stats">
                <div class="sr-stat">
                    <span class="sr-stat-label">FORM</span>
                    <span class="sr-stat-value ${fClass}">${form !== null ? form : '—'}</span>
                </div>
                <div class="sr-stat">
                    <span class="sr-stat-label">SR</span>
                    <span class="sr-stat-value">${sr !== null ? sr : '—'}</span>
                </div>
                <div class="sr-stat">
                    <span class="sr-stat-label">ECON</span>
                    <span class="sr-stat-value">${econ !== null ? econ : '—'}</span>
                </div>
            </div>

            <div class="sr-bar-wrap">
                <div class="progress-bar" style="margin:0">
                    <div class="progress-fill ${pClass}" style="width:${pct}%"></div>
                </div>
            </div>

            <div class="sr-price">${formatPlayerPrice(player)}</div>
            <div class="sr-actions">
                <button class="btn-action btn-action--bid" onclick="openBidAdvisor('${player.name.replace(/'/g, "\\'")}')" title="Bid advice">Advisor</button>
                <button class="btn-action btn-action--remove" onclick="removeFromSquad('${player.name.replace(/'/g, "\\'")}')" title="Remove">✕</button>
            </div>
        </div>`;
    }

    let html = '';
    if (window._squadLoadNotice) {
        html += `<div class="wr-squad-warn" style="background:#ecfdf5;border-color:#10b981;color:#065f46">${window._squadLoadNotice}</div>`;
        window._squadLoadNotice = null;
    }

    html += renderSquadXaiPanel();

    if (currentSquad.length > 0 && currentSquad.length < SQUAD_TARGET) {
        html += `<div class="wr-squad-warn">Showing ${currentSquad.length}/${SQUAD_TARGET} players — click <strong>Sync Squad</strong> to load the full IPL 2026 roster.</div>`;
    }

    html += `
        <div class="section-header section-header--exec">
            <div>
                <h2>Full Squad <span class="section-count">(${currentSquad.length}/${SQUAD_TARGET})</span></h2>
                <p>All retained, traded & auction players — not just DB-scraped wins</p>
            </div>
            <div class="section-actions">
                <button class="btn-secondary" onclick="reloadSquadFromApi()">↻ Sync Squad</button>
                <button class="btn-secondary" onclick="resetSquadStorage()">Reset to 25</button>
                <button class="btn-primary" onclick="showTab('bidadvisor')">Bid Advisor →</button>
            </div>
        </div>`;

    html += `
        <div class="section-header section-header--exec" style="margin-top:0;padding-top:0;border:none">
            <div>
                <h2>Playing XI <span class="section-count">(${playing11.length}/11)</span></h2>
                <p>Default matchday lineup — drag order via squad planning</p>
            </div>
        </div>
        <div class="squad-list squad-list--xi">
            <div class="squad-list-header">
                <span class="slh-slot">#</span>
                <span class="slh-identity">Player</span>
                <span class="slh-stats">Stats (last 10)</span>
                <span class="slh-bar">Form</span>
                <span class="slh-price">Price</span>
                <span class="slh-actions"></span>
            </div>`;

    playing11.forEach((p, i) => {
        html += rowHtml(p, i + 1, xi_stats[i], 'PLAYING');
    });
    if (playing11.length === 0) {
        html += `<div class="empty-state" style="padding:32px">No players in Playing XI</div>`;
    }
    html += `</div>`;

    // Bench
    html += `
        <div class="section-header" style="margin-top:28px">
            <h2>Bench <span style="font-weight:400;font-size:14px;color:#94a3b8;">(${bench.length} players)</span></h2>
            <p>Support squad — available for selection</p>
        </div>
        <div class="squad-list squad-list--bench">
            <div class="squad-list-header">
                <span class="slh-slot">#</span>
                <span class="slh-identity">Player</span>
                <span class="slh-stats">Stats (last 10)</span>
                <span class="slh-bar">Form</span>
                <span class="slh-price">Price</span>
                <span class="slh-actions"></span>
            </div>`;

    bench.forEach((p, i) => {
        html += rowHtml(p, i + 12, bench_stats[i], 'BENCH');
    });
    if (bench.length === 0) {
        html += `<div class="empty-state" style="padding:24px">No bench players. Scout targets from the Scout tab.</div>`;
    }
    html += `</div>`;

    if (currentSquad.length === 0) {
        html = `
        <div class="empty-state">
            <p>No players in squad.</p>
            <p class="wr-muted" style="margin:12px 0">Loads the <strong>official IPL 2026 CSK 25-man roster</strong> (Sportstar/Hindu lists + DB prices). Old War Room showed a stale 2024/25 live API roster — Jadeja/Pathirana were released for 2026.</p>
            <div class="link-actions">
                <button class="btn-add" onclick="reloadSquadFromApi()">Reload squad from API</button>
                <button class="btn-link-tab" onclick="showTab('players')">Open Scout →</button>
            </div>
        </div>`;
    } else {
        html += `
        <div class="tab-footer-links">
            <span>Squad data powers Bid Advisor purse & gap analysis.</span>
            <button class="btn-primary-sm" onclick="showTab('bidadvisor')">Open Bid Advisor →</button>
        </div>`;
    }

    if (_activeTab !== 'squad' || tabAtStart !== 'squad') return;
    document.getElementById('contentArea').innerHTML = html;
}

// ─────────────────────────────────────────────────────────────────────
// PLAYER POOL TAB (unchanged from original)
// ─────────────────────────────────────────────────────────────────────

function playerInitials(name) {
    const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
    if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    return (parts[0]?.[0] || '?').toUpperCase();
}

function poolFilterLabel(key) {
    return {
        batters: 'Auction pool · batters',
        bowlers: 'Auction pool · bowlers',
        allrounders: 'Auction pool · all-rounders',
        inform: 'Auction pool · in form',
    }[key] || 'Auction pool';
}

async function renderPlayersTab(searchTerm = '', roleFilter = _poolFilter) {
    _poolFilter = roleFilter || 'batters';
    let url;
    let poolMeta = null;
    if (searchTerm) {
        url = `${API_BASE}/players/search?name=${encodeURIComponent(searchTerm)}&limit=30`;
    } else {
        url = `${API_BASE}/players/auction-pool?filter=${encodeURIComponent(_poolFilter)}&year=${IPL_AUCTION_YEAR}&limit=48`;
    }

    try {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        const players = Array.isArray(payload) ? payload : (payload.players || []);
        poolMeta = Array.isArray(payload) ? null : payload;
        allPlayers = players;

        const isInSquad = name => currentSquad.some(p => p.name === name);
        const filters = [
            ['batters', 'Batters'],
            ['bowlers', 'Bowlers'],
            ['allrounders', 'All-rounders'],
            ['inform', 'In form'],
        ];
        const filterHtml = filters.map(([key, label]) =>
            `<button type="button" class="pool-filter ${key === _poolFilter && !searchTerm ? 'pool-filter--active' : ''}"
                onclick="renderPlayersTab(document.getElementById('playerSearch')?.value || '', '${key}')">${label}</button>`
        ).join('');

        let html = `
            <div class="scout-panel">
                <div class="scout-header">
                    <div>
                        <h2 class="scout-title">Player Scout</h2>
                        <p class="scout-sub">Search or browse IPL ${IPL_AUCTION_YEAR} auction pool · click for FMV, CSK fit & bid range</p>
                    </div>
                    <span class="scout-count">${players.length}${poolMeta?.pool_size ? ` / ${poolMeta.pool_size}` : ''} players</span>
                </div>
                <div class="scout-toolbar">
                    <div class="scout-search-wrap">
                        <input type="text" id="playerSearch" class="search-input scout-search"
                               placeholder="Search by name — e.g. Rahul Chahar, Prashant Veer…"
                               value="${escapeAttr(searchTerm)}">
                    </div>
                    <div class="pool-filters" role="tablist" aria-label="Browse by role">${filterHtml}</div>
                </div>
                ${searchTerm
                    ? `<p class="scout-context">Search results for “${escapeAttr(searchTerm)}”</p>`
                    : `<p class="scout-context">${poolFilterLabel(_poolFilter)} (IPL ${IPL_AUCTION_YEAR} bid history) — not your CSK squad</p>`}
                <div class="player-list" id="playerPoolGrid">`;

        for (const player of players) {
            const inSquad = isInSquad(player.player_name);
            const formVal = player.form_rating;
            const form = formVal != null ? Math.round(formVal) : null;
            const safeData = escapeAttr(player.player_name);
            html += `
                <div class="player-row player-card--clickable" data-player="${safeData}" role="button" tabindex="0"
                     aria-label="Scout ${escapeAttr(player.player_name)}">
                    ${CSKAvatars.markup(player.player_name, 'player-row__avatar')}
                    <div class="player-row__main">
                        <div class="player-row__top">
                            <span class="player-row__name">${player.player_name}</span>
                            ${inSquad
                                ? '<span class="player-row__chip player-row__chip--squad">In squad</span>'
                                : ''}
                            <span class="player-row__chip player-row__chip--form ${getFormClass(form ?? 0)}">${form !== null ? form : '—'} form</span>
                        </div>
                        <div class="player-row__stats">
                            <span>${player.total_runs || 0} runs</span>
                            <span class="player-row__dot">·</span>
                            <span>${player.total_wickets || 0} wkts</span>
                        </div>
                    </div>
                    <div class="player-row__cta">
                        <span class="player-row__fmv-hint">FMV</span>
                        <span class="player-row__chevron" aria-hidden="true">›</span>
                    </div>
                </div>`;
        }

        html += `
                </div>
            </div>`;
        if (players.length === 0) {
            html = `
            <div class="scout-panel">
                <div class="scout-header"><h2 class="scout-title">Player Scout</h2></div>
                <div class="empty-state">No players found — try another name or filter.</div>
            </div>`;
        }

        document.getElementById('contentArea').innerHTML = html;

        document.getElementById('playerSearch')?.addEventListener('keypress', e => {
            if (e.key === 'Enter') renderPlayersTab(e.target.value, _poolFilter);
        });

        const grid = document.getElementById('playerPoolGrid');
        grid?.addEventListener('click', e => {
            const card = e.target.closest('.player-card--clickable');
            if (!card?.dataset.player) return;
            openPlayerPreview(card.dataset.player);
        });
        grid?.addEventListener('keydown', e => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            const card = e.target.closest('.player-card--clickable');
            if (!card?.dataset.player) return;
            e.preventDefault();
            openPlayerPreview(card.dataset.player);
        });

        CSKAvatars.bindAll(grid);

    } catch {
        document.getElementById('contentArea').innerHTML =
            '<div class="empty-state">Could not load players. Run <code>./start.sh</code> from repo root (API :8000 + dashboard :8080) or check the banner above.</div>';
    }
}

function addToSquad(playerName, estimatedValue) {
    if (currentSquad.length >= 25) { alert('Squad limit reached (max 25 players)'); return; }
    if (currentSquad.some(p => p.name === playerName)) { alert(`${playerName} is already in the squad`); return; }
    fetchPlayerStats(playerName).then(stats => {
        currentSquad.push(normalizeSquadPlayer({
            name: playerName,
            role: stats?.role || 'Player',
            price: Math.max(0.5, Math.min(18, estimatedValue || stats?.estimated_value || 0.5)),
            country: stats?.country || 'India',
            retained: false,
        }));
        saveSquad();
        updatePurseDisplay();
        renderPlayersTab(document.getElementById('playerSearch')?.value || '');
    });
}

function removeFromSquad(playerName) {
    currentSquad = currentSquad.filter(p => p.name !== playerName);
    saveSquad();
    updatePurseDisplay();
    if (_activeTab === 'squad') renderSquadTab();
}

// ─────────────────────────────────────────────────────────────────────
// COMPARE TAB — full side-by-side with winner highlighting
// ─────────────────────────────────────────────────────────────────────

async function renderCompareTab() {
    document.getElementById('contentArea').innerHTML = `
        <p class="wr-muted" style="margin-bottom:12px">Compare stats, then open Bid Advisor on either player from the results.</p>
        <div class="search-box">
            <div style="display:grid;grid-template-columns:1fr auto 1fr;gap:12px;align-items:center;">
                <input type="text" id="compareP1" class="search-input" placeholder="Player 1 name...">
                <span class="vs-divider">VS</span>
                <input type="text" id="compareP2" class="search-input" placeholder="Player 2 name...">
            </div>
            <div style="margin-top:14px;text-align:center;">
                <button class="btn-compare-run" onclick="performComparison()">Compare Players</button>
            </div>
        </div>
        <div id="comparisonResult"></div>`;

    // Allow Enter key in either field
    ['compareP1','compareP2'].forEach(id => {
        document.getElementById(id)?.addEventListener('keypress', e => {
            if (e.key === 'Enter') performComparison();
        });
    });
}

async function performComparison() {
    const p1 = document.getElementById('compareP1').value.trim();
    const p2 = document.getElementById('compareP2').value.trim();
    const resultEl = document.getElementById('comparisonResult');

    if (!p1 || !p2) { alert('Please enter both player names'); return; }

    resultEl.innerHTML = `<div class="loading" style="padding:40px">Fetching comparison data…</div>`;

    try {
        const r = await fetch(`${API_BASE}/players/compare?p1=${encodeURIComponent(p1)}&p2=${encodeURIComponent(p2)}`);
        const data = await r.json();
        if (data.detail || data.error) {
            resultEl.innerHTML = `<div class="empty-state">${data.detail || data.error}</div>`;
            return;
        }
        renderComparisonResult(data.player1, data.player2);
    } catch {
        resultEl.innerHTML = `<div class="empty-state">Error comparing players. Check API is running.</div>`;
    }
}

function renderComparisonResult(v1, v2) {
    // Metrics to compare: [label, key1, key2, lowerIsBetter]
    const metrics = [
        { label: 'Form Score',       k1: 'form_score',       k2: 'form_score',       lower: false, unit: '/100', pct: true },
        { label: 'CSK Fit',          k1: 'csk_fit_score',    k2: 'csk_fit_score',    lower: false, unit: '/100', pct: true },
        { label: 'Est. Value',       k1: 'estimated_value',  k2: 'estimated_value',  lower: true,  unit: ' Cr',  fmt: v => `₹${v}` },
        { label: 'Career Runs',      k1: 'career_runs',      k2: 'career_runs',      lower: false, unit: '' },
        { label: 'Career Wickets',   k1: 'career_wickets',   k2: 'career_wickets',   lower: false, unit: '' },
        { label: 'Last 10 SR',       k1: 'last_10_sr',       k2: 'last_10_sr',       lower: false, unit: '' },
        { label: 'Last 10 Economy',  k1: 'last_10_econ',     k2: 'last_10_econ',     lower: true,  unit: '' },
        { label: 'Last 10 Runs',     k1: 'last_10_runs',     k2: 'last_10_runs',     lower: false, unit: '' },
        { label: 'Last 10 Wickets',  k1: 'last_10_wickets',  k2: 'last_10_wickets',  lower: false, unit: '' },
        { label: 'Career SR',        k1: 'career_sr',        k2: 'career_sr',        lower: false, unit: '' },
        { label: 'Career Economy',   k1: 'career_econ',      k2: 'career_econ',      lower: true,  unit: '' },
        { label: 'Matches',          k1: 'matches_played',   k2: 'matches_played',   lower: false, unit: '' },
    ];

    // Count wins per player for headline
    let wins1 = 0, wins2 = 0;
    metrics.forEach(m => {
        const a = parseFloat(v1[m.k1]) || 0;
        const b = parseFloat(v2[m.k2]) || 0;
        if (a === b || (a === 0 && b === 0)) return;
        const p1wins = m.lower ? a < b : a > b;
        if (p1wins) wins1++; else wins2++;
    });

    const winner = wins1 > wins2 ? v1.player_name : wins2 > wins1 ? v2.player_name : null;

    function verdictCls(v) {
        return verdictFromApi(v).cls;
    }

    function metricRows() {
        return metrics.map(m => {
            const a = parseFloat(v1[m.k1]);
            const b = parseFloat(v2[m.k2]);
            const aValid = !isNaN(a) && a > 0;
            const bValid = !isNaN(b) && b > 0;

            let p1win = false, p2win = false;
            if (aValid && bValid && a !== b) {
                p1win = m.lower ? a < b : a > b;
                p2win = !p1win;
            }

            const fmtVal = (v, valid) => {
                if (!valid) return '<span style="color:#cbd5e1">—</span>';
                if (m.fmt) return m.fmt(v);
                return `${v}${m.unit || ''}`;
            };

            // Mini bar for percentage metrics
            const barHtml = (v, valid, win) => {
                if (!m.pct || !valid) return '';
                const pct = Math.min(100, v);
                const cls = pct >= 70 ? 'progress-high' : pct >= 50 ? 'progress-mid' : 'progress-low';
                return `<div class="cmp-mini-bar"><div class="progress-fill ${cls}" style="width:${pct}%"></div></div>`;
            };

            return `
            <div class="cmp-row">
                <div class="cmp-cell cmp-cell--left ${p1win ? 'cmp-winner' : ''}">
                    <span class="cmp-val">${fmtVal(a, aValid)}</span>
                    ${p1win ? '<span class="cmp-win-badge">✓</span>' : ''}
                    ${barHtml(a, aValid, p1win)}
                </div>
                <div class="cmp-label">${m.label}</div>
                <div class="cmp-cell cmp-cell--right ${p2win ? 'cmp-winner' : ''}">
                    ${p2win ? '<span class="cmp-win-badge">✓</span>' : ''}
                    <span class="cmp-val">${fmtVal(b, bValid)}</span>
                    ${barHtml(b, bValid, p2win)}
                </div>
            </div>`;
        }).join('');
    }

    const html = `
    <div class="cmp-container">

        <!-- Headline -->
        <div class="cmp-headline">
            <div class="cmp-head cmp-head--left ${wins1 >= wins2 ? 'cmp-head--active' : ''}">
                <div class="cmp-head-name">${v1.player_name}</div>
                <div class="cmp-head-meta">${v1.role || '—'}</div>
                <div class="cmp-head-price">₹${v1.estimated_value || '—'} Cr</div>
                <span class="verdict-badge ${verdictCls(v1.auction_verdict)}">${v1.auction_verdict || '—'}</span>
                <div class="cmp-wins-badge">${wins1} wins</div>
            </div>

            <div class="cmp-vs-block">
                <div class="cmp-vs-text">VS</div>
                ${winner
                    ? `<div class="cmp-overall-winner">${winner}<br><span>Overall Edge</span></div>`
                    : `<div class="cmp-overall-winner" style="font-size:12px">Even</div>`}
            </div>

            <div class="cmp-head cmp-head--right ${wins2 >= wins1 ? 'cmp-head--active' : ''}">
                <div class="cmp-head-name">${v2.player_name}</div>
                <div class="cmp-head-meta">${v2.role || '—'}</div>
                <div class="cmp-head-price">₹${v2.estimated_value || '—'} Cr</div>
                <span class="verdict-badge ${verdictCls(v2.auction_verdict)}">${v2.auction_verdict || '—'}</span>
                <div class="cmp-wins-badge">${wins2} wins</div>
            </div>
        </div>

        <!-- Metric rows -->
        <div class="cmp-table">
            <div class="cmp-table-header">
                <span>${v1.player_name}</span>
                <span style="text-align:center;color:#94a3b8;font-size:11px;font-weight:500">METRIC</span>
                <span style="text-align:right">${v2.player_name}</span>
            </div>
            ${metricRows()}
        </div>

        <!-- CSK Fit Reasons -->
        <div class="cmp-reasons-grid">
            <div class="cmp-reasons-col">
                <div class="cmp-reasons-title">Why CSK Should Buy ${v1.player_name}</div>
                ${(v1.csk_fit_reasons || []).length
                    ? v1.csk_fit_reasons.map(r => `<div class="cmp-reason-item">${r}</div>`).join('')
                    : '<div class="cmp-reason-item" style="color:#94a3b8">No specific reasons</div>'}
            </div>
            <div class="cmp-reasons-col">
                <div class="cmp-reasons-title">Why CSK Should Buy ${v2.player_name}</div>
                ${(v2.csk_fit_reasons || []).length
                    ? v2.csk_fit_reasons.map(r => `<div class="cmp-reason-item">${r}</div>`).join('')
                    : '<div class="cmp-reason-item" style="color:#94a3b8">No specific reasons</div>'}
            </div>
        </div>

        <!-- Add to Squad buttons -->
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:0 24px 12px;">
            <button class="btn-add" style="width:100%;padding:10px" onclick="addToSquad('${v1.player_name.replace(/'/g, "\\'")}', ${v1.estimated_value || 1})">
                + Add ${v1.player_name} to Squad
            </button>
            <button class="btn-add" style="width:100%;padding:10px" onclick="addToSquad('${v2.player_name.replace(/'/g, "\\'")}', ${v2.estimated_value || 1})">
                + Add ${v2.player_name} to Squad
            </button>
        </div>
        <div class="tab-footer-links" style="padding:0 24px 24px;">
            <button class="btn-link-tab" onclick="openBidAdvisor('${v1.player_name.replace(/'/g, "\\'")}')">Bid Advisor: ${v1.player_name} →</button>
            <button class="btn-link-tab" onclick="openBidAdvisor('${v2.player_name.replace(/'/g, "\\'")}')">Bid Advisor: ${v2.player_name} →</button>
        </div>
    </div>`;

    document.getElementById('comparisonResult').innerHTML = html;
}

// ─────────────────────────────────────────────────────────────────────
// VALUATION TAB (unchanged from original)
// ─────────────────────────────────────────────────────────────────────

async function renderValuationTab() {
    document.getElementById('contentArea').innerHTML = `
        <div class="search-box">
            <div style="display:flex;gap:12px;">
                <input type="text" id="valuationSearch" class="search-input" placeholder="Enter player name..." value="${_valuationPrefill.replace(/"/g, '&quot;')}">
                <button class="btn-add" onclick="getValuation()">Get Valuation</button>
            </div>
        </div>
        <p class="wr-muted" style="margin-bottom:12px">Valuation = fair price & fit. For bid/walk-away limits and rivals, use <button class="btn-link-inline" onclick="showTab('bidadvisor')">Bid Advisor</button>.</p>
        <div id="valuationResult"></div>`;

    document.getElementById('valuationSearch')?.addEventListener('keypress', e => {
        if (e.key === 'Enter') getValuation();
    });
    if (_valuationPrefill) {
        _valuationPrefill = '';
        getValuation();
    }
}

async function getValuation() {
    const playerName = document.getElementById('valuationSearch').value.trim();
    if (!playerName) {
        alert('Please enter a player name');
        return;
    }
    
    try {
        const response = await fetch(`${API_BASE}/players/valuation/${encodeURIComponent(playerName)}`);
        const valuation = await response.json();
        
        if (valuation.detail) {
            document.getElementById('valuationResult').innerHTML = `<div class="empty-state">${valuation.detail}</div>`;
            return;
        }
        
        const { cls: verdictClass, text: verdictText } = verdictFromApi(valuation.auction_verdict);
        const formScore = valuation.form_score || 0;
        const histLine = valuation.historical_auction_price
            ? `<div class="hist-price">Last IPL auction: <strong>₹${valuation.historical_auction_price} Cr</strong></div>`
            : '';
        const progressClass = formScore >= 70 ? 'progress-high' : (formScore >= 50 ? 'progress-mid' : 'progress-low');
        const injuryText = valuation.injury_risk === 'Low' ? 'Low Risk' : (valuation.injury_risk === 'High' ? 'High Risk' : 'Medium Risk');
        
        const html = `
            <div class="valuation-card">
                <div class="valuation-header">
                    <div>
                        <div class="valuation-name">${valuation.player_name}</div>
                        <div style="font-size: 13px; color: #64748b; margin-top: 4px;">
                            ${valuation.role}${valuation.role_detail ? ' · ' + valuation.role_detail : ''} | Age: ${valuation.age}${valuation.age_upside ? ' ↑' : ''} | ${injuryText}
                            ${valuation.confidence != null ? ` | Confidence: ${valuation.confidence}%` : ''}
                            ${valuation.volatility ? ` | ${valuation.volatility} volatility` : ''}
                        </div>
                    </div>
                    <div class="valuation-price">₹${valuation.estimated_value} Cr</div>
                </div>
                
                ${renderConfidenceWarning(valuation)}

                <div class="valuation-verdict-row">
                    <span class="verdict-badge ${verdictClass}">${verdictText}</span>
                    <span class="valuation-range-meta">
                        Range: ₹${valuation.floor_price} – ₹${valuation.ceiling_price} Cr
                        ${valuation.experience_factor != null ? ` · Experience: ${Math.round(valuation.experience_factor * 100)}%` : ''}
                        ${valuation.scarcity_bonus ? ` · Scarcity ×${valuation.scarcity_bonus}` : ''}
                    </span>
                </div>
                ${histLine}
                ${renderMarketBand(valuation.market_value, valuation.estimated_value)}

                <div class="valuation-metrics">
                    <div class="metric-card">
                        <div class="metric-value">${valuation.form_score}</div>
                        <div class="metric-label">Form Score</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${valuation.csk_fit_score}</div>
                        <div class="metric-label">CSK Fit</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${valuation.career_runs || 0}</div>
                        <div class="metric-label">Career Runs</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${valuation.career_wickets || 0}</div>
                        <div class="metric-label">Career Wickets</div>
                    </div>
                </div>
                
                <div style="margin: 20px 0; padding: 16px; background: #f8fafc; border-radius: 12px;">
                    <div style="font-size: 13px; font-weight: 600; margin-bottom: 8px;">Why CSK should consider:</div>
                    <ul style="margin: 0; padding-left: 20px;">
                        ${(valuation.csk_fit_reasons || []).slice(0, 4).map(r => `<li style="font-size: 12px; color: #475569; margin-bottom: 4px;">${r}</li>`).join('')}
                    </ul>
                </div>
                
                <div style="margin: 20px 0; padding: 16px; background: #f1f5f9; border-radius: 12px;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 12px;">
                        <span style="font-size: 13px; font-weight: 500;">Last 10 Matches</span>
                        <span style="font-size: 13px;">Runs: ${valuation.last_10_runs || 0} | Wickets: ${valuation.last_10_wickets || 0}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between; font-size: 12px; color: #64748b;">
                        <span>SR: ${valuation.last_10_sr || '-'}</span>
                        <span>Economy: ${valuation.last_10_econ || '-'}</span>
                        <span>Career SR: ${valuation.career_sr || '-'}</span>
                    </div>
                    <div class="progress-bar" style="margin-top: 12px;">
                        <div class="progress-fill ${progressClass}" style="width: ${Math.min(100, formScore)}%"></div>
                    </div>
                </div>
                
                <div style="display: flex; justify-content: flex-end; gap: 10px; flex-wrap: wrap;">
                    <button class="btn-link-tab" onclick="openBidAdvisor('${valuation.player_name.replace(/'/g, "\\'")}')">Bid Advisor →</button>
                    <button class="btn-add" onclick="addToSquad('${valuation.player_name.replace(/'/g, "\\'")}', ${valuation.estimated_value})">Add to Squad</button>
                </div>
            </div>
        `;
        
        document.getElementById('valuationResult').innerHTML = html;
        
    } catch (error) {
        document.getElementById('valuationResult').innerHTML = '<div class="empty-state">Error fetching valuation. Make sure API is running.</div>';
    }
}

// ─────────────────────────────────────────────────────────────────────
// BID ADVISOR TAB — squad + valuation + bid-history (not valuation alone)
// ─────────────────────────────────────────────────────────────────────

function squadRoleCounts() {
    const counts = { Batter: 0, Bowler: 0, 'All Rounder': 0, 'Wicket Keeper': 0 };
    currentSquad.forEach(p => {
        const r = p.role || 'Batter';
        if (counts[r] !== undefined) counts[r]++;
    });
    return counts;
}

function remainingBudget() {
    return IPL_PURSE_CR - currentSquad.reduce((s, p) => s + (p.price || 0), 0);
}

async function fetchBidAdvisorDecision(playerName, currentBid = 0, basePrice = 2) {
    const squad = currentSquad.map(p => ({
        name: p.name,
        role: p.role || 'Player',
        price: p.price || 0,
        country: p.country || 'India',
    }));
    const r = await fetch(`${API_BASE}/war-room/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            player: playerName,
            budget: IPL_PURSE_CR,
            current_bid: currentBid,
            base_price: basePrice,
            auction_year: 2026,
            squad,
        }),
    });
    if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || 'Bid Advisor API error');
    }
    return r.json();
}

function bidAdvisorVerdictClass(shouldBid) {
    if (shouldBid === 'YES') return 'wr-yes';
    if (shouldBid === 'MAYBE') return 'wr-maybe';
    return 'wr-no';
}

function renderBidAdvisorDecision(d) {
    const q = d.quick_decision || {};
    const bi = d.bidding_intelligence || {};
    const sc = d.squad_context || {};
    const rc = sc.role_counts || {};
    const win = d.budget_impact?.if_win || {};
    const lose = d.budget_impact?.if_lose || {};
    const live = d.live_bid || {};
    const pa = d.player_analysis || {};

    const compsHtml = (bi.similar_players || []).map(c =>
        `<div class="wr-comp-row">
            <span>${c.player}</span>
            <span>${c.num_bids} bids</span>
            <span>₹${c.final_price_cr} Cr</span>
            <span class="wr-comp-result">${c.csk_result !== '—' ? 'CSK ' + c.csk_result : c.winner_team}</span>
        </div>`
    ).join('') || '<div class="wr-muted">No similar bid comps in 2026 data</div>';

    const rivalsHtml = (bi.likely_competitors || []).map(c =>
        `<div class="wr-rival-row">
            <strong>${c.team}</strong>
            <span>${c.threat_level}</span>
            <span>${c.targets_in_role} targets · avg ${c.avg_bids} bids · ₹${c.avg_price_cr} Cr</span>
        </div>`
    ).join('') || '<div class="wr-muted">No competitor data</div>';

    const gaps = (sc.gaps || []).slice(0, 4).map(g =>
        `<span class="wr-gap-chip ${g.priority === 'Critical' ? 'wr-gap-critical' : ''}">${g.role}: ${g.have}/${g.ideal}</span>`
    ).join('');

    return `
    <div class="wr-layout">
        <!-- SECTION 1: Quick Decision -->
        <div class="wr-quick ${bidAdvisorVerdictClass(q.should_bid)}">
            <div class="wr-quick-top">
                <div class="wr-squad-strip">
                    CSK STATUS · Budget ₹${sc.remaining_budget_cr ?? remainingBudget().toFixed(1)} Cr ·
                    Squad ${sc.squad_size ?? currentSquad.length}/25 ·
                    AR ${rc['All Rounder'] ?? 0}/${5} · WK ${rc['Wicket Keeper'] ?? 0}/2
                </div>
                <div class="wr-gaps">${gaps}</div>
            </div>
            <div class="wr-quick-main">
                <div class="wr-player-title">${d.player_name} · ${d.role} · ${d.country || '—'}</div>
                <div class="wr-should-bid">SHOULD WE BID? <span>${q.should_bid || '—'}</span></div>
                <div class="wr-price-row">
                    <div><label>Entry</label><strong>₹${q.entry_bid_cr} Cr</strong></div>
                    <div><label>FMV</label><strong>₹${q.fair_market_value_cr} Cr</strong></div>
                    <div><label>Walk-away</label><strong>₹${q.walk_away_cr} Cr</strong></div>
                    <div><label>Strategy</label><strong>${q.strategy}</strong></div>
                    <div><label>Confidence</label><strong>${q.confidence_pct}%</strong></div>
                </div>
                <p class="wr-one-liner">${q.one_liner || ''}</p>
                <button class="btn-link-sm" onclick="openValuation('${(d.player_name || '').replace(/'/g, "\\'")}')">Full Valuation →</button>
            </div>
        </div>

        <div class="wr-columns">
            <!-- Left: Player + Market -->
            <div class="wr-panel">
                <h3>Player & CSK Fit</h3>
                <div class="wr-kv"><span>Form</span><strong>${pa.form_score ?? '—'}%</strong></div>
                <div class="wr-kv"><span>CSK Fit</span><strong>${pa.csk_fit_score ?? '—'}%</strong></div>
                <div class="wr-kv"><span>Verdict</span><strong>${pa.auction_verdict || '—'}</strong></div>
                <div class="wr-kv"><span>Base price</span><strong>₹${pa.base_price_cr ?? 2} Cr</strong></div>
                <div class="wr-kv"><span>CSK band win rate</span><strong>${bi.csk_band_win_rate_pct ?? '—'}%</strong></div>
                <ul class="wr-reasons">
                    ${(d.reasons || []).map(r => `<li>${r}</li>`).join('')}
                </ul>
            </div>

            <!-- Right: Bid Intelligence -->
            <div class="wr-panel">
                <h3>Bidding Intelligence</h3>
                <div class="wr-kv"><span>Expected bids</span><strong>${bi.expected_bids_range || '—'}</strong></div>
                <div class="wr-kv"><span>Bid war probability</span><strong>${bi.bid_war_probability_pct ?? '—'}%</strong></div>
                <div class="wr-kv"><span>Expected final</span><strong>₹${bi.expected_final_price_cr || '—'} Cr</strong></div>
                <div class="wr-subhead">Similar players (2026 bid sheet)</div>
                ${compsHtml}
                <div class="wr-subhead">Likely competitors</div>
                ${rivalsHtml}
            </div>
        </div>

        <!-- Live bid simulator -->
        <div class="wr-panel wr-live">
            <h3>Live Bid Simulator</h3>
            <p class="wr-muted">Enter current table bid (Cr) — updates recommendation in real time. Wire to live feed later.</p>
            <div class="wr-live-controls">
                <input type="number" id="baCurrentBid" class="search-input" step="0.25" min="0" placeholder="Current bid e.g. 5.5" value="">
                <button class="btn-add" onclick="updateBidAdvisorLive()">Update Live Call</button>
            </div>
            <div class="wr-live-call wr-live-${(live.status || 'wait').toLowerCase().replace(' ', '-')}">
                <strong>${live.status || 'WAIT'}</strong> — ${live.message || ''}
                ${live.recommended_bid_cr ? `<div>Recommended next bid: <strong>₹${live.recommended_bid_cr} Cr</strong></div>` : ''}
            </div>
        </div>

        <!-- Budget impact -->
        <div class="wr-budget-grid">
            <div class="wr-panel wr-budget-win">
                <h3>If CSK Wins</h3>
                <div class="wr-kv"><span>Squad</span><strong>${sc.squad_size ?? currentSquad.length} → ${win.squad_size_after ?? '?'}</strong></div>
                <div class="wr-kv"><span>Role fill</span><strong>${win.role_after || '—'}</strong></div>
                <div class="wr-kv"><span>Budget left</span><strong>₹${win.budget_after_cr ?? '—'} Cr</strong></div>
                <div class="wr-kv"><span>Status</span><strong>${win.on_track ? 'ON TRACK ✓' : 'TIGHT'}</strong></div>
            </div>
            <div class="wr-panel wr-budget-lose">
                <h3>If CSK Loses</h3>
                <div class="wr-kv"><span>Squad</span><strong>${lose.squad_size ?? currentSquad.length}/25</strong></div>
                <div class="wr-kv"><span>Still need (role)</span><strong>${lose.role_still_need ?? '—'}</strong></div>
                <div class="wr-kv"><span>Budget</span><strong>₹${lose.budget_unchanged_cr ?? '—'} Cr</strong></div>
                <div class="wr-kv"><span>Next</span><strong>${lose.next_action || '—'}</strong></div>
            </div>
        </div>
    </div>`;
}

async function runBidAdvisorAnalysis() {
    const player = document.getElementById('baPlayerSearch')?.value.trim();
    const basePrice = parseFloat(document.getElementById('baBasePrice')?.value) || 2;
    const resultEl = document.getElementById('bidAdvisorResult');
    if (!player) { alert('Enter player name'); return; }

    resultEl.innerHTML = '<div class="loading">Building bid recommendation…</div>';
    _bidAdvisorPlayer = player;

    try {
        const currentBid = parseFloat(document.getElementById('baCurrentBid')?.value) || 0;
        const data = await fetchBidAdvisorDecision(player, currentBid, basePrice);
        resultEl.innerHTML = renderBidAdvisorDecision(data);
        if (document.getElementById('baCurrentBid')) {
            document.getElementById('baCurrentBid').addEventListener('keypress', e => {
                if (e.key === 'Enter') updateBidAdvisorLive();
            });
        }
    } catch (e) {
        resultEl.innerHTML = `<div class="empty-state">${e.message}. Is API running on :8000?</div>`;
    }
}

async function updateBidAdvisorLive() {
    if (!_bidAdvisorPlayer) { alert('Run analysis for a player first'); return; }
    const currentBid = parseFloat(document.getElementById('baCurrentBid')?.value) || 0;
    const basePrice = parseFloat(document.getElementById('baBasePrice')?.value) || 2;
    const resultEl = document.getElementById('bidAdvisorResult');
    try {
        const data = await fetchBidAdvisorDecision(_bidAdvisorPlayer, currentBid, basePrice);
        resultEl.innerHTML = renderBidAdvisorDecision(data);
        const bidEl = document.getElementById('baCurrentBid');
        const searchEl = document.getElementById('baPlayerSearch');
        if (bidEl) bidEl.value = currentBid || '';
        if (searchEl) searchEl.value = _bidAdvisorPlayer;
    } catch (e) {
        alert(e.message);
    }
}

async function renderBidAdvisorTab() {
    let strategyHtml = '';
    try {
        const sr = await fetch(`${API_BASE}/war-room/strategy`);
        const st = await sr.json();
        if (st.available) {
            strategyHtml = `
            <div class="wr-strategy-banner">
                <strong>CSK Auction DNA (${st.from_year}–${st.to_year})</strong>
                ${st.archetype} · ${st.career_win_rate_pct}% win rate ·
                Value band ${st.value_band_win_rate_pct}% · Premium ${st.premium_win_rate_pct}% ·
                Top rival: ${(st.career_rivals && st.career_rivals[0]?.rival) || '—'}
            </div>`;
        }
    } catch {}

    const rc = squadRoleCounts();
    const squadWarn = currentSquad.length === 0
        ? `<div class="wr-squad-warn">No squad loaded — <button class="btn-link-inline" onclick="reloadSquadFromApi()">Reload from API</button> or <button class="btn-link-inline" onclick="showTab('players')">add players</button> for accurate gap/budget advice.</div>`
        : '';

    document.getElementById('contentArea').innerHTML = `
        ${strategyHtml}
        <div class="ba-intro">
            <strong>Bid Advisor</strong> ≠ Valuation alone. It merges fair price, <em>your squad gaps</em>, purse left, and <em>2018–2026 bid-war history</em> into a YES/NO/Maybe with walk-away price.
            <button class="btn-link-inline" onclick="showTab('valuation')">Valuation tab</button> = player worth · Bid Advisor = should CSK bid right now.
        </div>
        ${squadWarn}
        <div class="wr-search-bar">
            <input type="text" id="baPlayerSearch" class="search-input"
                   placeholder="Player on block — e.g. Rahul Chahar, Jason Holder…" value="${_bidAdvisorPlayer.replace(/"/g, '&quot;')}">
            <input type="number" id="baBasePrice" class="search-input wr-base-input"
                   step="0.25" min="0.2" placeholder="Base ₹ Cr" value="2">
            <button class="btn-compare-run" onclick="runBidAdvisorAnalysis()">Get Bid Advice</button>
        </div>
        <div class="wr-context-line">
            Squad ${currentSquad.length}/25 · ₹${remainingBudget().toFixed(1)} Cr left ·
            AR ${rc['All Rounder']}/5 · Bowler ${rc.Bowler}/6 ·
            <button class="btn-link-inline" onclick="showTab('squad')">Edit squad</button>
        </div>
        <div id="bidAdvisorResult">
            <div class="empty-state wr-empty-hint">
                Enter a player and hit <strong>Get Bid Advice</strong>.
            </div>
        </div>`;

    document.getElementById('baPlayerSearch')?.addEventListener('keypress', e => {
        if (e.key === 'Enter') runBidAdvisorAnalysis();
    });

    if (_bidAdvisorPlayer) {
        runBidAdvisorAnalysis();
    }
}

// ─────────────────────────────────────────────────────────────────────
// TAB ROUTER + EVENT SETUP
// ─────────────────────────────────────────────────────────────────────

async function showTab(tabName) {
    _initialRouteDone = true;
    if (_activeTab === 'arena' && tabName !== 'arena') {
        Arena.unmount();
        updatePurseDisplay();
    }
    _activeTab = tabName;
    document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
    document.querySelector(`[data-tab="${tabName}"]`)?.classList.add('active');
    updateTabHint(tabName);
    switch (tabName) {
        case 'bidadvisor': await renderBidAdvisorTab(); break;
        case 'squad':      await renderSquadTab();      break;
        case 'players':    await renderPlayersTab('');  break;
        case 'arena':      await Arena.mount(document.getElementById('contentArea')); break;
        case 'compare':    await renderCompareTab();    break;
        case 'valuation':  await renderValuationTab();  break;
    }
}

function applyArenaSquad(arenaPlayers) {
    currentSquad = arenaPlayers.map(p => normalizeSquadPlayer({
        name: p.player_name,
        role: p.role || p.pool_role || 'Player',
        price: p.price || p.bubble_price_cr || 0,
        country: p.country || 'India',
        overseas: p.overseas ?? computeOverseas({ country: p.country }),
        price_verified: false,
        price_estimated: true,
        price_source: 'arena_sim',
        price_note: 'Arena simulation — confirm against official IPL prices',
        acquisition: 'auction',
        retained: false,
    }));
    saveSquad();
    updatePurseDisplay();
}

window.CSKDashboard = {
    API_BASE,
    IPL_PURSE_CR,
    IPL_AUCTION_YEAR,
    openPlayerPreview,
    openBidAdvisor,
    applyArenaSquad,
};

function setupEventListeners() {
    document.querySelectorAll('.nav-tab').forEach(btn => {
        btn.addEventListener('click', () => showTab(btn.dataset.tab));
    });
}
