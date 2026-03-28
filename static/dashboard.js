if (typeof window.isManualSession === "undefined") window.isManualSession = false;
// ============================================
// METAR Auto Dashboard v2.0 — BMKG Aviation
// ============================================

// =======================
// STATE
// =======================
let tempChart = null;
let pressureChart = null;
let windChart = null;
let lastMetarRaw = null;
let lastMetarStatus = 'normal';
let lastVisibility = null;
let lastHasTS = false;

// =======================
// ALARM STATE TRACKING (Anti-spam)
// =======================
// Track kondisi alarm yang sudah pernah dipicu untuk mencegah spam setiap refresh
// PERSISTENCE: Data dimuat dari localStorage untuk mencegah alarm bunyi lagi saat pindah halaman
let alarmState = JSON.parse(localStorage.getItem('alarmState')) || {
    lowVisTriggered: false,      // Sudah pernah trigger alarm low visibility?
    thunderstormTriggered: false, // Sudah pernah trigger alarm TS?
    lastMetarHash: null,         // Hash METAR terakhir yang diproses
    lastUpdateTime: 0,           // Timestamp terakhir update (ms)
    lastProcessedServerTime: null // ISO timestamp dari server (data.last_update)
};

// Helper: Simpan state alarm ke localStorage
function saveAlarmState() {
    localStorage.setItem('alarmState', JSON.stringify(alarmState));
}

// Helper: Buat simple hash dari string METAR untuk comparison
function hashMetar(metar) {
    if (!metar) return null;
    // Normalisasi: uppercase, trim, remove =
    return metar.toUpperCase().replace(/=/g, '').trim();
}
// Load soundEnabled from localStorage, default false
let soundEnabled = localStorage.getItem('soundEnabled') === 'true';
// Load theme from localStorage, default 'light'
let currentTheme = localStorage.getItem('theme') || 'light';
let currentRunwayHeading = 100;  // Default RWY 10
let currentWindDir = null;
let currentWindSpeed = null;
let currentWindGust = null;
// Persistent auto-fetch state (persists across page reloads & server restarts)
let autoFetchEnabled = localStorage.getItem('autoFetchEnabled') === null ? true : localStorage.getItem('autoFetchEnabled') === 'true';

// WebSocket removed for Vercel/Polling compatibility
// const socket = io(window.location.origin);

// =======================
// POLLING SYSTEM
// =======================
let pollingInterval = null;
let isPolling = false;

// =======================
// CONNECTION STATUS HELPER
// =======================
function updateConnectionIndicator(isOnline) {
    const dot = document.getElementById('connectionDot');
    const text = document.getElementById('connectionText');
    
    if (dot) {
        if (isOnline) {
            dot.classList.add('online');
        } else {
            dot.classList.remove('online');
        }
    }
    
    if (text) {
        text.textContent = isOnline ? 'LIVE' : 'OFFLINE';
    }
}

// Polling replaces Socket Events
async function pollLatestData() {
    if (isPolling) return;
    isPolling = true;
    
    try {
        const response = await fetch('/api/latest-data');
        if (!response.ok) throw new Error('Polling failed');
        const data = await response.json();
        
        console.log('[POLL] Data received:', data);
        
        // Cek apakah data benar-benar berbeda dari sebelumnya 
        // 1. Hash METAR berubah (Weather changed)
        // 2. Server timestamp berubah (New report, even if weather same)
        // 3. UI belum pernah diupdate
        const currentHash = hashMetar(data.raw);
        const isDataChanged = currentHash !== alarmState.lastMetarHash;
        const isTimeChanged = data.last_update && data.last_update !== alarmState.lastProcessedServerTime;
        const isUIEmpty = lastMetarRaw === null;
        
        if (data.raw) {
            if (isDataChanged || isTimeChanged || isUIEmpty) {
                console.log(isUIEmpty ? '[POLL] Initial UI population...' : '[POLL] New data/time detected, processing update...');
                handleMetarUpdate(data);
            } else {
                console.log('[POLL] Data unchanged and UI already populated, skipping update');
                // Tetap update connection indicator
                updateConnectionIndicator(true);
                // Update status panel saja
                if (data.auto_fetch !== undefined) {
                    updateStatusPanel(data);
                }
            }
        } else if (data.auto_fetch !== undefined) {
            // No raw data, just update status
            updateStatusPanel(data);
            updateConnectionIndicator(true);
        }
    } catch (error) {
        console.error('[POLL] Error:', error);
        updateConnectionIndicator(false);
    } finally {
        isPolling = false;
    }
}

// =======================
// KEEP-ALIVE PING
// =======================
// Start polling every 12 seconds (Faster Real-time Sync)
if (typeof pollLatestData === 'function') {
    setInterval(pollLatestData, 12000);
}

// =======================
// CLOCKS (UTC & WIB)
// =======================
function updateClocks() {
    const utcEl = document.getElementById('utcClock');
    const wibEl = document.getElementById('wibClock');
    const now = new Date();

    if (utcEl) {
        const h = now.getUTCHours().toString().padStart(2, '0');
        const m = now.getUTCMinutes().toString().padStart(2, '0');
        const s = now.getUTCSeconds().toString().padStart(2, '0');
        utcEl.innerHTML = `${h}:${m}:${s} <small>UTC</small>`;
    }

    if (wibEl) {
        // WIB is UTC+7
        const wibOffset = 7 * 60 * 60 * 1000;
        const wibDate = new Date(now.getTime() + wibOffset);
        const h = wibDate.getUTCHours().toString().padStart(2, '0');
        const m = wibDate.getUTCMinutes().toString().padStart(2, '0');
        const s = wibDate.getUTCSeconds().toString().padStart(2, '0');
        wibEl.innerHTML = `${h}:${m}:${s} <small>WIB</small>`;
    }
}
setInterval(updateClocks, 1000);
updateClocks(); // Run immediately on load

// =======================
// SIDEBAR TOGGLE - UPDATED FOR STICKY LAYOUT
// =======================
function initSidebar() {
    const toggle = document.getElementById('sidebarToggle');
    const sidebar = document.getElementById('appSidebar');
    const overlay = document.getElementById('sidebarOverlay');
    const layout = document.getElementById('appLayout');
    
    if (!toggle || !sidebar) return;

    // Check saved state
    const isMobile = window.innerWidth <= 768;

    toggle.addEventListener('click', () => {
        if (window.innerWidth <= 768) {
            // Mobile: toggle sidebar dengan overlay
            sidebar.classList.toggle('open');
            if (overlay) overlay.classList.toggle('active');
        } else {
            // Desktop: toggle collapse
            layout.classList.toggle('sidebar-collapsed');
            localStorage.setItem('sidebarCollapsed', layout.classList.contains('sidebar-collapsed'));
        }
    });

    // Close sidebar saat resize ke desktop
    window.addEventListener('resize', () => {
        if (window.innerWidth > 768) {
            sidebar.classList.remove('open');
            if (overlay) overlay.classList.remove('active');
        }
    });

    // Auto-close on link click (mobile)
    document.querySelectorAll('.sidebar-link').forEach(link => {
        link.addEventListener('click', () => {
            if (window.innerWidth <= 768) {
                closeSidebar();
            }
        });
    });
}

function closeSidebar() {
    const sidebar = document.getElementById('appSidebar');
    const overlay = document.getElementById('sidebarOverlay');
    if (sidebar) sidebar.classList.remove('open');
    if (overlay) overlay.classList.remove('active');
}

// Global expose
window.closeSidebar = closeSidebar;

// =======================
// THEME MANAGEMENT (Dark/Light)
// =======================
function applyTheme(theme) {
    const html = document.documentElement;
    const btnHeader = document.getElementById('themeToggle');
    const btnSidebar = document.getElementById('themeToggleSidebar');
    const iconSidebar = document.getElementById('themeIconSidebar');
    const labelSidebar = document.getElementById('themeLabelSidebar');
    
    html.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
    
    if (btnHeader) {
        btnHeader.textContent = theme === 'light' ? '🌙' : '☀️';
    }

    if (btnSidebar) {
        if (iconSidebar) iconSidebar.textContent = theme === 'light' ? '🌙' : '☀️';
        if (labelSidebar) labelSidebar.textContent = theme === 'light' ? 'Dark Mode' : 'Light Mode';
    }

    // Update charts if they exist
    updateChartColors();
}

function toggleTheme() {
    currentTheme = currentTheme === 'light' ? 'dark' : 'light';
    applyTheme(currentTheme);
}

function updateChartColors() {
    const isDark = currentTheme === 'dark';
    const gridColor = isDark ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.06)';
    const tickColor = isDark ? '#94A3B8' : '#64748B';

    // Update charts if they exist and library is loaded
    if (typeof Chart !== 'undefined') {
        [tempChart, pressureChart, windChart].forEach(chart => {
            if (chart) {
            if (chart.options && chart.options.scales) {
                if (chart.options.scales.x) {
                    chart.options.scales.x.grid.color = gridColor;
                    chart.options.scales.x.ticks.color = tickColor;
                }
                if (chart.options.scales.y) {
                    chart.options.scales.y.grid.color = gridColor;
                    chart.options.scales.y.ticks.color = tickColor;
                }
            }
            
            // Adjust legend color
            if (chart.options.plugins && chart.options.plugins.legend && chart.options.plugins.legend.labels) {
                chart.options.plugins.legend.labels.color = isDark ? '#F1F5F9' : '#475569';
            }
            
            chart.update();
        }
    });
    }

    // Update Wind widgets if Plotly is used
    if (typeof loadWindCompass === 'function') {
         loadWindCompass();
    }
    if (typeof loadWindRose === 'function') {
         loadWindRose();
    }
}
window.toggleTheme = toggleTheme;

// =======================
// TOAST NOTIFICATIONS
// =======================
function showToast(title, body, type = 'success', duration = 5000) {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;

    const icon = type === 'danger' ? '🔴' : type === 'warning' ? '🟡' : '🟢';

    toast.innerHTML = `
        <span>${icon}</span>
        <div>
            <div class="toast-title">${title}</div>
            <div class="toast-body">${body}</div>
        </div>
        <button class="toast-close" onclick="this.parentElement.remove()">✕</button>
    `;

    container.appendChild(toast);

    // Play notify sound
    if (type !== 'danger') playNotify();

    // Auto dismiss
    setTimeout(() => {
        toast.classList.add('toast-exit');
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

// =======================
// METAR SYNTAX HIGHLIGHTING
// =======================
function highlightMetar(raw) {
    if (!raw) return '';

    const parts = raw.replace('=', '').split(/\s+/);
    const highlighted = parts.map(part => {
        // Station code (4 letter ICAO)
        if (/^[A-Z]{4}$/.test(part) && parts.indexOf(part) === 0) {
            return `<span class="metar-station">${part}</span>`;
        }
        // Time (ddhhmmZ)
        if (/^\d{6}Z$/.test(part)) {
            return `<span class="metar-time">${part}</span>`;
        }
        // Wind
        if (/KT$/.test(part)) {
            return `<span class="metar-wind">${part}</span>`;
        }
        // Weather phenomena
        if (/^(\+|-|VC)?(TS|RA|SN|SH|FG|BR|HZ|DZ|GR|GS|SQ|FC|SA|DU|VA|FU|PO|SS|DS)/.test(part)) {
            return `<span class="metar-weather">${part}</span>`;
        }
        // Clouds
        if (/^(FEW|SCT|BKN|OVC|NSC|NCD|CLR|SKC)/.test(part)) {
            return `<span class="metar-cloud">${part}</span>`;
        }
        // Temperature / Dewpoint
        if (/^M?\d{2}\/M?\d{2}$/.test(part)) {
            return `<span class="metar-temp">${part}</span>`;
        }
        // Pressure QNH
        if (/^Q\d{4}$/.test(part)) {
            return `<span class="metar-pressure">${part}</span>`;
        }
        // Trend
        if (/^(NOSIG|TEMPO|BECMG)$/.test(part)) {
            return `<span class="metar-trend">${part}</span>`;
        }
        return part;
    });

    return highlighted.join(' ');
}

/**
 * 🔥 METAR Validator Feature
 * Calls /api/validate to check the METAR string against 10 group rules
 */
async function runMetarValidation(raw) {
    const displayEl = document.getElementById('metarValidation');
    if (!displayEl || !raw) return;

    try {
        const response = await fetch('/api/validate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ metar: raw })
        });
        const data = await response.json();
        const results = data.results || [];

        const isValid = results.length === 1 && results[0].startsWith('✅');
        const validationClass = isValid ? 'validation-success' : 'validation-error';

        let html = `
            <div class="validation-card ${validationClass}">
                <div style="font-weight: 700; font-size: 0.9rem; margin-bottom: 6px; display: flex; align-items: center; gap: 6px;">
                    <span>📋</span> Validation Results:
                </div>
                <ul style="list-style: none; padding: 0; margin: 0; font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; line-height: 1.5;">
        `;

        results.forEach(err => {
            html += `<li style="margin-bottom: 2px;">${err}</li>`;
        });

        html += '</ul></div>';
        displayEl.innerHTML = html;
    } catch (err) {
        console.error('Validation API error:', err);
    }
}


// =======================
// DECODED PARAMETERS PANEL
// =======================
function updateDecodedPanel(raw) {
    if (!raw) return;

    // Extract temperature/dewpoint
    const tempMatch = raw.match(/\b(M?\d{2})\/(M?\d{2})\b/);
    if (tempMatch) {
        const temp = tempMatch[1].replace('M', '-');
        const dew = tempMatch[2].replace('M', '-');
        setParam('paramTemp', temp);
        setParam('paramDewpoint', dew);
    }

    // Extract wind
    const windMatch = raw.match(/\b(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT\b/);
    if (windMatch) {
        currentWindDir = windMatch[1] === 'VRB' ? 'VRB' : parseInt(windMatch[1]);
        currentWindSpeed = parseInt(windMatch[2]);
        currentWindGust = windMatch[4] ? parseInt(windMatch[4]) : null;

        const windDisplay = currentWindDir === 'VRB' ? 'VRB' : `${currentWindDir}°`;
        setParam('paramWind', `${windDisplay}/${currentWindSpeed}kt`);

        const gustText = currentWindGust ? ` G${currentWindGust}kt` : '';
        const detailEl = document.getElementById('paramWindDetail');
        if (detailEl) detailEl.textContent = gustText;

        updateCrosswind();
    }

    // Extract visibility
    const visParts = raw.replace('=', '').split(/\s+/);
    for (const p of visParts) {
        if (/^\d{4}$/.test(p) && !p.endsWith('Z')) {
            const vis = parseInt(p);
            setParam('paramVis', vis >= 9999 ? '10+' : (vis >= 1000 ? (vis / 1000).toFixed(vis % 1000 === 0 ? 0 : 1) : vis));
            const unitEl = document.getElementById('paramVisUnit');
            if (unitEl) unitEl.textContent = vis >= 1000 ? 'km' : 'm';

            // Update visibility bar
            const bar = document.getElementById('visBar');
            if (bar) {
                const pct = Math.min(100, (vis / 10000) * 100);
                bar.style.width = pct + '%';
                bar.className = 'vis-bar-fill ' + (vis >= 5000 ? 'vis-good' : vis >= 3000 ? 'vis-moderate' : 'vis-poor');
            }
            break;
        }
    }

    // Extract QNH
    const qnhMatch = raw.match(/Q(\d{4})/);
    if (qnhMatch) {
        setParam('paramQNH', qnhMatch[1]);
    }

    // Extract Cloud
    const cloudMatch = raw.match(/\b(FEW|SCT|BKN|OVC)(\d{3})(CB|TCU)?\b/);
    if (cloudMatch) {
        const height = parseInt(cloudMatch[2]) * 100;
        setParam('paramCloud', `${cloudMatch[1]} ${height}ft`);
        const extraEl = document.getElementById('paramCloudExtra');
        if (extraEl) extraEl.textContent = cloudMatch[3] ? cloudMatch[3] : '';
    }

    // Extract Weather
    const weatherCodes = ['\\+TSRA', '-TSRA', 'TSRA', '\\+TS', '-TS', 'TS', 'VCTS', '\\+RA', '-RA', 'RA', 'SH', 'FG', 'BR', 'HZ', 'DZ', 'SN', 'GR', 'SQ', 'DS', 'SS', 'FC'];
    let weatherFound = 'NIL';
    const weatherMap = {
        '+TSRA': '⛈️ Heavy Thunderstorm + Rain',
        '-TSRA': '🌩️ Light Thunderstorm + Rain',
        'TSRA': '⛈️ Thunderstorm + Rain',
        '+TS': '⛈️ Heavy Thunderstorm',
        '-TS': '🌩️ Light Thunderstorm',
        'TS': '⛈️ Thunderstorm',
        'VCTS': '🌩️ TS Vicinity',
        '+RA': '🌧️ Heavy Rain',
        '-RA': '🌦️ Light Rain',
        'RA': '🌧️ Rain',
        'SH': '🌦️ Showers',
        'FG': '🌫️ Fog',
        'BR': '🌫️ Mist',
        'HZ': '🌤️ Haze',
        'DZ': '🌧️ Drizzle',
        'SN': '❄️ Snow',
        'GR': '🧊 Hail',
        'SQ': '💨 Squall',
        'DS': '🌪️ Dust Storm',
        'SS': '🌪️ Sand Storm',
        'FC': '🌪️ Funnel Cloud'
    };

    for (const code of weatherCodes) {
        const regex = new RegExp('\\b' + code + '\\b');
        const cleanCode = code.replace(/\\/g, '');
        if (regex.test(raw)) {
            weatherFound = weatherMap[cleanCode] || cleanCode;
            break;
        }
    }
    setParam('paramWeather', weatherFound);

    // Styling khusus untuk kabut
    const weatherEl = document.getElementById('paramWeather');
    if (weatherEl) {
        if (/\b(FG|HZ|BR)\b/.test(raw)) {
            weatherEl.style.color = '#64748b'; // Abu-abu untuk kabut
            weatherEl.style.fontStyle = 'italic';
        } else {
            weatherEl.style.color = '';
            weatherEl.style.fontStyle = '';
        }
    }

    // Time
    const timeMatch = raw.match(/(\d{2})(\d{2})(\d{2})Z/);
    if (timeMatch) {
        setParam('paramTime', `${timeMatch[2]}:${timeMatch[3]} UTC`);
    }

    // Update Thunderstorm module
    updateThunderstormModule(raw);

    // Check for Rain effect
    checkRainStatus(raw);
}

// =======================
// RAIN EFFECT
// =======================
function checkRainStatus(raw) {
    // Precise regex with word boundaries to match specific weather codes
    // Matches RA, DZ, SHRA, TSRA, etc. but NOT WARR or RERA
    const rainRegex = /\b(\+|-|VC)?(RA|DZ|SHRA|TSRA|SH|SN|SG|GR|GS|PL|IC|UP)\b/;
    const hasRain = rainRegex.test(raw);
    
    if (hasRain) {
        document.body.classList.add('rain-active');
        makeItRain();
    } else {
        document.body.classList.remove('rain-active');
        stopRain();
    }
}

function makeItRain() {
    const frontRow = document.querySelector('.rain.front-row');
    const backRow = document.querySelector('.rain.back-row');
    
    // If rain is already falling, don't recreate it
    if (frontRow.children.length > 0) return;

    let increment = 0;
    let drops = "";
    let backDrops = "";

    // Higher density: increment is smaller on average
    while (increment < 100) {
        const randoHundo = Math.floor(Math.random() * 98) + 1;
        const randoFiver = Math.floor(Math.random() * 3) + 1; // Faster increment for more drops
        increment += randoFiver;

        drops += `<div class="drop" style="left: ${increment}%; bottom: ${randoFiver + randoFiver - 1 + 100}%; animation-delay: 0.${randoHundo}s; animation-duration: 0.5${randoHundo}s;">
                    <div class="stem" style="animation-delay: 0.${randoHundo}s; animation-duration: 0.5${randoHundo}s;"></div>
                    <div class="splat" style="animation-delay: 0.${randoHundo}s; animation-duration: 0.5${randoHundo}s;"></div>
                  </div>`;

        backDrops += `<div class="drop" style="right: ${increment}%; bottom: ${randoFiver + randoFiver - 1 + 100}%; animation-delay: 0.${randoHundo}s; animation-duration: 0.5${randoHundo}s;">
                        <div class="stem" style="animation-delay: 0.${randoHundo}s; animation-duration: 0.5${randoHundo}s;"></div>
                        <div class="splat" style="animation-delay: 0.${randoHundo}s; animation-duration: 0.5${randoHundo}s;"></div>
                      </div>`;
    }

    frontRow.innerHTML = drops;
    backRow.innerHTML = backDrops;
}

function stopRain() {
    const frontRow = document.querySelector('.rain.front-row');
    const backRow = document.querySelector('.rain.back-row');
    if (frontRow) frontRow.innerHTML = '';
    if (backRow) backRow.innerHTML = '';
}

function setParam(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
    el.classList.add('data-flash');
    setTimeout(() => el.classList.remove('data-flash'), 600);
}

// =======================
// CROSSWIND CALCULATOR
// =======================
function selectRunway(btn, heading) {
    document.querySelectorAll('.runway-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentRunwayHeading = heading;
    updateCrosswind();
}

function updateCrosswind() {
    if (!document.getElementById('xwHead')) return;
    if (currentWindDir === null || currentWindDir === 'VRB' || currentWindSpeed === null) {
        return;
    }

    const windDir = typeof currentWindDir === 'string' ? parseInt(currentWindDir) : currentWindDir;
    const windSpeed = currentWindSpeed;
    const rwyHdg = currentRunwayHeading;

    const angleDeg = windDir - rwyHdg;
    const angleRad = angleDeg * (Math.PI / 180);

    const headwind = Math.round(windSpeed * Math.cos(angleRad) * 10) / 10;
    const crosswind = Math.round(Math.abs(windSpeed * Math.sin(angleRad)) * 10) / 10;
    const tailwind = headwind < 0 ? Math.abs(headwind) : 0;
    const headVal = headwind > 0 ? headwind : 0;

    // Update values
    document.getElementById('xwHead').textContent = `${headVal.toFixed(1)} kt`;
    document.getElementById('xwCross').textContent = `${crosswind.toFixed(1)} kt`;
    document.getElementById('xwTail').textContent = `${tailwind.toFixed(1)} kt`;

    // Update status colors
    updateXwindStatus('xwHead', 'xwHeadStatus', headVal, 999, 'Favorable', 'Favorable', 'Favorable');
    updateXwindStatus('xwCross', 'xwCrossStatus', crosswind, 20, 'Safe', 'Monitor', 'EXCEEDED');
    updateXwindStatus('xwTail', 'xwTailStatus', tailwind, 10, 'Safe', 'Monitor', 'EXCEEDED');
}

function updateXwindStatus(parentId, statusId, value, limit, safeText, monitorText, criticalText) {
    const parent = document.getElementById(parentId).parentElement;
    const status = document.getElementById(statusId);
    if (!parent || !status) return;

    const pct = value / limit;
    parent.className = 'xwind-item ' + (pct >= 0.8 ? 'xw-critical' : pct >= 0.5 ? 'xw-monitor' : 'xw-safe');
    status.textContent = pct >= 0.8 ? criticalText : pct >= 0.5 ? monitorText : safeText;
}

// =======================
// THUNDERSTORM MODULE
// =======================
function updateThunderstormModule(raw) {
    const tsStatus = document.getElementById('tsStatus');
    const tsCB = document.getElementById('tsCB');
    const tsInMetar = document.getElementById('tsInMetar');
    const tsLightning = document.getElementById('tsLightning');
    const tsStatusText = document.getElementById('tsStatusText');
    const tsRadar = document.getElementById('tsRadar');

    if (!tsStatus) return;

    const tsCodes = ['TS', 'TSRA', 'VCTS', '+TS', 'TSGR', '-TS', '+TSRA', '-TSRA'];
    const hasTS = tsCodes.some(code => raw.includes(code));
    const hasCB = /CB\b/.test(raw);

    if (hasTS) {
        tsStatus.className = 'ts-status active';
        tsStatus.innerHTML = '<span class="lightning-icon">⚡</span> ACTIVE THUNDERSTORM DETECTED';

        // Add lightning icon to radar
        const existingIcon = tsRadar.querySelector('.ts-icon');
        if (!existingIcon) {
            const icon = document.createElement('span');
            icon.className = 'ts-icon';
            icon.textContent = '⚡';
            icon.style.top = '25%';
            icon.style.left = '60%';
            tsRadar.appendChild(icon);
        }
    } else {
        tsStatus.className = 'ts-status clear';
        tsStatus.innerHTML = '<span>✅</span> No Thunderstorm Detected';

        const existingIcon = tsRadar.querySelector('.ts-icon');
        if (existingIcon) existingIcon.remove();
    }

    if (tsCB) tsCB.textContent = hasCB ? 'Yes' : 'No';
    if (tsInMetar) tsInMetar.textContent = hasTS ? 'Yes' : 'No';
    if (tsLightning) tsLightning.textContent = hasTS ? 'Possible' : '--';
    if (tsStatusText) tsStatusText.textContent = hasTS ? 'ACTIVE' : 'Clear';

    const tsLastUpdate = document.getElementById('tsLastUpdate');
    if (tsLastUpdate) {
        const now = new Date();
        const utcStr = now.getUTCHours().toString().padStart(2, '0') + ':' +
                       now.getUTCMinutes().toString().padStart(2, '0') + ' UTC';
        tsLastUpdate.textContent = `Last Update: ${utcStr}`;
    }
}

// =======================
// DATA HANDLER (Formerly Socket Handler) - FIXED ANTI-SPAM ALARM
// =======================
function handleMetarUpdate(data) {
    console.log('Processing data update:', data);
    
    // Buat hash dari METAR untuk cek duplikat
    const currentHash = hashMetar(data.raw);
    const isDuplicate = currentHash && currentHash === alarmState.lastMetarHash;
    const isFirstLoad = alarmState.lastMetarHash === null;
    
    // 🔥 DATA FLIP-FLOP GUARD: 
    // Jika data yang datang lebih lama (older) dari yang sudah kita display, abaikan.
    if (data.last_update && alarmState.lastProcessedServerTime) {
        const incomingTime = new Date(data.last_update).getTime();
        const existingTime = new Date(alarmState.lastProcessedServerTime).getTime();
        
        if (incomingTime < existingTime) {
            console.warn('[SYNC] 🛑 Stale data detected (incoming time earlier than existing). Ignoring update.');
            return;
        }
    }
    
    // Update global state
    lastMetarRaw = data.raw;
    if (data.metar_status) lastMetarStatus = data.metar_status;
    if (data.visibility_m !== undefined) lastVisibility = data.visibility_m;
    
    // Cek kondisi berbahaya
    const tsCodes = ['TS', 'TSRA', 'VCTS', '+TS', 'TSGR'];
    const hasTS = data.raw ? tsCodes.some(c => data.raw.includes(c)) : false;
    lastHasTS = hasTS;
    
    const vis = data.visibility_m !== undefined ? data.visibility_m : null;
    const isLowVis = vis !== null && vis < 3000;

    // ============================================================
    // LOGIKA ANTI-SPAM: Jika data sama persis, hanya update UI tanpa alarm
    // ============================================================
    if (isDuplicate && !isFirstLoad) {
        console.log('[SYNC] Data duplicate detected, skipping alarm but refreshing UI components');
        updateConnectionIndicator(true);
        
        // Tetap update status panel jika ada
        if (data.auto_fetch !== undefined) {
            updateStatusPanel(data);
        }
        
        // Update last update time tracking anyway
        if (data.last_update) alarmState.lastProcessedServerTime = data.last_update;
        saveAlarmState();
        
        // Skip alarm logic but proceed to UI updates below
    }

    // ============================================================
    // DATA BARU ATAU BERUBAH - Proses alarm dengan logika yang benar
    // ============================================================
    
    // Update hash untuk tracking berikutnya
    alarmState.lastMetarHash = currentHash;
    alarmState.lastUpdateTime = Date.now();
    if (data.last_update) {
        alarmState.lastProcessedServerTime = data.last_update;
    }
    saveAlarmState(); // 🔥 COMMIT TO LOCALSTORAGE

    // Cek apakah kondisi berbahaya BARU muncul (transisi dari aman ke berbahaya)
    const isNewLowVis = isLowVis && !alarmState.lowVisTriggered;
    const isNewThunderstorm = hasTS && !alarmState.thunderstormTriggered;

    // Variable to prevent overlapping sounds
    let alarmPlayedThisCycle = false;

    // 1. Alarm untuk Low Visibility yang BARU terdeteksi
    if (isNewLowVis) {
        console.log('[ALARM] New low visibility detected!');
        document.body.classList.add('low-visibility');
        
        if (!alarmPlayedThisCycle) {
            playAlarm();
            alarmPlayedThisCycle = true;
        }
        
        showToast('⚠️ LOW VISIBILITY', `Visibility reduced to ${vis}m`, 'danger', 10000);
        alarmState.lowVisTriggered = true;
        saveAlarmState(); // 🔥 PERSIST
    } 
    // Jika visibility sudah normal, reset flag
    else if (!isLowVis && alarmState.lowVisTriggered) {
        alarmState.lowVisTriggered = false;
        document.body.classList.remove('low-visibility');
        saveAlarmState(); // 🔥 PERSIST
    }

    // 2. Alarm untuk Thunderstorm yang BARU terdeteksi
    if (isNewThunderstorm) {
        console.log('[ALARM] New thunderstorm detected!');
        showToast('⚡ THUNDERSTORM', 'Thunderstorm detected in METAR', 'danger', 10000);
        
        if (!alarmPlayedThisCycle) {
            playAlarm();
            alarmPlayedThisCycle = true;
        }
        
        alarmState.thunderstormTriggered = true;
        saveAlarmState(); // 🔥 PERSIST
    }
    // Jika TS sudah tidak ada, reset flag
    else if (!hasTS && alarmState.thunderstormTriggered) {
        alarmState.thunderstormTriggered = false;
        saveAlarmState(); // 🔥 PERSIST
    }

    // 3. Notifikasi suara untuk data baru (bukan alarm, hanya notify)
    if (!isFirstLoad && soundEnabled && data.status === 'new') {
        if (hasTS) {
            if (!alarmPlayedThisCycle) {
                playAlarm();
                alarmPlayedThisCycle = true;
            }
        } else {
            if (!alarmPlayedThisCycle) {
                playNotify();
            }
        }
    }

    // ============================================================
    // UI UPDATES (hanya jika data berbeda atau first load)
    // ============================================================
    
    // Show toast untuk data baru (non-critical)
    if (!isFirstLoad && data.raw && data.status === 'new') {
        showToast('New METAR Received', `${STATION} — ${new Date().toUTCString().slice(17, 25)} UTC`);
    }

    // Update METAR raw display
    if (data.raw) {
        const rawEl = document.getElementById('metarRawCode');
        if (rawEl) {
            let htmlStr = highlightMetar(data.raw);
            // Badge untuk COR/AMD/SPECI
            if (data.raw.includes(' COR ') || data.raw.includes('METAR COR') || data.raw.includes('CCA')) {
                htmlStr += ' <span class="badge" style="background-color: #f59e0b; color: #1e293b; margin-left: 10px; font-size: 0.75rem; padding: 4px 8px; vertical-align: middle;">⚠️ CORRECTION</span>';
            } else if (data.raw.includes(' AMD ') || data.raw.includes('METAR AMD')) {
                htmlStr += ' <span class="badge" style="background-color: #3b82f6; color: #ffffff; margin-left: 10px; font-size: 0.75rem; padding: 4px 8px; vertical-align: middle;">⚠️ AMD</span>';
            } else if (data.raw.includes(' SPECI ') || data.raw.startsWith('SPECI ')) {
                htmlStr += ' <span class="badge" style="background-color: #ef4444; color: #ffffff; margin-left: 10px; font-size: 0.75rem; padding: 4px 8px; vertical-align: middle;">⚠️ SPECI</span>';
            }
            rawEl.innerHTML = htmlStr;
        }

        // Update panel status color
        const panel = document.getElementById('metarRawPanel');
        if (panel) {
            panel.classList.remove('status-danger', 'status-warning');
            if (hasTS || isLowVis) {
                panel.classList.add('status-danger');
            } else if (vis !== null && vis <= 5000) {
                panel.classList.add('status-warning');
            }
        }
        
        updateDecodedPanel(data.raw);

        // 🔥 AKTIFKAN EFEK KABUT JIKA ADA FG/HZ/BR
        if (typeof updateFogEffect === 'function') {
            if (!window.isManualSession) updateFogEffect(data);
        }
    }

    // Update QAM dan Narrative
    if (data.qam) {
        const qamEl = document.getElementById('qamDisplay');
        if (qamEl) qamEl.textContent = data.qam;
    }

    if (data.narrative) {
        const narEl = document.getElementById('narrativeDisplay');
        if (narEl) narEl.textContent = data.narrative;
    }

    // Update last saved time
    const now = new Date();
    const formatted = now.toLocaleDateString('id-ID') + ' ' +
        now.getHours().toString().padStart(2, '0') + ':' +
        now.getMinutes().toString().padStart(2, '0') + ':' +
        now.getSeconds().toString().padStart(2, '0');

    const lastSaved = document.getElementById('lastSaved');
    if (lastSaved) lastSaved.textContent = formatted;

    // Refresh ALL UI Components in sync
    if (typeof loadHistory === 'function') loadHistory(); // Updates Charts
    if (typeof updateHistoryTable === 'function') updateHistoryTable(); // Updates Table
    if (typeof loadWindCompass === 'function') loadWindCompass();
    if (typeof loadWindRose === 'function') loadWindRose();
    
    // updateMiniTimeline(); // 🔥 DISABLED
    
    runMetarValidation(data.raw);
    
    // Update status panel
    if (data.auto_fetch !== undefined) {
        updateStatusPanel(data);
    }
    
    // Update connection indicator
    updateConnectionIndicator(true);
}

// =======================
// SYSTEM CONTROL
// =======================
function updateStatusPanel(data) {
    const serverEl = document.getElementById('server-status');
    const metarEl = document.getElementById('metar-status');
    const lastEl = document.getElementById('last-update-status');
    
    if (serverEl) serverEl.textContent = "ONLINE 🟢";
    if (metarEl) {
        metarEl.textContent = data.auto_fetch ? "RUNNING 🟢" : "PAUSED 🟡";
        metarEl.className = 'status-value ' + (data.auto_fetch ? 'status-running' : 'status-paused');
    }
    
    if (lastEl) {
        if (data.last_update) {
            const date = new Date(data.last_update);
            const timeStr = date.getHours().toString().padStart(2, '0') + ':' + 
                            date.getMinutes().toString().padStart(2, '0') + ' WIB';
            lastEl.textContent = timeStr;
        } else {
            lastEl.textContent = "WAITING...";
        }
    }
}

async function toggleSystem() {
    try {
        const res = await fetch("/api/toggle_fetch", { method: "POST" });
        const data = await res.json();
        
        // Update local state and persistence
        autoFetchEnabled = data.auto_fetch;
        localStorage.setItem('autoFetchEnabled', autoFetchEnabled);
        
        updateStatusPanel(data);
        showToast(
            'System Control',
            `Auto fetch: ${data.auto_fetch ? 'ENABLED ✅' : 'DISABLED ⏸️'}`,
            data.auto_fetch ? 'success' : 'warning'
        );
    } catch (err) {
        console.error("Toggle system failed:", err);
        showToast('Error', 'Failed to toggle system', 'danger');
    }
}

// Global expose
window.toggleSystem = toggleSystem;

// Initial health check
fetch("/health")
    .then(r => r.json())
    .then(updateStatusPanel)
    .catch(() => console.warn("Initial health check failed"));

// =======================
// ALERT BANNER
// =======================
function showAlert(title, message, level = 'critical') {
    const banner = document.getElementById('alertBanner');
    if (!banner) return;
    banner.className = `alert-banner visible alert-${level}`;
    document.getElementById('alertTitle').textContent = title;
    document.getElementById('alertMessage').textContent = message;
}

function dismissAlert() {
    const banner = document.getElementById('alertBanner');
    if (banner) banner.classList.remove('visible');
}

// =======================
// COPY FUNCTIONS
// =======================
function copyMetar() {
    const el = document.getElementById('metarRawCode');
    if (!el) return;
    navigator.clipboard.writeText(el.textContent).then(() => {
        const fb = document.getElementById('metarCopyFeedback');
        if (fb) { fb.classList.add('visible'); setTimeout(() => fb.classList.remove('visible'), 2000); }
    });
}

function copyQam() {
    const el = document.getElementById('qamDisplay');
    if (!el) return;
    navigator.clipboard.writeText(el.textContent).then(() => {
        const fb = document.getElementById('qamCopyFeedback');
        if (fb) { fb.classList.add('visible'); setTimeout(() => fb.classList.remove('visible'), 2000); }
    });
}

window.copyMetar = copyMetar;
window.copyQam = copyQam;

// =======================
// SOUND FUNCTIONS
// =======================
function enableSound() {
    soundEnabled = !soundEnabled;
    const btnHeader = document.getElementById('soundToggle');
    const btnSidebar = document.getElementById('soundToggleSidebar');
    const iconSidebar = document.getElementById('soundIconSidebar');
    const labelSidebar = document.getElementById('soundLabelSidebar');

    // Save state to localStorage
    localStorage.setItem('soundEnabled', soundEnabled);

    // Update Header
    if (btnHeader) {
        btnHeader.textContent = soundEnabled ? '🔊 Sound ON' : '🔇 Sound OFF';
        if (soundEnabled) btnHeader.classList.add('active');
        else btnHeader.classList.remove('active');
    }
    
    // Update Sidebar
    if (btnSidebar) {
        if (iconSidebar) iconSidebar.textContent = soundEnabled ? '🔊' : '🔇';
        if (labelSidebar) labelSidebar.textContent = soundEnabled ? 'Sound ON' : 'Sound OFF';
        if (soundEnabled) btnSidebar.classList.add('active');
        else btnSidebar.classList.remove('active');
    }

    if (soundEnabled) {
        console.log('Sound ENABLED');

        // Immediate check: If there's an active critical condition, play alarm now
        if (lastVisibility !== null && lastVisibility < 3000 || lastHasTS || lastMetarStatus === 'danger') {
            console.log('Immediate alert: critical condition detected on activation');
            playAlarm();
        }
    } else {
        console.log('Sound DISABLED');
    }
}
window.enableSound = enableSound;

function playAlarm() {
    if (!soundEnabled) {
        console.log('Sound disabled, skipping alarm');
        return;
    }
    const a = document.getElementById('lowVisSound');
    if (a) {
        a.currentTime = 0;
        a.play()
            .then(() => console.log('Alarm played'))
            .catch(error => {
                console.error('Failed to play alarm:', error);
                // Try to load and play again
                a.load();
                a.play().catch(e => console.error('Retry also failed:', e));
            });
    } else {
        console.error('Alarm audio element not found');
    }
}

function playNotify() {
    if (!soundEnabled) {
        console.log('Sound disabled, skipping notify');
        return;
    }
    const a = document.getElementById('newDataSound');
    if (a) {
        a.currentTime = 0;
        a.play()
            .then(() => console.log('Notify sound played'))
            .catch(error => {
                console.error('Failed to play notify sound:', error);
                // Try loading the audio again
                a.load();
                a.play().catch(e => console.warn('Retry also failed:', e));
            });
    } else {
        console.error('Notify audio element not found');
    }
}

// =======================
// CHARTS (Light Theme)
// =======================
const chartColors = {
    tempLine: '#DC2626',
    tempFill: 'rgba(220, 38, 38, 0.08)',
    pressureLine: '#2E5C8A',
    pressureFill: 'rgba(46, 92, 138, 0.08)',
    windLine: '#E8B339',
    windFill: 'rgba(232, 179, 57, 0.1)',
    gustLine: '#059669',
    gustFill: 'rgba(5, 150, 105, 0.08)',
    tickColor: '#64748B',
    refLine: 'rgba(30, 58, 95, 0.3)',
    legendColor: '#475569'
};

function getDynamicChartColors() {
    const isDark = currentTheme === 'dark';
    return {
        grid: isDark ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.06)',
        ticks: isDark ? '#94A3B8' : '#64748B',
        legend: isDark ? '#F1F5F9' : '#475569'
    };
}

function chartDefaults() {
    return {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 800, easing: 'easeOutQuart' },
        interaction: { mode: 'index', intersect: false },
        plugins: {
            legend: {
                labels: {
                    font: { family: 'Inter', weight: '600', size: 12 },
                    color: getDynamicChartColors().legend,
                    usePointStyle: true,
                    padding: 28 // Increased from 16 for better margin
                }
            },
            tooltip: {
                backgroundColor: '#1E3A5F',
                titleFont: { family: 'Inter', size: 13, weight: '700' },
                bodyFont: { family: 'JetBrains Mono', size: 12 },
                padding: 12,
                cornerRadius: 8,
                borderColor: 'rgba(232,179,57,0.3)',
                borderWidth: 1
            }
        },
        layout: {
            padding: {
                top: 10 // Extra room above the legend
            }
        },
        scales: {
            y: {
                beginAtZero: false,
                grace: '10%', // Prevents chart data from touching the top edge
                grid: { color: getDynamicChartColors().grid, drawBorder: false },
                ticks: { color: getDynamicChartColors().ticks, font: { family: 'Inter', weight: '600', size: 11 } }
            },
            x: {
                grid: { display: false },
                ticks: {
                    color: getDynamicChartColors().ticks,
                    font: { family: 'Inter', size: 10 },
                    maxTicksLimit: 10,
                    maxRotation: 45
                }
            }
        }
    };
}

function createCharts() {
    const tempCanvas = document.getElementById('tempChart');
    const pressureCanvas = document.getElementById('pressureChart');
    if (!tempCanvas || !pressureCanvas) return;

    if (tempChart) tempChart.destroy();
    if (pressureChart) pressureChart.destroy();

    tempChart = new Chart(tempCanvas.getContext('2d'), {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Temperature (°C)',
                data: [],
                borderColor: chartColors.tempLine,
                backgroundColor: chartColors.tempFill,
                borderWidth: 2.5,
                tension: 0.4,
                fill: true,
                pointRadius: 3,
                pointHoverRadius: 6,
                pointBackgroundColor: chartColors.tempLine
            }]
        },
        options: chartDefaults()
    });

    pressureChart = new Chart(pressureCanvas.getContext('2d'), {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'QNH (hPa)',
                data: [],
                borderColor: chartColors.pressureLine,
                backgroundColor: chartColors.pressureFill,
                borderWidth: 2.5,
                tension: 0.4,
                fill: true,
                pointRadius: 3,
                pointHoverRadius: 6,
                pointBackgroundColor: chartColors.pressureLine
            }]
        },
        options: chartDefaults()
    });
}

function createWindChart() {
    const canvas = document.getElementById('windChart');
    if (!canvas) return;
    if (windChart) windChart.destroy();

    windChart = new Chart(canvas.getContext('2d'), {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                {
                    label: 'Wind Speed (KT)',
                    data: [],
                    borderColor: chartColors.windLine,
                    backgroundColor: chartColors.windFill,
                    borderWidth: 2.5,
                    tension: 0.4,
                    fill: true,
                    pointRadius: 3,
                    pointHoverRadius: 6,
                    pointBackgroundColor: chartColors.windLine
                },
                {
                    label: 'Wind Gust (KT)',
                    data: [],
                    borderColor: chartColors.gustLine,
                    backgroundColor: chartColors.gustFill,
                    borderWidth: 2,
                    borderDash: [5, 5],
                    tension: 0.4,
                    fill: true,
                    spanGaps: true,
                    pointRadius: 3,
                    pointHoverRadius: 5,
                    pointBackgroundColor: chartColors.gustLine,
                    hidden: true
                }
            ]
        },
        options: {
            ...chartDefaults(),
            scales: {
                ...chartDefaults().scales,
                y: {
                    ...chartDefaults().scales.y,
                    beginAtZero: true,
                    grace: '10%', // Provides headroom above top line
                    title: {
                        display: true,
                        text: 'Speed (KT)',
                        color: '#475569',
                        font: { family: 'Inter', weight: '700' }
                    }
                }
            }
        }
    });
}

// =======================
// FETCH & UPDATE CHARTS
// =======================
async function loadHistory() {
    if (!document.getElementById('tempChart')) return;
    console.log('[CHART] Requesting history update...');
    try {
        const res = await fetch('/api/metar/history');
        const result = await res.json();
        
        if (!result.data || result.data.length === 0) {
            console.warn('[CHART] No history data received');
            return;
        }

        console.log(`[CHART] History loaded: ${result.data.length} records`);

        const labels = result.labels || result.data.map(r => r.time);
        const temps = result.temps || result.data.map(r => r.temp);
        const pressures = result.pressures || result.data.map(r => r.pressure);
        const winds = result.data.map(r => r.wind);
        const gusts = result.data.map(r => r.gust);

        if (!tempChart || !pressureChart) {
            console.log('[CHART] Initializing charts...');
            createCharts();
        }
        if (!windChart) createWindChart();

        // Update datasets
        if (tempChart && tempChart.data) {
            tempChart.data.labels = labels;
            tempChart.data.datasets[0].data = temps;
            tempChart.update('active');
        }
        pressureChart.update('active');
        windChart.update('active');
        
        // Update data summary indicators (footers)
        const infoText = `${result.range.start} to ${result.range.end} • ${result.count} records (from ${result.source})`;
        ['tempChart-info', 'pressureChart-info', 'windChart-info'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.textContent = infoText;
        });

        console.log('[CHART] Charts successfully updated with summary info');
    } catch (e) {
        console.error('[CHART] Error updating charts:', e);
    }
}

// Alias for compatibility
function updateCharts() {
    loadHistory();
}

// =======================
// FETCH METAR DATA
// =======================
// 🗑️ fetchMetar() removed - replaced by pollLatestData() for consistency

// =======================
// HISTORY TABLE UPDATE
// =======================
async function updateHistoryTable() {
    try {
        const res = await fetch('/api/history');
        const result = await res.json();
        const tbody = document.getElementById('historyTableBody');
        if (!tbody) return;

        tbody.innerHTML = '';
        if (result.data && result.data.length > 0) {
            result.data.forEach((item, i) => {
                const row = document.createElement('tr');
                row.style.animation = `fadeIn 0.3s ease-out ${i * 0.03}s both`;
                let badgeHtml = '';
                if (item.metar.includes(' COR ') || item.metar.startsWith('METAR COR') || item.metar.includes('CCA') || item.metar.includes(' CCA ')) {
                    badgeHtml = '<span class="badge badge-cor" style="background-color: #f59e0b; color: #1e293b; margin-left: 6px; font-size: 0.70rem; padding: 2px 6px; vertical-align: middle;">🟡 COR</span>';
                } else if (item.metar.includes(' AMD ') || item.metar.startsWith('METAR AMD')) {
                    badgeHtml = '<span class="badge badge-amd" style="background-color: #3b82f6; color: #ffffff; margin-left: 6px; font-size: 0.70rem; padding: 2px 6px; vertical-align: middle;">🔵 AMD</span>';
                } else if (item.metar.includes(' SPECI ') || item.metar.startsWith('SPECI ')) {
                    badgeHtml = '<span class="badge badge-speci" style="background-color: #ef4444; color: #ffffff; margin-left: 6px; font-size: 0.70rem; padding: 2px 6px; vertical-align: middle;">🔴 SPECI</span>';
                } else {
                    badgeHtml = '<span class="badge badge-metar" style="background-color: #059669; color: #ffffff; margin-left: 6px; font-size: 0.70rem; padding: 2px 6px; vertical-align: middle;">🟢 METAR</span>';
                }
                
                row.innerHTML = `
                    <td>${item.time}</td>
                    <td>${item.station}</td>
                    <td class="metar-cell">${item.metar}${badgeHtml}</td>
                `;
                tbody.appendChild(row);
            });
        } else {
            tbody.innerHTML = '<tr><td colspan="3" class="text-center text-muted">No data available</td></tr>';
        }
    } catch (e) {
        console.error('History table error:', e);
    }
}

// =======================
// MINI TIMELINE RENDERER
// =======================
async function updateMiniTimeline() {
    try {
        const res = await fetch('/api/history');
        const result = await res.json();
        const tlContainer = document.getElementById('metarTimeline');
        
        // This element only exists on index.html
        if (!tlContainer) return;

        tlContainer.innerHTML = '';
        
        if (result.data && result.data.length > 0) {
            // We want chronological order for timeline (oldest to newest left to right)
            // The API returns newest first (descending), so we reverse it or just slice
            // Let's show the last 5 reports for the timeline
            const timelineData = result.data.slice(0, 5).reverse();
            
            timelineData.forEach((item) => {
                // Extract just the HH:MM
                const timeMatch = item.time.match(/\d{4}-\d{2}-\d{2}\s(\d{2}:\d{2})/);
                const shortTime = timeMatch ? timeMatch[1] : item.time;
                
                let badgeText = 'METAR';
                let badgeColor = '#059669'; // Emerald
                
                if (item.metar.includes(' COR ') || item.metar.startsWith('METAR COR') || item.metar.includes('CCA') || item.metar.includes(' CCA ')) {
                    badgeText = 'COR';
                    badgeColor = '#f59e0b'; // Amber
                } else if (item.metar.includes(' AMD ') || item.metar.startsWith('METAR AMD')) {
                    badgeText = 'AMD';
                    badgeColor = '#3b82f6'; // Blue
                } else if (item.metar.includes(' SPECI ') || item.metar.startsWith('SPECI ')) {
                    badgeText = 'SPECI';
                    badgeColor = '#ef4444'; // Red
                }

                const node = document.createElement('div');
                node.style.cssText = 'display: flex; flex-direction: column; align-items: center; min-width: 60px;';
                node.innerHTML = `
                    <span style="font-weight: bold; margin-bottom: 4px;">${shortTime}</span>
                    <span style="background-color: ${badgeColor}; color: ${badgeText === 'COR' ? '#1e293b' : '#fff'}; padding: 2px 8px; border-radius: 4px; font-size: 0.70rem; font-weight: bold;">
                        ${badgeText}
                    </span>
                `;
                
                tlContainer.appendChild(node);
                
                // Add a connector line if it's not the last element (handled implicitly via gap in CSS)
            });
        } else {
            tlContainer.innerHTML = '<span style="color: #64748b;">No recent reports</span>';
        }
    } catch (e) {
        console.error('Mini timeline error:', e);
    }
}

// =======================
// WIND COMPASS (Plotly)
// =======================
function loadWindCompass() {
    fetch(`/api/metar/${STATION}`)
        .then(r => r.json())
        .then(data => {
            if (data.error || !data.wind_direction) return;

            let windDir = data.wind_direction === 'VRB' ? 0 : parseInt(data.wind_direction);
            let windSpeed = data.wind_speed || 0;

            // Update global variables for crosswind calculator
            currentWindDir = windDir;
            currentWindSpeed = windSpeed;

            // Update crosswind calculator
            updateCrosswind();

            // Update compass display
            updateWindCompassDisplay(windDir, windSpeed);
        })
        .catch(e => console.error('Wind compass error:', e));
}

function updateWindCompassDisplay(windDir, windSpeed) {
    if (!windDir && windDir !== 0) return;
    
    const dir = windDir === 'VRB' ? 0 : windDir;
    const speed = windSpeed || 0;
    const isDark = currentTheme === 'dark';
    const accentColor = isDark ? '#F59E0B' : '#DC2626'; // Amber in dark mode, Red in light
    const textColor = isDark ? '#F1F5F9' : '#1E3A5F';
    const gridColor = isDark ? 'rgba(255, 255, 255, 0.4)' : '#1E3A5F';

    Plotly.newPlot('windCompassChart', [{
        type: 'scatterpolar',
        r: [0, 1],
        theta: [0, dir],
        mode: 'lines+markers',
        line: { color: accentColor, width: 4 },
        marker: { size: 10, color: accentColor },
        fill: 'toself',
        fillcolor: isDark ? 'rgba(245, 158, 11, 0.15)' : 'rgba(220, 38, 38, 0.1)'
    }], {
        polar: {
            bgcolor: 'rgba(0,0,0,0)',
            angularaxis: {
                direction: 'clockwise',
                rotation: 90,
                tickmode: 'array',
                tickvals: [0, 45, 90, 135, 180, 225, 270, 315],
                ticktext: ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'],
                tickfont: { size: 13, color: gridColor, family: 'Inter', weight: isDark ? 'bold' : 'normal' },
                gridcolor: isDark ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.1)'
            },
            radialaxis: { visible: false }
        },
        showlegend: false,
        margin: { t: 40, b: 40, l: 40, r: 40 },
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        annotations: [{
            text: `<b>${dir}°</b><br>${speed} kt`,
            showarrow: false,
            font: { size: 24, color: textColor, family: 'Inter' },
            y: 0.5, x: 0.5, xref: 'paper', yref: 'paper'
        }]
    }, { responsive: true });
}
// WIND ROSE (Plotly)
// =======================
async function loadWindRose(station = STATION) {
    if (typeof Plotly === 'undefined') return;
    if (!station) return;

    try {
        // 1. Fetch & Render 24h Wind Rose
        const res24h = await fetch(`/api/windrose/${station}`);
        const data24h = await res24h.json();
        
        renderWindRose('windRose24h', data24h.data, {
            title: 'Last 24 Hours',
            colorScale: currentTheme === 'dark' 
                ? [[0, '#4ade80'], [0.5, '#facc15'], [1, '#f87171']] 
                : [[0, '#2E5C8A'], [0.5, '#E8B339'], [1, '#DC2626']]
        });
        
        const badge24h = document.getElementById('windrose24h-badge');
        const info24h = document.getElementById('windrose24h-info');
        if (badge24h && data24h.count !== undefined) {
            badge24h.textContent = `${data24h.count} records`;
        }
        if (info24h && data24h.range) {
            info24h.textContent = `${data24h.range.start} to ${data24h.range.end} • ${data24h.count} records (from ${data24h.source || 'Sheets'})`;
        }

        // 2. Fetch & Render Monthly Wind Rose
        const resMonth = await fetch(`/api/windrose-monthly/${station}`);
        const dataMonth = await resMonth.json();
        
        renderWindRose('windRoseMonth', dataMonth.data, {
            title: `${dataMonth.month_name} ${dataMonth.year}`,
            colorScale: currentTheme === 'dark' 
                ? [[0, '#10b981'], [0.5, '#facc15'], [1, '#f87171']] 
                : [[0, '#059669'], [0.5, '#E8B339'], [1, '#DC2626']]
        });
        
        const badgeMonth = document.getElementById('windroseMonth-badge');
        const infoMonth = document.getElementById('windroseMonth-info');
        if (badgeMonth) badgeMonth.textContent = `${dataMonth.month_name} ${dataMonth.year}`;
        if (infoMonth) {
            infoMonth.textContent = `${dataMonth.range.start} to ${dataMonth.range.end} • ${dataMonth.count} records (from Sheets)`;
        }

    } catch (e) {
        console.error('Dual Wind Rose error:', e);
    }
}

function renderWindRose(containerId, data, options) {
    const container = document.getElementById(containerId);
    if (!container) return;

    if (!data || data.length === 0) {
        container.innerHTML = 
            '<div style="display:flex;justify-content:center;align-items:center;height:100%;color:#64748B;font-family:Inter;font-size:0.9rem;">No wind data available</div>';
        return;
    }

    const isDark = currentTheme === 'dark';
    const gridColor = isDark ? 'rgba(255, 255, 255, 0.4)' : '#1E3A5F';

    Plotly.newPlot(containerId, [{
        type: 'barpolar',
        r: data.map(d => d.speed),
        theta: data.map(d => d.dir),
        customdata: data.map(d => d.utc_time || ''),
        marker: {
            color: data.map(d => d.speed),
            colorscale: options.colorScale,
            showscale: true,
            colorbar: { 
                title: 'kt', 
                thickness: 12, 
                len: 0.6,
                tickfont: { family: 'Inter', size: 10, color: isDark ? '#F1F5F9' : '#475569' },
                titlefont: { family: 'Inter', size: 12, color: isDark ? '#F1F5F9' : '#475569' }
            }
        },
        hovertemplate: '<b>Dir: %{theta}°</b><br>Speed: %{r} KT<br>Time: %{customdata}<extra></extra>',
        opacity: 0.85
    }], {
        polar: {
            bgcolor: 'rgba(0,0,0,0)',
            angularaxis: {
                direction: 'clockwise',
                rotation: 90,
                tickmode: 'array',
                tickvals: [0, 45, 90, 135, 180, 225, 270, 315],
                ticktext: ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'],
                tickfont: { size: 12, color: gridColor, family: 'Inter', weight: isDark ? 'bold' : 'normal' }
            },
            radialaxis: { 
                showgrid: true, 
                gridcolor: isDark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.06)',
                title: 'KT',
                tickfont: { color: isDark ? '#94A3B8' : '#64748B' }
            }
        },
        showlegend: false,
        margin: { t: 60, b: 40, l: 60, r: 60 },
        paper_bgcolor: 'rgba(0,0,0,0)', 
        plot_bgcolor: 'rgba(0,0,0,0)',
        title: {
            text: options.title || '',
            font: { family: 'Inter', size: 18, color: isDark ? '#F1F5F9' : '#475569', weight: 'bold' },
            y: 0.98,
            x: 0.5,
            xanchor: 'center',
            yanchor: 'top'
        }
    }, { responsive: true });
}

setInterval(loadWindCompass, 5000);
setInterval(loadWindRose, 10000);

/**
 * Global Chart Download Handler
 * Supports both Chart.js and Plotly.js charts
 */
function downloadChart(chartId) {
    console.log(`[DOWNLOAD] Initiating export for: ${chartId}`);
    
    // 1. Handle Plotly.js Charts (Compass & Roses)
    const plotlyCharts = ['windCompassChart', 'windRose24h', 'windRoseMonth'];
    if (plotlyCharts.includes(chartId)) {
        if (typeof Plotly !== 'undefined') {
            const filename = chartId.replace(/([A-Z])/g, '_$1').replace(/^./, str => str.toUpperCase());
            Plotly.downloadImage(chartId, {
                format: 'png',
                width: 1000,
                height: 800,
                filename: `${filename}_${STATION}`
            });
        }
        return;
    }

    // 2. Handle Chart.js Plots (Trends)
    let chartInstance = null;
    let fallbackFilename = 'Chart';

    if (chartId === 'tempChart') { chartInstance = tempChart; fallbackFilename = 'Temperature_Trend'; }
    else if (chartId === 'pressureChart') { chartInstance = pressureChart; fallbackFilename = 'Pressure_Trend'; }
    else if (chartId === 'windChart') { chartInstance = windChart; fallbackFilename = 'Wind_Speed_Trend'; }

    if (chartInstance) {
        const link = document.createElement('a');
        link.download = `${fallbackFilename}_${STATION}_${new Date().toISOString().slice(0, 10)}.png`;
        link.href = chartInstance.toBase64Image();
        link.click();
    } else {
        // Fallback for canvas elements not using the global instances
        const canvas = document.getElementById(chartId);
        if (canvas && canvas.toDataURL) {
            const link = document.createElement('a');
            link.download = `${chartId}_Export.png`;
            link.href = canvas.toDataURL('image/png');
            link.click();
        } else {
            console.error(`[DOWNLOAD] Chart instance or canvas not found for ID: ${chartId}`);
        }
    }
}

// =======================
// INITIALIZATION
// =======================
document.addEventListener('DOMContentLoaded', function () {
    // 1. Sync Sound Button UI FIRST (Most critical for UX persistence)
    const soundToggleHeader = document.getElementById('soundToggle');
    const soundToggleSidebar = document.getElementById('soundToggleSidebar');
    const soundIconSidebar = document.getElementById('soundIconSidebar');
    const soundLabelSidebar = document.getElementById('soundLabelSidebar');

    if (soundEnabled) {
        if (soundToggleHeader) {
            soundToggleHeader.textContent = '🔊 Sound ON';
            soundToggleHeader.classList.add('active');
        }
        if (soundToggleSidebar) {
            if (soundIconSidebar) soundIconSidebar.textContent = '🔊';
            if (soundLabelSidebar) soundLabelSidebar.textContent = 'Sound ON';
            soundToggleSidebar.classList.add('active');
        }
    } else {
        if (soundToggleHeader) {
            soundToggleHeader.textContent = '🔇 Sound OFF';
            soundToggleHeader.classList.remove('active');
        }
        if (soundToggleSidebar) {
            if (soundIconSidebar) soundIconSidebar.textContent = '🔇';
            if (soundLabelSidebar) soundLabelSidebar.textContent = 'Sound OFF';
            soundToggleSidebar.classList.remove('active');
        }
    }

    // 2. Clocks
    updateClocks();

    // 3. Sidebar
    initSidebar();

    // 4. Theme (Apply saved preference)
    applyTheme(currentTheme);

    // 5. Staggered card fade-in
    document.querySelectorAll('.card, .metar-raw-panel').forEach((el, i) => {
        if (el) el.style.animationDelay = `${0.05 + i * 0.06}s`;
    });

    // Create charts
    createCharts();
    createWindChart();
    updateCharts();
    loadHistory();
    // updateMiniTimeline(); // 🔥 DISABLED (As requested)
    loadWindCompass();
    loadWindRose();

    // 6. Instant UI Population from SSR content (Eliminates loading delay)
    const initialRaw = document.getElementById('metarRawCode');
    if (initialRaw) {
        // Clean text content (remove any existing badge text that might be inside)
        // We clone it to get clean text without the badge span contents if they were rendered
        const cleanText = initialRaw.innerText.split('⚠️')[0].trim().split('🟢')[0].trim();
        
        if (cleanText && cleanText !== '{{ latest.metar }}' && cleanText !== '--') {
            console.log('[INIT] Pre-populating UI from clean text:', cleanText);
            
            initialRaw.innerHTML = highlightMetar(cleanText);
            
            // Re-append badges based on text content
            if (cleanText.includes(' COR ') || cleanText.includes('CCA')) {
                initialRaw.innerHTML += ' <span class="badge" style="background-color: #f59e0b; color: #1e293b; margin-left: 10px; font-size: 0.75rem; padding: 4px 8px; vertical-align: middle;">⚠️ COR</span>';
            } else if (cleanText.includes(' AMD ')) {
                initialRaw.innerHTML += ' <span class="badge" style="background-color: #3b82f6; color: #ffffff; margin-left: 10px; font-size: 0.75rem; padding: 4px 8px; vertical-align: middle;">⚠️ AMD</span>';
            } else if (cleanText.includes(' SPECI ')) {
                initialRaw.innerHTML += ' <span class="badge" style="background-color: #ef4444; color: #ffffff; margin-left: 10px; font-size: 0.75rem; padding: 4px 8px; vertical-align: middle;">⚠️ SPECI</span>';
            }

            updateDecodedPanel(cleanText);
            runMetarValidation(cleanText);
            lastMetarRaw = cleanText;
            alarmState.lastMetarHash = hashMetar(cleanText);
            saveAlarmState();
        }
    }

    // 7. Sync System Fetch Status and trigger first poll
    console.log('[INIT] Syncing system fetch status with server...');
    fetch("/api/set_fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: autoFetchEnabled })
    })
    .then(r => r.json())
    .then(data => {
        updateStatusPanel(data);
        console.log('[INIT] Server fetch status synced:', data.auto_fetch);
        // Start polling now that we are synced
        pollLatestData();
    })
    .catch(err => {
        console.error('[INIT] Failed to sync fetch status:', err);
        pollLatestData(); // Fallback to poll anyway
    });

    // Initial poll will handle first load
    // setInterval(fetchMetar, 60000); // 🗑️ REMOVED REDUNDANT LOOP
});

// =======================
// HELP MODAL FUNCTIONS
// =======================
function toggleHelpModal() {
    const modal = document.getElementById('helpModal');
    const overlay = document.getElementById('helpModalOverlay');
    
    if (modal.classList.contains('active')) {
        closeHelpModal();
    } else {
        modal.classList.add('active');
        overlay.classList.add('active');
        document.body.style.overflow = 'hidden'; // Prevent background scroll
    }
}

function closeHelpModal() {
    const modal = document.getElementById('helpModal');
    const overlay = document.getElementById('helpModalOverlay');
    
    modal.classList.remove('active');
    overlay.classList.remove('active');
    document.body.style.overflow = ''; // Restore scroll
}

// Close modal dengan tombol Escape
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        closeHelpModal();
    }
});

// Global expose
window.toggleHelpModal = toggleHelpModal;
window.closeHelpModal = closeHelpModal;

// =======================
// FOG / MIST / HAZE EFFECT CONTROLLER (VANTA.JS)
// =======================

let vantaFogInstance = null;
const fogState = {
    isActive: false,
    currentType: null, // 'FG', 'HZ', 'BR', atau null
    lastWeather: null
};

/**
 * Watch for dark/light mode toggle to update Vanta colors
 */
const toggleObserver = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
        if (mutation.attributeName === 'data-theme' && fogState.isActive) {
            console.log('[FOG] Theme changed, updating Vanta colors...');
            applyVantaFog(fogState.currentType);
        }
    });
});
if (document.documentElement) {
    toggleObserver.observe(document.documentElement, { attributes: true });
}

function applyVantaFog(fogType) {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    
    // Default FOG (FG) - Original thickness
    let vantaConfig = {
        highlightColor: isDark ? 0xcccccc : 0x888888,
        midtoneColor: isDark ? 0x888888 : 0xaaaaaa,
        lowlightColor: isDark ? 0x222222 : 0xdddddd,
        baseColor: isDark ? 0x000000 : 0xffffff,
        blurFactor: 0.5,
        speed: 0.6,
        zoom: 1.2
    };
    
    if (fogType === 'HZ') {
        // Haze - Saturated for light mode, luminous for dark mode, thicker mist
        vantaConfig.highlightColor = isDark ? 0xebe1cb : 0x8c7c61;
        vantaConfig.midtoneColor = isDark ? 0xbdaf91 : 0x9c8e76;
        vantaConfig.lowlightColor = isDark ? 0x333027 : 0xc0b9a3;
        vantaConfig.blurFactor = 0.45; 
        vantaConfig.speed = 0.4;
        vantaConfig.zoom = 1.8;
    } else if (fogType === 'BR') {
        // Mist - Original thickness
        vantaConfig.highlightColor = isDark ? 0xaaaaaa : 0x94a3b8;
        vantaConfig.midtoneColor = isDark ? 0x666666 : 0xcbd5e1;
        vantaConfig.lowlightColor = isDark ? 0x222222 : 0xe2e8f0;
        vantaConfig.blurFactor = 0.3;
        vantaConfig.speed = 0.5;
        vantaConfig.zoom = 1.0;
    }
    
    if (vantaFogInstance) {
        console.log(`[FOG] Updating Vanta instance for ${fogType} (Softened)...`);
        vantaFogInstance.setOptions(vantaConfig);
    } else {
        if (typeof VANTA === 'undefined') {
            console.warn('[FOG] Vanta.js not loaded yet, retrying in 500ms...');
            setTimeout(() => applyVantaFog(fogType), 500);
            return;
        }
        console.log(`[FOG] Initializing Vanta instance for ${fogType} (Softened)...`);
        vantaFogInstance = VANTA.FOG(Object.assign({
            el: "#fogContainer",
            mouseControls: true,
            touchControls: true,
            gyroControls: false,
            minHeight: 200.00,
            minWidth: 200.00
        }, vantaConfig));
    }
}

function checkAndActivateFog(rawMetar) {
    if (!rawMetar) return;
    const metar = rawMetar.toUpperCase();
    console.log(`[FOG] Checking weather for: ${metar.substring(0, 50)}...`);
    const hasFG = /\bFG\b/i.test(metar);
    const hasHZ = /\bHZ\b/i.test(metar);
    const hasBR = /\bBR\b/i.test(metar);
    console.log(`[FOG] Regex results -> FG: ${hasFG}, HZ: ${hasHZ}, BR: ${hasBR}`);
    if (hasFG) console.log("[FOG] Match Found: FG (Fog)");
    if (hasHZ) console.log("[FOG] Match Found: HZ (Haze)");
    if (hasBR) console.log("[FOG] Match Found: BR (Mist)");

    const fogIndicator = document.getElementById('fogIndicator');
    const fogIndicatorText = document.getElementById('fogIndicatorText');
    
    if (!fogContainer || !fogIndicator) {
        console.warn('[FOG] Container or indicator element not found.');
        return;
    }
    
    let fogType = null;
    let indicatorClass = '';
    let icon = '';
    let text = '';
    
    if (hasFG) {
        fogType = 'FG';
        indicatorClass = 'fog-fg';
        icon = '🌫️';
        text = 'FOG - VISIBILITY REDUCED';
    } else if (hasHZ) {
        fogType = 'HZ';
        indicatorClass = 'fog-hz';
        icon = '😶‍🌫️';
        text = 'HAZE - SMOKE/DUST';
    } else if (hasBR) {
        fogType = 'BR';
        indicatorClass = 'fog-br';
        icon = '💨';
        text = 'MIST - LIGHT FOG';
    }
    
    // Jika tidak ada kabut, matikan efek
    if (!fogType) {
        if (fogState.isActive) {
            console.log('[FOG] Weather clear, deactivating...');
            deactivateFog();
        }
        return;
    }
    
    // Jika tipe sama dan sudah aktif, tidak perlu update berat
    if (fogState.isActive && fogState.currentType === fogType) {
        return;
    }
    
    console.log(`[FOG] Detected ${fogType}. Activating Vanta...`);
    fogState.isActive = true;
    fogState.currentType = fogType;
    fogState.lastWeather = metar;
    
    // Reset and show elements
    fogContainer.className = 'fog-container active';
    fogIndicator.className = 'fog-indicator active';
    
    // Switch Vanta Fog Config
    applyVantaFog(fogType);
    
    // Badge update
    fogIndicator.classList.add(indicatorClass);
    const iconEl = fogIndicator.querySelector('.fog-indicator-icon');
    if (iconEl) iconEl.textContent = icon;
    if (fogIndicatorText) fogIndicatorText.textContent = text;
}

/**
 * Matikan efek kabut dengan transisi halus
 */
function deactivateFog() {
    if (!fogState.isActive) return;
    
    const fogContainer = document.getElementById('fogContainer');
    const fogIndicator = document.getElementById('fogIndicator');
    
    if (fogContainer) fogContainer.classList.remove('active');
    if (fogIndicator) fogIndicator.classList.remove('active');
    
    // Destroy Vanta instance after fade-out transition (2s)
    setTimeout(() => {
        if (fogContainer) {
            fogContainer.className = 'fog-container';
            if (vantaFogInstance) {
                console.log('[FOG] Destroying Vanta instance to save resources.');
                vantaFogInstance.destroy();
                vantaFogInstance = null;
            }
        }
        if (fogIndicator) fogIndicator.className = 'fog-indicator';
    }, 2000); 
    
    fogState.isActive = false;
    fogState.currentType = null;
}

/**
 * Update fog effect saat data METAR berubah
 */
function updateFogEffect(data) {
    const rawMetar = data.raw || data.metar || '';
    checkAndActivateFog(rawMetar);
}

// Global expose
window.updateFogEffect = updateFogEffect;
window.checkAndActivateFog = checkAndActivateFog;