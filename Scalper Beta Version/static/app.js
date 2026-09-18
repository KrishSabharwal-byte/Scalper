/**
 * Slicer Nifty – Multi-Slot Scalper Cockpit Client v3.0
 * - Connects to /api/stream SSE for live state
 * - Renders 3-slot tab switcher
 * - Renders ladder with slice labels (A, B, C...)
 * - Renders open slices with Points + ₹ P&L
 * - Renders closed trade history
 * - Run/Stop Slicer against /api/runs/start and /runs/{id}/stop
 * - Auto Spot + Option LTP fetch from Angel One
 */

let currentState = null;
let eventSource = null;
let currentSelectedSide = 'CE';
let activeSlotId = 'run01';
let currentInstrument = 'NIFTY';

const INSTRUMENT_CONFIG = {
  NIFTY: {
    name: 'NIFTY',
    symbolPrefix: 'NIFTY',
    strikeStep: 50,
    lotSize: 65,
    defaultSpot: 24200,
    defaultRangePoints: 40,
    defaultSlicerCount: 5,
    defaultRangeHigh: 140,
    defaultRangeLow: 100,
    defaultStep: 8,
    defaultProfitPoint: 8,
    defaultLossPoint: 8,
    spotLabel: 'Nifty Spot Index Price',
    strikeLabel: 'Calculated Nearest 50 Strike:',
    headerTitle: 'Spot & Nearest 50 Strike',
  },
  SENSEX: {
    name: 'SENSEX',
    symbolPrefix: 'SENSEX',
    strikeStep: 100,
    lotSize: 20,
    defaultSpot: 81000,
    defaultRangePoints: 150,
    defaultSlicerCount: 5,
    defaultRangeHigh: 250,
    defaultRangeLow: 100,
    defaultStep: 30,
    defaultProfitPoint: 30,
    defaultLossPoint: 30,
    spotLabel: 'Sensex Spot Index Price',
    strikeLabel: 'Calculated Nearest 100 Strike:',
    headerTitle: 'Spot & Nearest 100 Strike',
  },
};

const DEFAULT_INSTRUMENT_PARAMS = {
  NIFTY: {
    rangePoints: 40,
    slicerCount: 5,
    rangeHigh: 140,
    rangeLow: 100,
    sliceSize: 8,
    profitPoint: 8,
    lossPoint: 8,
    qtyPerSlice: 1,
    optionType: 'CE',
    spot: 24200,
    cutoffTime: '15:22',
  },
  SENSEX: {
    rangePoints: 150,
    slicerCount: 5,
    rangeHigh: 250,
    rangeLow: 100,
    sliceSize: 30,
    profitPoint: 30,
    lossPoint: 30,
    qtyPerSlice: 1,
    optionType: 'CE',
    spot: 81000,
    cutoffTime: '15:22',
  },
};

let savedInstrumentParams = JSON.parse(JSON.stringify(DEFAULT_INSTRUMENT_PARAMS));
try {
  const stored = localStorage.getItem('slicer_saved_params_v3');
  if (stored) {
    const parsed = JSON.parse(stored);
    if (parsed.NIFTY) savedInstrumentParams.NIFTY = { ...DEFAULT_INSTRUMENT_PARAMS.NIFTY, ...parsed.NIFTY };
    if (parsed.SENSEX) savedInstrumentParams.SENSEX = { ...DEFAULT_INSTRUMENT_PARAMS.SENSEX, ...parsed.SENSEX };
  }
} catch (e) {
  console.warn('Note reading stored parameters:', e);
}

// ── Authentication & Session Management (Per-Tab Scoped via sessionStorage) ────────────────────────
const AUTH_TOKEN_KEY = 'slicer_jwt_token';
const AUTH_CLIENT_ID_KEY = 'slicer_client_id';

function getAuthToken() {
  try {
    return sessionStorage.getItem(AUTH_TOKEN_KEY) || '';
  } catch (e) {
    return '';
  }
}

function setAuthToken(token, clientId) {
  try {
    sessionStorage.setItem(AUTH_TOKEN_KEY, token);
    if (clientId) sessionStorage.setItem(AUTH_CLIENT_ID_KEY, clientId);
  } catch (e) { }
  // Clean up any stale localStorage tokens to prevent cross-tab leakage
  try {
    localStorage.removeItem(AUTH_TOKEN_KEY);
    localStorage.removeItem(AUTH_CLIENT_ID_KEY);
  } catch (e) { }
  updateUserBadge();
}

function removeAuthToken() {
  try {
    sessionStorage.removeItem(AUTH_TOKEN_KEY);
    sessionStorage.removeItem(AUTH_CLIENT_ID_KEY);
  } catch (e) { }
  try {
    localStorage.removeItem(AUTH_TOKEN_KEY);
    localStorage.removeItem(AUTH_CLIENT_ID_KEY);
  } catch (e) { }
  updateUserBadge();
}

function getStoredClientId() {
  try {
    return sessionStorage.getItem(AUTH_CLIENT_ID_KEY) || '';
  } catch (e) {
    return '';
  }
}

function updateUserBadge() {
  const badge = document.getElementById('userSessionBadge');
  const userLabel = document.getElementById('userBadgeClientId');
  const signInBtn = document.getElementById('signInHeaderBtn');
  const token = getAuthToken();
  const clientId = getStoredClientId();
  if (token && clientId && badge && userLabel) {
    badge.style.display = 'inline-flex';
    userLabel.innerText = clientId;
    userLabel.style.fontWeight = '800';
    if (signInBtn) signInBtn.style.display = 'none';
  } else {
    if (badge) badge.style.display = 'none';
    if (signInBtn) signInBtn.style.display = 'inline-flex';
  }
}

// ── Trading Mode State & Safety Handlers ─────────────────────────────────
let currentClientTradingMode = 'paper';
let currentLedgerModeFilter = 'all';

function updateTradingModeUI(mode) {
  currentClientTradingMode = (mode || 'paper').toLowerCase();
  const banner = document.getElementById('topModeBanner');
  const beacon = document.getElementById('modeBannerBeacon');
  const title = document.getElementById('modeBannerTitle');
  const sub = document.getElementById('modeBannerSub');
  const btn = document.getElementById('btnToggleTradingMode');
  const hudPill = document.getElementById('hudModePill');
  const hudPillText = document.getElementById('hudModePillText');

  if (currentClientTradingMode === 'live') {
    if (banner) banner.className = 'top-mode-banner mode-banner-live';
    if (beacon) beacon.innerText = '🔴';
    if (title) title.innerText = 'LIVE TRADING ACTIVE — REAL MONEY AT RISK';
    if (sub) sub.innerText = '(Angel One SmartAPI Live Execution · Fail-Closed Active)';
    if (btn) {
      btn.innerText = 'Switch to PAPER (Simulation)';
      btn.className = 'btn-mode-toggle to-paper';
    }
    if (hudPill) hudPill.className = 'hud-mode-pill live';
    if (hudPillText) hudPillText.innerText = '🔴 LIVE (REAL)';
  } else {
    if (banner) banner.className = 'top-mode-banner mode-banner-paper';
    if (beacon) beacon.innerText = '🟢';
    if (title) title.innerText = 'PAPER TRADING MODE — Zero-Risk Simulation';
    if (sub) sub.innerText = '(Market Data Active · Orders Simulated with SmartAPI LTP · No Real Capital at Risk)';
    if (btn) {
      btn.innerText = 'Switch to LIVE (Real Money)';
      btn.className = 'btn-mode-toggle to-live';
    }
    if (hudPill) hudPill.className = 'hud-mode-pill paper';
    if (hudPillText) hudPillText.innerText = '🟢 PAPER MODE';
  }
}

function handleModeSwitchClick() {
  if (currentClientTradingMode === 'paper') {
    promptSwitchToLive();
  } else {
    executeSwitchToPaper();
  }
}

function promptSwitchToLive() {
  const modal = document.getElementById('liveConfirmModal');
  const input = document.getElementById('liveConfirmInput');
  const submitBtn = document.getElementById('btnSubmitLiveConfirm');
  const errorPill = document.getElementById('liveModalError');

  if (input) input.value = '';
  if (submitBtn) { submitBtn.disabled = true; submitBtn.innerText = '🚨 Confirm Switch to LIVE'; }
  if (errorPill) { errorPill.style.display = 'none'; errorPill.innerText = ''; }
  if (modal) modal.style.display = 'flex';
  if (input) setTimeout(() => input.focus(), 50);
}

function closeLiveConfirmModal() {
  const modal = document.getElementById('liveConfirmModal');
  if (modal) modal.style.display = 'none';
}

function onLiveConfirmInputChange(val) {
  const submitBtn = document.getElementById('btnSubmitLiveConfirm');
  if (!submitBtn) return;
  const isExact = (val || '').trim() === 'LIVE';
  submitBtn.disabled = !isExact;
}

async function executeSwitchToLive() {
  const submitBtn = document.getElementById('btnSubmitLiveConfirm');
  const errorPill = document.getElementById('liveModalError');
  if (submitBtn) { submitBtn.disabled = true; submitBtn.innerText = 'Activating Live...'; }

  try {
    const res = await apiFetch('/api/client/trading_mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'live' }),
    });
    const data = await res.json();
    if (data.status === 'success') {
      updateTradingModeUI('live');
      closeLiveConfirmModal();
      if (currentState) {
        currentState.trading_mode = 'live';
      }
    } else {
      if (errorPill) {
        errorPill.innerText = data.error || data.message || 'Failed to activate live mode.';
        errorPill.style.display = 'block';
      }
    }
  } catch (e) {
    if (errorPill) {
      errorPill.innerText = 'Network error switching to live mode: ' + e;
      errorPill.style.display = 'block';
    }
  } finally {
    if (submitBtn) { submitBtn.innerText = '🚨 Confirm Switch to LIVE'; submitBtn.disabled = false; }
  }
}

async function executeSwitchToPaper() {
  const btn = document.getElementById('btnToggleTradingMode');
  const originalText = btn ? btn.innerText : '';
  if (btn) {
    btn.disabled = true;
    btn.innerText = 'Switching to Paper...';
  }
  try {
    const res = await apiFetch('/api/client/trading_mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'paper' }),
    });
    const data = await res.json();
    if (data.status === 'success') {
      currentClientTradingMode = 'paper';
      updateTradingModeUI('paper');
      if (currentState) {
        currentState.trading_mode = 'paper';
      }
    } else {
      alert(data.error || data.message || 'Failed to switch to paper mode');
    }
  } catch (e) {
    console.error('Error switching to paper mode:', e);
    alert('Network error switching to paper mode: ' + e);
  } finally {
    if (btn) {
      btn.disabled = false;
    }
  }
}

function setLedgerModeFilter(mode) {
  currentLedgerModeFilter = (mode || 'all').toLowerCase();
  document.querySelectorAll('.btn-filter-mode').forEach(btn => btn.classList.remove('active'));
  if (currentLedgerModeFilter === 'paper') document.getElementById('btnFilterPaper')?.classList.add('active');
  else if (currentLedgerModeFilter === 'live') document.getElementById('btnFilterLive')?.classList.add('active');
  else document.getElementById('btnFilterAll')?.classList.add('active');

  if (currentState) {
    renderState(currentState);
  }
}

function showLoginModal(errorMsg = '') {
  removeAuthToken();
  if (eventSource) {
    try { eventSource.close(); } catch (e) { }
    eventSource = null;
  }
  const modal = document.getElementById('loginModal');
  const errorEl = document.getElementById('loginErrorMsg');
  if (errorEl) {
    if (errorMsg) {
      errorEl.innerText = errorMsg;
      errorEl.style.display = 'block';
    } else {
      errorEl.innerText = '';
      errorEl.style.display = 'none';
    }
  }
  if (modal) {
    modal.style.display = 'flex';
    const idInput = document.getElementById('loginClientId');
    const pwInput = document.getElementById('loginPassword');
    if (pwInput) pwInput.value = '';
    if (idInput) {
      idInput.value = '';
      idInput.focus();
    }
  }
}

function hideLoginModal() {
  const modal = document.getElementById('loginModal');
  if (modal) modal.style.display = 'none';
}

async function handleLoginSubmit(event) {
  if (event) event.preventDefault();
  const idInput = document.getElementById('loginClientId');
  const pwInput = document.getElementById('loginPassword');
  const submitBtn = document.getElementById('loginSubmitBtn');
  const errorEl = document.getElementById('loginErrorMsg');

  const clientId = (idInput ? idInput.value : '').trim();
  const password = (pwInput ? pwInput.value : '');

  if (!clientId || !password) {
    if (errorEl) {
      errorEl.innerText = 'Please enter both Client ID and Password.';
      errorEl.style.display = 'block';
    }
    return;
  }

  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<span>Authenticating...</span>';
  }

  try {
    const res = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: clientId, password: password }),
    });
    const data = await res.json();
    if (res.ok && data.access_token) {
      setAuthToken(data.access_token, data.client_id || clientId);
      hideLoginModal();
      if (pwInput) pwInput.value = '';
      connectSSE();
      fetchInitialState();
      refreshAstroStatus();
      const savedArmed = getPersistedAstroArmed();
      if (savedArmed !== null) {
        toggleAstroAutoExecution(savedArmed);
      }
    } else {
      showLoginModal(data.detail || data.error || 'Invalid credentials. Please try again.');
    }
  } catch (err) {
    showLoginModal('Network error connecting to authentication server.');
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.innerHTML = `<span>Authenticate &amp; Access Cockpit</span>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="9 18 15 12 9 6"></polyline>
        </svg>`;
    }
  }
}

async function handleLogout() {
  const token = getAuthToken();
  if (token) {
    try {
      await fetch('/auth/logout', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
      });
    } catch (e) { }
  }
  removeAuthToken();
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  currentState = null;
  const idInput = document.getElementById('loginClientId');
  const pwInput = document.getElementById('loginPassword');
  if (idInput) idInput.value = '';
  if (pwInput) pwInput.value = '';
  showLoginModal();
}

async function apiFetch(url, options = {}) {
  const token = getAuthToken();
  const headers = options.headers || {};
  if (token && !(headers instanceof Headers)) {
    headers['Authorization'] = `Bearer ${token}`;
  } else if (token && headers instanceof Headers) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  options.headers = headers;
  options.credentials = 'include';

  const res = await fetch(url, options);
  if (res.status === 401) {
    removeAuthToken();
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    showLoginModal('Session expired. Please log in again.');
    throw new Error('Unauthorized');
  }
  return res;
}

function connectSSE() {
  const token = getAuthToken();
  if (!token) {
    showLoginModal();
    return;
  }

  if (eventSource) {
    try { eventSource.close(); } catch (e) { }
    eventSource = null;
  }

  const sseUrl = `/api/stream?token=${encodeURIComponent(token)}`;
  eventSource = new EventSource(sseUrl);

  eventSource.onopen = () => {
    if (connectionStatus) {
      connectionStatus.innerHTML = '<span class="pulse-indicator"></span><span class="status-text">Connected</span>';
      connectionStatus.style.borderColor = 'rgba(16, 185, 129, 0.3)';
    }
  };

  eventSource.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data && data.client_id) {
        if (data.client_id !== getStoredClientId()) {
          setAuthToken(token, data.client_id);
        }
        renderState(data);
      }
    } catch (err) {
      console.warn('Error parsing SSE data:', err);
    }
  };

  eventSource.onerror = (err) => {
    if (connectionStatus) {
      connectionStatus.innerHTML = '<span class="pulse-indicator" style="background:#ef4444;box-shadow:none;"></span><span class="status-text" style="color:#ef4444;">Reconnecting...</span>';
      connectionStatus.style.borderColor = 'rgba(239, 68, 68, 0.4)';
    }
  };
}

async function fetchInitialState() {
  try {
    const [resState, resHist] = await Promise.all([
      apiFetch('/api/state'),
      apiFetch('/api/history?limit=300').catch(() => null)
    ]);
    if (resState && resState.ok) {
      const data = await resState.json();
      if (resHist && resHist.ok) {
        try {
          const histData = await resHist.json();
          if (Array.isArray(histData.trades) && histData.trades.length > 0) {
            const map = new Map();
            histData.trades.forEach(t => { if (t && (t.trade_id || t.entry_time)) map.set(t.trade_id || `${t.run_id}_${t.entry_time}`, t); });
            if (Array.isArray(data.history)) {
              data.history.forEach(t => { if (t && (t.trade_id || t.entry_time)) map.set(t.trade_id || `${t.run_id}_${t.entry_time}`, t); });
            }
            data.history = Array.from(map.values());
          }
        } catch (he) { }
      }
      renderState(data);
    }
  } catch (e) { }
}

// ── Helper ─────────────────────────────────────────────────────────────────
function calculateNearestStrike(spotPrice, instrument = currentInstrument) {
  const cfg = INSTRUMENT_CONFIG[instrument] || INSTRUMENT_CONFIG.NIFTY;
  let p = parseFloat(spotPrice);
  if (isNaN(p) || p <= 0) p = cfg.defaultSpot;
  const isSensex = instrument.toUpperCase() === 'SENSEX';
  if (isSensex && p < 50000) p = cfg.defaultSpot;
  if (!isSensex && p > 50000) p = cfg.defaultSpot;
  const step = cfg.strikeStep;
  return Math.round(p / step) * step;
}

function calculateNearest50Strike(spotPrice) {
  return calculateNearestStrike(spotPrice, 'NIFTY');
}

function levelForPrice(price, rangeHigh = 150, rangeLow = 100, step = 10) {
  const p = parseFloat(price);
  const rH = parseFloat(rangeHigh);
  const rL = parseFloat(rangeLow);
  const s = parseFloat(step);
  if (isNaN(p) || isNaN(rH) || isNaN(s) || s <= 0 || p > rH + 1e-4) return null;
  const k = Math.floor((rH - p + 1e-7) / s);
  const lvl = rH - (k * s);
  if (lvl < rL - 1e-4) return null;
  return lvl;
}

function fmt(n, dp = 2) {
  if (n === null || n === undefined || isNaN(n)) return '--';
  return parseFloat(n).toFixed(dp);
}

function fmtPnl(n) {
  const v = parseFloat(n) || 0;
  const sign = v >= 0 ? '+' : '-';
  return `${sign}₹${Math.abs(v).toFixed(2)}`;
}

function formatISTDateTime(dateStr) {
  if (!dateStr) return '--';
  try {
    let str = typeof dateStr === 'string' ? dateStr.trim() : String(dateStr);
    if (!str.includes('Z') && !/[+-]\d{2}:\d{2}$/.test(str) && str.includes('T')) {
      str = str + '+05:30';
    }
    const d = new Date(str);
    if (isNaN(d.getTime())) {
      if (typeof dateStr === 'string') {
        return dateStr.replace('T', ' ').split('.')[0] || '--';
      }
      return '--';
    }
    return new Intl.DateTimeFormat('en-IN', {
      timeZone: 'Asia/Kolkata',
      day: '2-digit',
      month: 'short',
      year: 'numeric',
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }).format(d);
  } catch (e) {
    if (typeof dateStr === 'string') {
      return dateStr.replace('T', ' ').split('.')[0] || '--';
    }
    return '--';
  }
}

function formatISTTime(dateStr) {
  return formatISTDateTime(dateStr);
}

function isTodayIST(dateStr) {
  if (!dateStr) return false;
  try {
    let str = typeof dateStr === 'string' ? dateStr.trim() : String(dateStr);
    if (!str.includes('Z') && !/[+-]\d{2}:\d{2}$/.test(str)) {
      str = str.replace(' ', 'T') + '+05:30';
    }
    const d = new Date(str);
    if (isNaN(d.getTime())) return false;
    const tradeDateIST = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata' }).format(d);
    const todayIST = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata' }).format(new Date());
    return tradeDateIST === todayIST;
  } catch (e) {
    return false;
  }
}

// DOM Elements
const symbolText = document.getElementById('symbolText');
const headerAccentTitle = document.getElementById('headerAccentTitle');
const instrumentSelect = document.getElementById('instrumentSelect');
const spotCardHeaderTitle = document.getElementById('spotCardHeaderTitle');
const spotInputLabel = document.getElementById('spotInputLabel');
const resStrikeLabel = document.getElementById('resStrikeLabel');

const headerSpotLtp = document.getElementById('headerSpotLtp');
const headerActiveStrike = document.getElementById('headerActiveStrike');
const headerLtp = document.getElementById('headerLtp');
const headerTotalPnl = document.getElementById('headerTotalPnl');
const connectionStatus = document.getElementById('connectionStatus');

const metricLiveLtp = document.getElementById('metricLiveLtp');
const metricLiveLtpSub = document.getElementById('metricLiveLtpSub');
const resOptLtpVal = document.getElementById('resOptLtpVal');
const spotInput = document.getElementById('spotInput');
const optionTypeSelect = document.getElementById('optionTypeSelect');
const optLtpInput = document.getElementById('optLtpInput');
const strikeBadge = document.getElementById('strikeBadge');
const strikeSelect = document.getElementById('strikeSelect');
const strikeSourceBadge = document.getElementById('strikeSourceBadge');
const resStrikeVal = document.getElementById('resStrikeVal');
const resContractVal = document.getElementById('resContractVal');

let selectedStrikeSource = 'auto'; // 'auto' | 'manual'
let manualSelectedStrike = null;
const rangePointsInput = document.getElementById('rangePoints');
const slicerCountInput = document.getElementById('slicerCount');
const computedStepVal = document.getElementById('computedStepVal');
const computedStepFormula = document.getElementById('computedStepFormula');
const rangeHighInput = document.getElementById('rangeHigh');
const rangeLowInput = document.getElementById('rangeLow');
const sliceSizeInput = document.getElementById('sliceSize');
const profitPointInput = document.getElementById('profitPoint');
const lossPointInput = document.getElementById('lossPoint');
const qtyPerSliceInput = document.getElementById('qtyPerSlice');
const eodCutoffSelect = document.getElementById('eodCutoffSelect');
const eodCutoffInput = document.getElementById('eodCutoffInput');
const eodCutoffBadge = document.getElementById('eodCutoffBadge');
const spanBadge = document.getElementById('spanBadge');

// Astro elements
const astroStatusBadge = document.getElementById('astroStatusBadge');
const astroCsvFileInput = document.getElementById('astroCsvFileInput');
const astroUploadFeedback = document.getElementById('astroUploadFeedback');
const astroActiveFileCard = document.getElementById('astroActiveFileCard');
const astroActiveFileName = document.getElementById('astroActiveFileName');
const astroActiveFileMeta = document.getElementById('astroActiveFileMeta');
const btnDeleteAstroFile = document.getElementById('btnDeleteAstroFile');
const astroClusterTime = document.getElementById('astroClusterTime');
const astroConsensusBanner = document.getElementById('astroConsensusBanner');
const astroConsensusIcon = document.getElementById('astroConsensusIcon');
const astroConsensusLabel = document.getElementById('astroConsensusLabel');
const filterHoursPill = document.getElementById('filterHoursPill');
const filterTradePill = document.getElementById('filterTradePill');
const astroAutoToggle = document.getElementById('astroAutoToggle');
const astroSlotTag = document.getElementById('astroSlotTag');

const metricGrossPnl = document.getElementById('metricGrossPnl');
const metricGrossClosedCount = document.getElementById('metricGrossClosedCount');
const metricRealizedPnl = document.getElementById('metricRealizedPnl');
const metricClosedCount = document.getElementById('metricClosedCount');
const metricUnrealizedPnl = document.getElementById('metricUnrealizedPnl');
const metricOpenCount = document.getElementById('metricOpenCount');
const metricSpanStep = document.getElementById('metricSpanStep');
const metricTotalLevels = document.getElementById('metricTotalLevels');
const metricLossTrigger = document.getElementById('metricLossTrigger');
const metricLossDetail = document.getElementById('metricLossDetail');
const ladderContainer = document.getElementById('ladderContainer');
const ladderNote = document.getElementById('ladderNote');
const openSlicesBadge = document.getElementById('openSlicesBadge');
const openSlicesTableBody = document.getElementById('openSlicesTableBody');
const closedSlicesBadge = document.getElementById('closedSlicesBadge');
const closedSlicesTableBody = document.getElementById('closedSlicesTableBody');
const slotTabBar = document.getElementById('slotTabBar');

// ── Trade Instrument Helper ────────────────────────────────────────────────
function getTradeInstrument(rec) {
  if (!rec) return 'NIFTY';
  if (rec.instrument) return rec.instrument.toUpperCase();
  if (rec.symbol) return rec.symbol.toUpperCase();
  if (rec.contract_symbol) {
    const sym = rec.contract_symbol.toUpperCase();
    if (sym.includes('SENSEX')) return 'SENSEX';
    if (sym.includes('NIFTY')) return 'NIFTY';
  }
  return 'NIFTY';
}

// ── Parameter Management per Instrument ───────────────────────────────────
function saveCurrentInputParams() {
  if (currentState?.active_run?.is_active) return;
  const inst = (currentInstrument || 'NIFTY').toUpperCase();
  if (!savedInstrumentParams[inst]) {
    savedInstrumentParams[inst] = { ...(DEFAULT_INSTRUMENT_PARAMS[inst] || DEFAULT_INSTRUMENT_PARAMS.NIFTY) };
  }
  const p = savedInstrumentParams[inst];
  if (rangePointsInput && !isNaN(parseFloat(rangePointsInput.value))) p.rangePoints = parseFloat(rangePointsInput.value);
  if (slicerCountInput && !isNaN(parseInt(slicerCountInput.value))) p.slicerCount = parseInt(slicerCountInput.value);
  if (rangeHighInput && !isNaN(parseFloat(rangeHighInput.value))) p.rangeHigh = parseFloat(rangeHighInput.value);
  if (rangeLowInput && !isNaN(parseFloat(rangeLowInput.value))) p.rangeLow = parseFloat(rangeLowInput.value);
  if (sliceSizeInput && !isNaN(parseFloat(sliceSizeInput.value))) p.sliceSize = parseFloat(sliceSizeInput.value);
  if (profitPointInput && !isNaN(parseFloat(profitPointInput.value))) p.profitPoint = parseFloat(profitPointInput.value);
  if (lossPointInput && !isNaN(parseFloat(lossPointInput.value))) p.lossPoint = parseFloat(lossPointInput.value);
  if (qtyPerSliceInput && !isNaN(parseInt(qtyPerSliceInput.value))) p.qtyPerSlice = parseInt(qtyPerSliceInput.value);
  if (eodCutoffInput && eodCutoffInput.value) p.cutoffTime = eodCutoffInput.value;
  if (optionTypeSelect && optionTypeSelect.value) p.optionType = optionTypeSelect.value;
  if (spotInput && !isNaN(parseFloat(spotInput.value))) p.spot = parseFloat(spotInput.value);
  p.selectedStrikeSource = selectedStrikeSource;
  p.manualSelectedStrike = manualSelectedStrike;

  try {
    localStorage.setItem('slicer_saved_params_v3', JSON.stringify(savedInstrumentParams));
  } catch (e) { }
}

function loadInstrumentParams(inst) {
  const instKey = (inst || 'NIFTY').toUpperCase();
  const fallback = DEFAULT_INSTRUMENT_PARAMS[instKey] || DEFAULT_INSTRUMENT_PARAMS.NIFTY;
  const p = savedInstrumentParams[instKey] || fallback;
  if (rangePointsInput) rangePointsInput.value = p.rangePoints != null ? p.rangePoints : (fallback.rangePoints || (instKey === 'SENSEX' ? 150 : 40));
  if (slicerCountInput) slicerCountInput.value = p.slicerCount != null ? p.slicerCount : (fallback.slicerCount || 5);
  if (rangeHighInput) rangeHighInput.value = p.rangeHigh != null ? p.rangeHigh : fallback.rangeHigh;
  if (rangeLowInput) rangeLowInput.value = p.rangeLow != null ? p.rangeLow : fallback.rangeLow;
  if (sliceSizeInput) sliceSizeInput.value = p.sliceSize != null ? p.sliceSize : fallback.sliceSize;
  if (profitPointInput) profitPointInput.value = p.profitPoint != null ? p.profitPoint : fallback.profitPoint;
  if (lossPointInput) lossPointInput.value = p.lossPoint != null ? p.lossPoint : fallback.lossPoint;
  if (qtyPerSliceInput) qtyPerSliceInput.value = p.qtyPerSlice != null ? p.qtyPerSlice : fallback.qtyPerSlice;
  updateEodCutoffDisplay(p.cutoffTime || fallback.cutoffTime || '15:22', false);
  if (optionTypeSelect && p.optionType) optionTypeSelect.value = p.optionType;
  if (spotInput) {
    const spotVal = parseFloat(spotInput.value);
    const isSensex = instKey === 'SENSEX';
    if (isNaN(spotVal) || (isSensex && spotVal < 50000) || (!isSensex && spotVal > 50000)) {
      spotInput.value = p.spot || fallback.spot || (isSensex ? 81000 : 24200);
    }
  }
  selectedStrikeSource = p.selectedStrikeSource || 'auto';
  manualSelectedStrike = p.manualSelectedStrike != null ? parseInt(p.manualSelectedStrike) : null;
  updateStrikeSourceBadge();

  currentSelectedSide = p.optionType || 'CE';
  updateSpanBadge();
  updateSpotPreview();
}

function getActiveSlotRun() {
  const runs = currentState && Array.isArray(currentState.runs) ? currentState.runs : [];
  const target = runs.find(r => r.run_id === activeSlotId);
  return target || currentState?.active_run || {};
}

function setStrategyControlsLock(isRunning) {
  const inputsToLock = [
    rangePointsInput,
    slicerCountInput,
    rangeHighInput,
    rangeLowInput,
    sliceSizeInput,
    profitPointInput,
    lossPointInput,
    qtyPerSliceInput,
    instrumentSelect,
    optionTypeSelect,
    spotInput,
    optLtpInput,
    strikeSelect,
  ];

  inputsToLock.forEach(input => {
    if (input) {
      input.disabled = isRunning;
      const wrapper = input.closest('.input-wrapper');
      if (wrapper) {
        if (isRunning) {
          wrapper.classList.add('locked');
        } else {
          wrapper.classList.remove('locked');
        }
      }
    }
  });

  const btnAutoSpot = document.getElementById('btnAutoSpot');
  const btnAutoOptLtp = document.getElementById('btnAutoOptLtp');
  if (btnAutoSpot) btnAutoSpot.disabled = isRunning;
  if (btnAutoOptLtp) btnAutoOptLtp.disabled = isRunning;

  const settingsCard = document.querySelector('.settings-card');
  const spanBadge = document.getElementById('spanBadge');
  if (settingsCard) {
    if (isRunning) settingsCard.classList.add('locked-card');
    else settingsCard.classList.remove('locked-card');
  }
  if (spanBadge) {
    if (isRunning) {
      spanBadge.innerHTML = '🔒 LOCKED (Running)';
      spanBadge.style.background = 'rgba(245, 158, 11, 0.2)';
      spanBadge.style.color = 'var(--accent-amber)';
      spanBadge.style.borderColor = 'rgba(245, 158, 11, 0.5)';
    } else {
      updateSpanBadge();
      spanBadge.style.background = '';
      spanBadge.style.color = '';
      spanBadge.style.borderColor = '';
    }
  }
}

// ── Instrument Switching ───────────────────────────────────────────────────
async function onInstrumentChange(inst) {
  const currentSlot = getActiveSlotRun();
  const isRunning = Boolean(currentSlot?.is_active);
  const activeSlices = Array.isArray(currentSlot?.active_slices) ? currentSlot.active_slices : [];
  if (isRunning && activeSlices.length > 0) {
    alert(`Slot ${activeSlotId} has open positions. Cannot change instrument while ladder has open positions.`);
    if (instrumentSelect) instrumentSelect.value = currentInstrument;
    return;
  }

  // Save current parameters for old instrument
  saveCurrentInputParams();

  currentInstrument = (inst || 'NIFTY').toUpperCase();
  const cfg = INSTRUMENT_CONFIG[currentInstrument] || INSTRUMENT_CONFIG.NIFTY;

  // Stay on the currently selected slot tab (never redirect to another slot)
  const currentSlotId = activeSlotId || 'run01';
  if (currentSlot) {
    currentSlot.instrument_name = currentInstrument;
    if (currentSlot.config) {
      currentSlot.config.instrument_name = currentInstrument;
    }
  }

  // Load the target instrument's own saved parameters (never bleed across instruments)
  loadInstrumentParams(currentInstrument);
  updateStrikeSourceBadge();

  if (headerAccentTitle) headerAccentTitle.innerText = currentInstrument;
  if (spotCardHeaderTitle) spotCardHeaderTitle.innerText = cfg.headerTitle;
  if (spotInputLabel) spotInputLabel.innerText = cfg.spotLabel;
  if (resStrikeLabel) resStrikeLabel.innerText = cfg.strikeLabel;
  if (instrumentSelect) instrumentSelect.value = currentInstrument;

  // Instantly apply cached live spot for this instrument if available in background feed
  const feedSpot = currentState?.angel_feed?.spot_by_inst?.[currentInstrument];
  if (feedSpot && spotInput) {
    const isSensex = currentInstrument === 'SENSEX';
    if ((isSensex && feedSpot >= 50000) || (!isSensex && feedSpot < 50000)) {
      spotInput.value = fmt(feedSpot, 2);
    }
  }
  updateSpotPreview();

  // Optimistically re-render state immediately with the selected instrument on THIS slot
  if (currentState) {
    renderState(currentState);
  }

  // Notify backend to update the instrument on this exact slot
  try {
    await apiFetch(`/api/runs/${currentSlotId}/instrument`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        instrument: currentInstrument,
        strike: manualSelectedStrike,
        strike_source: selectedStrikeSource,
      }),
    });
  } catch (e) {
    console.error('Error updating slot instrument on backend:', e);
  }
}

// ── EOD Cutoff Controls ───────────────────────────────────────────────────
function formatTime12h(timeStr) {
  if (!timeStr || typeof timeStr !== 'string' || !timeStr.includes(':')) return timeStr || '3:22 PM';
  const parts = timeStr.trim().split(':');
  const h = parseInt(parts[0], 10);
  const m = parseInt(parts[1], 10);
  if (isNaN(h) || isNaN(m)) return timeStr;
  const ampm = h >= 12 ? 'PM' : 'AM';
  const h12 = h % 12 || 12;
  return `${h12}:${m < 10 ? '0' : ''}${m} ${ampm}`;
}

function updateEodCutoffDisplay(timeStr, syncToServer = true) {
  if (!timeStr) timeStr = '15:22';
  timeStr = timeStr.trim();
  const timeFormatted = formatTime12h(timeStr);

  if (eodCutoffBadge) {
    eodCutoffBadge.innerText = `Cutoff: ${timeFormatted}`;
  }

  const btnEod = document.getElementById('btnEodSquareoff');
  if (btnEod) {
    btnEod.innerHTML = `⏹ EOD Squareoff (${timeFormatted})`;
  }

  if (eodCutoffInput && eodCutoffInput.value !== timeStr) {
    eodCutoffInput.value = timeStr;
  }

  if (eodCutoffSelect) {
    let matched = false;
    for (let i = 0; i < eodCutoffSelect.options.length; i++) {
      if (eodCutoffSelect.options[i].value === timeStr) {
        eodCutoffSelect.selectedIndex = i;
        matched = true;
        break;
      }
    }
    if (!matched) {
      eodCutoffSelect.value = 'custom';
    }
  }

  saveCurrentInputParams();

  if (syncToServer && activeSlotId) {
    apiFetch(`/runs/${activeSlotId}/cutoff_time`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cutoff_time_ist: timeStr }),
    }).catch(e => console.warn('Could not sync cutoff time:', e));
  }
}

function onEodCutoffSelectChange(val) {
  if (val === 'custom') {
    if (eodCutoffInput) eodCutoffInput.focus();
    return;
  }
  updateEodCutoffDisplay(val, true);
}

function onEodCutoffInputChange(val) {
  if (!val) return;
  updateEodCutoffDisplay(val, true);
}


// ── Span Badge & Dynamic Range Calculation ────────────────────────────────
function updateSpanBadge() {
  const rangePts = parseFloat(rangePointsInput ? rangePointsInput.value : 0) || (currentInstrument === 'SENSEX' ? 150 : 40);
  const slicerCnt = Math.max(1, parseInt(slicerCountInput ? slicerCountInput.value : 5) || 5);
  const step = slicerCnt > 0 ? (rangePts / slicerCnt) : 0;

  if (computedStepVal) {
    computedStepVal.innerText = `${step.toFixed(2)} pts`;
  }
  if (computedStepFormula) {
    computedStepFormula.innerText = `(${rangePts.toFixed(2)} pts / ${slicerCnt} levels)`;
  }

  if (spanBadge) {
    spanBadge.innerText = `Span: ${rangePts.toFixed(2)} pts | Step: ${step.toFixed(2)} pts (${slicerCnt} lvls)`;
    spanBadge.style.background = '';
    spanBadge.style.color = '';
    spanBadge.style.borderColor = '';
    spanBadge.title = '';
  }

  // Derive and synchronize backward-compatible range_high / range_low / slice_interval
  const currentOptLtp = parseFloat(optLtpInput?.value) || (currentInstrument === 'SENSEX' ? 250 : 140);
  if (rangeHighInput) rangeHighInput.value = currentOptLtp.toFixed(2);
  if (rangeLowInput) rangeLowInput.value = Math.max(0, currentOptLtp - rangePts).toFixed(2);
  if (sliceSizeInput) sliceSizeInput.value = step.toFixed(2);
}

// ── Spot Preview ───────────────────────────────────────────────────────────
// ── Spot Preview & Manual Strike Handling ────────────────────────────────
function updateStrikeSourceBadge() {
  if (!strikeSourceBadge) return;
  const isManual = selectedStrikeSource === 'manual';
  strikeSourceBadge.className = 'strike-source-tag ' + (isManual ? 'manual' : 'auto');
  strikeSourceBadge.innerText = isManual ? 'MANUAL' : 'AUTO';
}

async function populateStrikeDropdown(spot, instrument, selectedStrike = null) {
  if (!strikeSelect) return;
  const inst = (instrument || currentInstrument || 'NIFTY').toUpperCase();
  const cfg = INSTRUMENT_CONFIG[inst] || INSTRUMENT_CONFIG.NIFTY;
  let spotVal = spot != null ? parseFloat(spot) : (parseFloat(spotInput?.value) || cfg.defaultSpot);
  const isSensex = inst === 'SENSEX';
  if (isSensex && spotVal < 50000) spotVal = cfg.defaultSpot;
  if (!isSensex && spotVal > 50000) spotVal = cfg.defaultSpot;

  const atmStrike = calculateNearestStrike(spotVal, inst);
  const isManual = selectedStrikeSource === 'manual' && manualSelectedStrike != null;
  const targetStrike = isManual ? manualSelectedStrike : (selectedStrike != null ? parseInt(selectedStrike) : atmStrike);

  // If dropdown already has strikes for this instrument/ATM range, just maintain selection
  const existingOptions = Array.from(strikeSelect.options).map(o => parseInt(o.value));
  if (existingOptions.length >= 10 && existingOptions.includes(atmStrike) && existingOptions.includes(targetStrike)) {
    if (document.activeElement !== strikeSelect) {
      strikeSelect.value = targetStrike;
    }
    return;
  }

  try {
    const res = await apiFetch(`/api/angel/available_strikes?instrument=${encodeURIComponent(inst)}&spot=${spotVal}`);
    const data = await res.json();
    let strikes = data && Array.isArray(data.strikes) ? data.strikes : [];
    if (!strikes.length) {
      const step = isSensex ? 100 : 50;
      strikes = [];
      for (let i = -15; i <= 15; i++) strikes.push(atmStrike + (i * step));
    }
    if (targetStrike && !strikes.includes(targetStrike)) {
      strikes.push(targetStrike);
      strikes.sort((a, b) => a - b);
    }

    let html = '';
    strikes.forEach(s => {
      const isAtm = s === atmStrike;
      html += `<option value="${s}">${s} ${isAtm ? '(ATM)' : ''}</option>`;
    });
    strikeSelect.innerHTML = html;
    strikeSelect.value = targetStrike;
  } catch (e) {
    const step = isSensex ? 100 : 50;
    let strikes = [];
    for (let i = -15; i <= 15; i++) {
      strikes.push(atmStrike + (i * step));
    }
    if (targetStrike && !strikes.includes(targetStrike)) {
      strikes.push(targetStrike);
      strikes.sort((a, b) => a - b);
    }
    let html = '';
    strikes.forEach(s => {
      html += `<option value="${s}">${s} ${s === atmStrike ? '(ATM)' : ''}</option>`;
    });
    strikeSelect.innerHTML = html;
    strikeSelect.value = targetStrike;
  }
}

async function onManualStrikeChange(strikeVal) {
  const currentSlot = getActiveSlotRun();
  const isRunning = Boolean(currentSlot?.is_active);
  const activeSlices = Array.isArray(currentSlot?.active_slices) ? currentSlot.active_slices : [];

  // Guard: If THIS specific run is active and has open slices, changing strike is strictly blocked!
  if (isRunning && activeSlices.length > 0) {
    alert("Cannot change strike while ladder has open positions");
    const currentLockedStrike = currentSlot.locked_strike || currentSlot.strike || currentSlot.config?.strike;
    if (strikeSelect && currentLockedStrike) {
      strikeSelect.value = currentLockedStrike;
    }
    return;
  }

  const chosenStrike = parseInt(strikeVal);
  if (isNaN(chosenStrike) || chosenStrike <= 0) return;

  selectedStrikeSource = 'manual';
  manualSelectedStrike = chosenStrike;
  updateStrikeSourceBadge();
  saveCurrentInputParams();

  // Always notify backend to record the pinned strike and strike_source on this slot
  try {
    await apiFetch(`/api/runs/${activeSlotId}/strike`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ strike: chosenStrike, strike_source: 'manual' }),
    });
  } catch (e) {
    console.error('Error updating strike on backend:', e);
  }

  await fetchOptionLtpForStrike(chosenStrike, 'manual');
}

async function fetchOptionLtpForStrike(strike, source = 'manual') {
  try {
    const inst = currentInstrument;
    const optType = optionTypeSelect ? optionTypeSelect.value : currentSelectedSide;
    const res = await apiFetch('/api/angel/fetch_option', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        run_id: activeSlotId,
        instrument: inst,
        option_type: optType,
        strike: strike,
        strike_source: source
      }),
    });
    const data = await res.json();
    if (data.status === 'success' && data.option_ltp != null) {
      const resolvedStrike = data.strike || strike;
      const contractLabel = data.contract || `${inst} ${resolvedStrike} ${optType}`;
      if (optLtpInput) optLtpInput.value = parseFloat(data.option_ltp).toFixed(2);
      if (resOptLtpVal) resOptLtpVal.innerText = `₹${parseFloat(data.option_ltp).toFixed(2)}`;
      if (metricLiveLtp) metricLiveLtp.innerText = `₹${parseFloat(data.option_ltp).toFixed(2)}`;
      if (headerLtp) headerLtp.innerText = `₹${parseFloat(data.option_ltp).toFixed(2)}`;
      const isRunning = Boolean(currentState?.active_run?.is_active);
      const cfg = INSTRUMENT_CONFIG[inst] || INSTRUMENT_CONFIG.NIFTY;
      let spotVal = parseFloat(spotInput ? spotInput.value : 0) || cfg.defaultSpot;
      const nearestAtm = calculateNearestStrike(spotVal, inst);
      if (resStrikeVal) resStrikeVal.innerText = nearestAtm;
      const srcText = source === 'manual' ? ' [MANUAL]' : ' [AUTO]';
      if (strikeBadge) strikeBadge.innerText = (isRunning ? `LOCKED: ${resolvedStrike}` : `ATM: ${nearestAtm}`) + srcText;
      if (resContractVal) resContractVal.innerText = `Active Contract: ${contractLabel}`;
      if (headerActiveStrike) headerActiveStrike.innerText = resolvedStrike;
      if (symbolText) symbolText.innerText = contractLabel;
      if (strikeSelect) strikeSelect.value = resolvedStrike;
    }
  } catch (e) {
    console.error('Error fetching option LTP for strike:', e);
  }
}

function updateSpotPreview() {
  const cfg = INSTRUMENT_CONFIG[currentInstrument] || INSTRUMENT_CONFIG.NIFTY;
  let spotVal = spotInput ? parseFloat(spotInput.value) : cfg.defaultSpot;
  if (isNaN(spotVal) || spotVal <= 0) spotVal = cfg.defaultSpot;
  const isSensex = currentInstrument.toUpperCase() === 'SENSEX';
  if (isSensex && spotVal < 50000) spotVal = cfg.defaultSpot;
  if (!isSensex && spotVal > 50000) spotVal = cfg.defaultSpot;

  const nearestStrike = calculateNearestStrike(spotVal, currentInstrument);
  const side = optionTypeSelect ? optionTypeSelect.value : currentSelectedSide;
  currentSelectedSide = side;

  const isManual = selectedStrikeSource === 'manual' && manualSelectedStrike != null;
  const strike = isManual ? manualSelectedStrike : nearestStrike;
  const srcTag = isManual ? ' [MANUAL]' : ' [AUTO]';

  if (strikeBadge) strikeBadge.innerText = `ATM: ${nearestStrike}${srcTag}`;
  if (resStrikeVal) resStrikeVal.innerText = nearestStrike;
  if (resContractVal) resContractVal.innerText = `Active Contract: ${currentInstrument} ${strike} ${side}`;

  updateStrikeSourceBadge();
  populateStrikeDropdown(spotVal, currentInstrument, strike);

  if (!currentState || !currentState.active_run || !currentState.active_run.is_active) {
    if (headerActiveStrike) headerActiveStrike.innerText = strike;
    if (symbolText) symbolText.innerText = `${currentInstrument} ${strike} ${side}`;
  }
}

async function selectRaiseSide(side) {
  currentSelectedSide = side.toUpperCase() === 'PUT' || side.toUpperCase() === 'PE' ? 'PE' : 'CE';
  if (optionTypeSelect && optionTypeSelect.value !== currentSelectedSide) {
    optionTypeSelect.value = currentSelectedSide;
  }
  // Switching Option Side (CE/PE) while manual strike is selected re-resolves and re-fetches LTP for that same manual strike!
  if (selectedStrikeSource === 'manual' && manualSelectedStrike) {
    await fetchOptionLtpForStrike(manualSelectedStrike, 'manual');
  } else {
    updateSpotPreview();
  }
}

if (spotInput) spotInput.addEventListener('input', () => { updateSpotPreview(); saveCurrentInputParams(); });
if (rangeHighInput) rangeHighInput.addEventListener('input', () => { updateSpanBadge(); saveCurrentInputParams(); });
if (rangeLowInput) rangeLowInput.addEventListener('input', () => { updateSpanBadge(); saveCurrentInputParams(); });
if (sliceSizeInput) sliceSizeInput.addEventListener('input', () => { updateSpanBadge(); saveCurrentInputParams(); });
if (profitPointInput) profitPointInput.addEventListener('input', saveCurrentInputParams);
if (lossPointInput) lossPointInput.addEventListener('input', saveCurrentInputParams);
if (qtyPerSliceInput) qtyPerSliceInput.addEventListener('input', saveCurrentInputParams);
if (optionTypeSelect) optionTypeSelect.addEventListener('change', () => { updateSpotPreview(); saveCurrentInputParams(); });



// ── Slot Tab Switcher ──────────────────────────────────────────────────────
// ── Canonical Slot Definitions ─────────────────────────────────────────────
const FIXED_SLOT_DEFS = [
  { code: 'N-C', legacy: 'run01', name: 'NIFTY CALL', inst: 'NIFTY', opt: 'CE' },
  { code: 'N-P', legacy: 'run03', name: 'NIFTY PUT', inst: 'NIFTY', opt: 'PE' },
  { code: 'S-C', legacy: 'run02', name: 'SENSEX CALL', inst: 'SENSEX', opt: 'CE' },
  { code: 'S-P', legacy: 'run04', name: 'SENSEX PUT', inst: 'SENSEX', opt: 'PE' },
];

const SLOT_LEGACY_MAP = {
  'N-C': 'run01',
  'S-C': 'run02',
  'N-P': 'run03',
  'S-P': 'run04',
};

const SLOT_CANONICAL_MAP = {
  'run01': 'N-C',
  'run02': 'S-C',
  'run03': 'N-P',
  'run04': 'S-P',
};

function tradeMatchesSlot(t, slotCode) {
  if (!t || !slotCode) return false;
  const canonical = SLOT_CANONICAL_MAP[slotCode] || slotCode;
  const legacy = SLOT_LEGACY_MAP[canonical] || canonical;
  
  if (t.slot_id === canonical || t.slot_code === canonical || t.slot_id === legacy || t.slot_code === legacy) return true;
  if (t.run_id === canonical || t.run_id === legacy) return true;
  
  const inst = (t.instrument || t.symbol || '').toUpperCase();
  const opt = (t.option_type || t.side || '').toUpperCase();
  const targetInst = canonical.startsWith('S') ? 'SENSEX' : 'NIFTY';
  const targetOpt = canonical.endsWith('P') ? 'PE' : 'CE';
  
  if (inst === targetInst && (opt === targetOpt || (opt === 'CALL' && targetOpt === 'CE') || (opt === 'PUT' && targetOpt === 'PE'))) {
    return true;
  }
  
  const csym = (t.contract_symbol || '').toUpperCase();
  if (csym.includes(targetInst) && (csym.includes(` ${targetOpt}`) || csym.endsWith(targetOpt))) {
    return true;
  }
  return false;
}

// ── Slot Tab Switcher ──────────────────────────────────────────────────────
function renderSlotTabs(runs, activeRunId, slotsMatrix) {
  if (!slotTabBar) return;
  const runsArr = Array.isArray(runs) ? runs : [];

  let html = '';
  FIXED_SLOT_DEFS.forEach((def) => {
    const run = runsArr.find(r => r.run_id === def.legacy || r.slot_id === def.code || r.slot_code === def.code) || {};
    const slotData = (slotsMatrix && (slotsMatrix[def.code] || slotsMatrix[def.legacy])) || {};
    const isActive = (activeRunId === def.legacy || activeRunId === def.code);
    const isRunning = Boolean(run.is_active || slotData.is_active);
    const inst = (slotData.instrument || run.instrument_name || run.config?.instrument_name || def.inst).toUpperCase();
    const strike = slotData.strike || run.locked_strike || run.strike || run.config?.strike || '--';
    const optType = slotData.option_type || run.option_type || run.config?.option_type || def.opt;
    const label = `${inst} ${strike} ${optType}`;

    const todayPnl = Number(slotData.today_pnl ?? slotData.today_realized_pnl ?? run.today_realized_pnl ?? 0);
    const overallPnl = Number(slotData.overall_pnl ?? slotData.overall_realized_pnl ?? run.accumulated_realized_pnl ?? 0);
    const pnlSign = todayPnl > 0 ? '+' : '';
    const pnlClass = todayPnl > 0 ? 'slot-pnl-pos' : todayPnl < 0 ? 'slot-pnl-neg' : 'slot-pnl-zero';
    const pnlFormatted = `${pnlSign}₹${todayPnl.toFixed(2)}`;

    html += `
      <button class="slot-tab ${isActive ? 'active' : ''} ${isRunning ? 'running' : ''}" onclick="switchSlot('${def.code}')" title="Today P&L: ₹${todayPnl.toFixed(2)} | Overall P&L: ₹${overallPnl.toFixed(2)}">
        <span class="slot-num">${def.code}</span>
        <span class="slot-label">${label}</span>
        <span class="slot-tab-pnl-badge ${pnlClass}">${pnlFormatted}</span>
        ${isRunning ? '<span class="slot-running-dot" title="Running"></span>' : ''}
      </button>
    `;
  });

  slotTabBar.innerHTML = html;
}

async function switchSlot(slotIdentifier) {
  const legacyId = SLOT_LEGACY_MAP[slotIdentifier] || slotIdentifier;
  const canonicalCode = SLOT_CANONICAL_MAP[slotIdentifier] || slotIdentifier;
  activeSlotId = legacyId;

  const runs = currentState && Array.isArray(currentState.runs) ? currentState.runs : [];
  const targetRun = runs.find(r => r.run_id === legacyId || r.slot_id === canonicalCode || r.slot_code === canonicalCode);
  if (targetRun) {
    currentInstrument = (targetRun.instrument_name || targetRun.config?.instrument_name || (canonicalCode.startsWith('N') ? 'NIFTY' : 'SENSEX')).toUpperCase();
    const cfg = targetRun.config;
    if (cfg) {
      if (cfg.range_high != null && rangeHighInput) rangeHighInput.value = cfg.range_high;
      if (cfg.range_low != null && rangeLowInput) rangeLowInput.value = cfg.range_low;
      if (cfg.slice_interval != null && sliceSizeInput) sliceSizeInput.value = cfg.slice_interval;
      if (cfg.profit_point != null && profitPointInput) profitPointInput.value = cfg.profit_point;
      if (cfg.loss_point != null && lossPointInput) lossPointInput.value = cfg.loss_point;
      if (cfg.qty_per_slice_lots != null && qtyPerSliceInput) qtyPerSliceInput.value = cfg.qty_per_slice_lots;
      if (cfg.option_type && optionTypeSelect) {
        optionTypeSelect.value = cfg.option_type;
        currentSelectedSide = cfg.option_type;
      }
      if (cfg.strike_source) {
        selectedStrikeSource = cfg.strike_source;
      }
      if (cfg.strike && selectedStrikeSource === 'manual') {
        manualSelectedStrike = cfg.strike;
      }
      if (cfg.cutoff_time_ist) {
        updateEodCutoffDisplay(cfg.cutoff_time_ist, false);
      }
      updateStrikeSourceBadge();
      updateSpanBadge();
      updateSpotPreview();
    } else {
      loadInstrumentParams(currentInstrument);
    }
    if (currentState) {
      currentState.active_run = targetRun;
      renderState(currentState);
    }
  }

  // ── Restore Astro Auto-Trigger master state ──────────────────────────────
  const masterSaved = getPersistedAstroArmed();
  const isArmed = masterSaved !== null ? masterSaved : Boolean(currentState?.astro_state?.auto_trigger_by_slot?.all);
  _userAstroArmed = isArmed;
  const toggleEl = document.getElementById('astroAutoToggle');
  if (toggleEl) toggleEl.checked = isArmed;
  const slotTagEl = document.getElementById('astroSlotTag');
  if (slotTagEl) {
    safeSetText(slotTagEl, isArmed
      ? `Astro Auto-Execution: ARMED (Call & Put Active)`
      : `Auto-Trigger: OFF (All Slots Inactive)`);
  }
  // ─────────────────────────────────────────────────────────────────────────

  try {
    await apiFetch('/api/runs/switch_active', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: legacyId, slot_id: canonicalCode }),
    });
  } catch (e) {
    console.error('Error switching slot:', e);
  }
}

// ── Fixed 4-Slots Status Matrix Renderer ──────────────────────────────────
function renderFixedSlotsMatrix(slotsMatrix, activeSignal, eligibleSlots, basketScope, instrumentPnl, runs) {
  // Update basket risk scope toggle buttons
  const scopePer = document.getElementById('scopePerInstrument');
  const scopeGlob = document.getElementById('scopeGlobal');
  const currentScope = basketScope || 'per_instrument';
  if (scopePer) scopePer.classList.toggle('active', currentScope === 'per_instrument');
  if (scopeGlob) scopeGlob.classList.toggle('active', currentScope === 'global');

  // Update live signal indicator bar
  const signalPill = document.getElementById('liveSignalTagPill');
  const signalHint = document.getElementById('liveSignalActionHint');
  const signal = (activeSignal || 'NEUTRAL').toUpperCase();
  if (signalPill) {
    signalPill.innerText = `SIGNAL: ${signal}`;
    signalPill.className = 'signal-tag-pill';
    if (signal === 'UPSIDE' || signal === 'BUY CE') {
      signalPill.classList.add('signal-upside');
    } else if (signal === 'DOWNSIDE' || signal === 'BUY PE') {
      signalPill.classList.add('signal-downside');
    } else {
      signalPill.classList.add('signal-neutral');
    }
  }
  if (signalHint) {
    if (signal === 'UPSIDE' || signal === 'BUY CE') {
      signalHint.innerText = 'Targeting CALL Slots (N-C, S-C) if free. PUT slots untouched.';
    } else if (signal === 'DOWNSIDE' || signal === 'BUY PE') {
      signalHint.innerText = 'Targeting PUT Slots (N-P, S-P) if free. CALL slots untouched.';
    } else {
      signalHint.innerText = 'Watching for 3-row cluster consensus. No new entries.';
    }
  }

  // Update each slot card
  const slots = ['N-C', 'N-P', 'S-C', 'S-P'];
  const elMap = {
    'N-C': { card: 'cardSlotNC', pill: 'statusPillNC', sym: 'symbolNC', entry: 'entryLtpNC', todayPnl: 'todayPnlNC', pnl: 'pnlNC', elig: 'eligibilityNC', legacy: 'run01' },
    'N-P': { card: 'cardSlotNP', pill: 'statusPillNP', sym: 'symbolNP', entry: 'entryLtpNP', todayPnl: 'todayPnlNP', pnl: 'pnlNP', elig: 'eligibilityNP', legacy: 'run02' },
    'S-C': { card: 'cardSlotSC', pill: 'statusPillSC', sym: 'symbolSC', entry: 'entryLtpSC', todayPnl: 'todayPnlSC', pnl: 'pnlSC', elig: 'eligibilitySC', legacy: 'run03' },
    'S-P': { card: 'cardSlotSP', pill: 'statusPillSP', sym: 'symbolSP', entry: 'entryLtpSP', todayPnl: 'todayPnlSP', pnl: 'pnlSP', elig: 'eligibilitySP', legacy: 'run04' }
  };

  const eligibleSet = new Set(eligibleSlots || []);
  const runsArr = Array.isArray(runs) ? runs : [];
  const slotPnlCache = {};

  slots.forEach(code => {
    const refs = elMap[code];
    if (!refs) return;
    const cardEl = document.getElementById(refs.card);
    const pillEl = document.getElementById(refs.pill);
    const symEl = document.getElementById(refs.sym);
    const entryEl = document.getElementById(refs.entry);
    const todayPnlEl = document.getElementById(refs.todayPnl);
    const pnlEl = document.getElementById(refs.pnl);
    const eligEl = document.getElementById(refs.elig);

    const run = runsArr.find(r => r.run_id === refs.legacy || r.slot_id === code || r.slot_code === code) || {};
    const slotData = (slotsMatrix && (slotsMatrix[code] || slotsMatrix[refs.legacy])) || {};
    const status = (slotData.status || run.slot_status || (run.is_active ? 'OPEN' : 'EMPTY')).toUpperCase();
    const isEligible = eligibleSet.has(code);

    if (cardEl) {
      cardEl.classList.toggle('eligible-glow', isEligible);
      const isCardActive = (activeSlotId === code || activeSlotId === refs.legacy || (SLOT_LEGACY_MAP[code] === activeSlotId));
      cardEl.classList.toggle('selected-slot', Boolean(isCardActive));
    }

    if (pillEl) {
      pillEl.innerText = status;
      pillEl.className = 'slot-status-pill';
      if (status === 'OPEN') pillEl.classList.add('status-open');
      else if (status === 'CLOSED') pillEl.classList.add('status-closed');
      else pillEl.classList.add('status-empty');
    }

    if (symEl) {
      if (slotData.symbol) {
        symEl.innerText = slotData.symbol;
      } else if (slotData.strike && slotData.option_type) {
        symEl.innerText = `${slotData.instrument || (code.startsWith('N') ? 'NIFTY' : 'SENSEX')} ${slotData.strike} ${slotData.option_type}`;
      } else if (run.config?.strike && run.config?.option_type) {
        symEl.innerText = `${run.instrument_name || run.config?.instrument_name || (code.startsWith('N') ? 'NIFTY' : 'SENSEX')} ${run.config.strike} ${run.config.option_type}`;
      } else {
        const inst = code.startsWith('N') ? 'NIFTY' : 'SENSEX';
        const type = code.endsWith('C') ? 'CE' : 'PE';
        symEl.innerText = `${inst} -- ${type}`;
      }
    }

    if (entryEl) {
      const ep = slotData.entry_price != null ? Number(slotData.entry_price).toFixed(2) : (run.active_avg_price != null ? Number(run.active_avg_price).toFixed(2) : '--');
      const ltp = (slotData.last_ltp != null || slotData.ltp != null || run.last_ltp != null) ? Number(slotData.last_ltp ?? slotData.ltp ?? run.last_ltp).toFixed(2) : '--';
      entryEl.innerText = `${ep} / ${ltp}`;
    }

    const tPnl = Number(slotData.today_pnl ?? slotData.today_realized_pnl ?? run.today_realized_pnl ?? 0);
    const oPnl = Number(slotData.overall_pnl ?? slotData.overall_realized_pnl ?? slotData.realized_pnl ?? run.accumulated_realized_pnl ?? run.total_pnl ?? 0);
    slotPnlCache[code] = { today: tPnl, overall: oPnl };

    if (todayPnlEl) {
      renderPnl(todayPnlEl, tPnl);
    }

    if (pnlEl) {
      renderPnl(pnlEl, oPnl);
    }

    if (eligEl) {
      if (status === 'OPEN') {
        eligEl.innerText = 'Active Position';
        eligEl.style.color = 'var(--accent-emerald)';
      } else if (isEligible) {
        eligEl.innerText = '⚡ Signal Ready';
        eligEl.style.color = 'var(--accent-cyan, #06b6d4)';
      } else {
        eligEl.innerText = 'Standby (Slot Free)';
        eligEl.style.color = 'var(--text-muted)';
      }
    }
  });

  // Update Instrument-Level P&L Headers (NIFTY & SENSEX)
  const niftyToday = (instrumentPnl && instrumentPnl.NIFTY) ? (instrumentPnl.NIFTY.today_pnl ?? instrumentPnl.NIFTY.today_realized_pnl ?? 0) : ((slotPnlCache['N-C']?.today || 0) + (slotPnlCache['N-P']?.today || 0));
  const niftyOverall = (instrumentPnl && instrumentPnl.NIFTY) ? (instrumentPnl.NIFTY.overall_pnl ?? instrumentPnl.NIFTY.overall_realized_pnl ?? 0) : ((slotPnlCache['N-C']?.overall || 0) + (slotPnlCache['N-P']?.overall || 0));
  const sensexToday = (instrumentPnl && instrumentPnl.SENSEX) ? (instrumentPnl.SENSEX.today_pnl ?? instrumentPnl.SENSEX.today_realized_pnl ?? 0) : ((slotPnlCache['S-C']?.today || 0) + (slotPnlCache['S-P']?.today || 0));
  const sensexOverall = (instrumentPnl && instrumentPnl.SENSEX) ? (instrumentPnl.SENSEX.overall_pnl ?? instrumentPnl.SENSEX.overall_realized_pnl ?? 0) : ((slotPnlCache['S-C']?.overall || 0) + (slotPnlCache['S-P']?.overall || 0));

  const elNiftyToday = document.getElementById('instPnlTodayNifty');
  const elNiftyOverall = document.getElementById('instPnlOverallNifty');
  const elSensexToday = document.getElementById('instPnlTodaySensex');
  const elSensexOverall = document.getElementById('instPnlOverallSensex');

  if (elNiftyToday) renderPnl(elNiftyToday, niftyToday);
  if (elNiftyOverall) renderPnl(elNiftyOverall, niftyOverall);
  if (elSensexToday) renderPnl(elSensexToday, sensexToday);
  if (elSensexOverall) renderPnl(elSensexOverall, sensexOverall);
}

// ── Basket Risk Scope Handler ──────────────────────────────────────────────
async function setBasketRiskScope(scope) {
  try {
    const res = await apiFetch('/api/risk/basket_scope', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope }),
    });
    const data = await res.json();
    if (data.status === 'success') {
      const scopePer = document.getElementById('scopePerInstrument');
      const scopeGlob = document.getElementById('scopeGlobal');
      if (scopePer) scopePer.classList.toggle('active', scope === 'per_instrument');
      if (scopeGlob) scopeGlob.classList.toggle('active', scope === 'global');
    }
  } catch (e) {
    console.error('Failed to set basket risk scope:', e);
  }
}

// ── Ledger Tab & Slot Events Handlers ──────────────────────────────────────
let currentLedgerTab = 'slices';

function setLedgerTab(tab) {
  currentLedgerTab = tab;
  const btnSlices = document.getElementById('btnViewSlices');
  const btnEvents = document.getElementById('btnViewSlotEvents');
  const slicesVp = document.getElementById('slicesViewport');
  const eventsVp = document.getElementById('slotEventsViewport');
  const modeFilterGroup = document.getElementById('slicesModeFilterGroup');

  if (tab === 'slot_events') {
    if (btnSlices) btnSlices.classList.remove('active');
    if (btnEvents) btnEvents.classList.add('active');
    if (slicesVp) slicesVp.style.display = 'none';
    if (eventsVp) eventsVp.style.display = 'block';
    if (modeFilterGroup) modeFilterGroup.style.display = 'none';
    refreshSlotEvents();
  } else {
    if (btnSlices) btnSlices.classList.add('active');
    if (btnEvents) btnEvents.classList.remove('active');
    if (slicesVp) slicesVp.style.display = 'block';
    if (eventsVp) eventsVp.style.display = 'none';
    if (modeFilterGroup) modeFilterGroup.style.display = 'inline-flex';
  }
}

async function refreshSlotEvents(events) {
  const tbody = document.getElementById('slotEventsTableBody');
  if (!tbody) return;

  let slotEvents = events;
  if (!slotEvents) {
    try {
      const res = await apiFetch('/api/slots/events');
      const data = await res.json();
      if (data && Array.isArray(data.events)) {
        slotEvents = data.events;
      }
    } catch (e) {
      console.warn('Error fetching slot events:', e);
    }
  }

  if (!slotEvents || slotEvents.length === 0) {
    tbody.innerHTML = '<tr class="empty-table-row empty-row"><td colspan="10">No slot fill or exit events recorded yet.</td></tr>';
    return;
  }

  // Sort descending by timestamp
  const sorted = slotEvents.slice().reverse();
  let html = '';
  sorted.forEach(ev => {
    const isExit = ev.event_type && ev.event_type.includes('EXIT');
    const pnlInr = ev.pnl_inr || 0;
    const pnlPts = ev.pnl_points != null ? Number(ev.pnl_points).toFixed(2) : '--';
    const pnlColor = pnlInr > 0 ? 'var(--accent-emerald)' : (pnlInr < 0 ? 'var(--accent-rose)' : 'inherit');
    const evBadge = isExit
      ? '<span class="status-pill status-closed" style="padding:2px 8px;font-size:0.75rem;">EXIT</span>'
      : '<span class="status-pill status-open" style="padding:2px 8px;font-size:0.75rem;">FILL</span>';

    html += `<tr>
      <td>${ev.timestamp_ist || ev.timestamp || '--'}</td>
      <td><strong>${ev.slot_id || '--'}</strong></td>
      <td>${evBadge}</td>
      <td><span style="font-weight:600;">${ev.signal || '--'}</span></td>
      <td>${ev.strike || '--'} ${ev.option_type || ''}</td>
      <td>₹${Number(ev.fill_price || ev.price || 0).toFixed(2)}</td>
      <td>${ev.qty || '--'}</td>
      <td style="color:${pnlColor};font-weight:600;">${pnlPts !== '--' ? pnlPts + ' pts' : '--'}</td>
      <td style="color:${pnlColor};font-weight:600;">${fmtPnl(pnlInr)}</td>
      <td style="font-size:0.8rem;color:var(--text-muted);">${ev.details || ev.exit_reason || '--'}</td>
    </tr>`;
  });
  tbody.innerHTML = html;
}

// Expose functions globally for onclick bindings in HTML
window.switchSlot = switchSlot;
window.setBasketRiskScope = setBasketRiskScope;
window.setLedgerTab = setLedgerTab;
window.refreshSlotEvents = refreshSlotEvents;

// ── Main Render ────────────────────────────────────────────────────────────
function renderState(state) {
  currentState = state;

  const runs = Array.isArray(state.runs) ? state.runs : [];
  const currentSlotId = activeSlotId || state.active_run_id || (runs[0] && runs[0].run_id) || 'run01';
  activeSlotId = currentSlotId;

  renderSlotTabs(runs, currentSlotId, state.slots_matrix);

  // Strictly bind activeRun to the currently selected slot tab
  const activeRun = runs.find(r => r.run_id === currentSlotId) || state.active_run || runs[0] || {};
  const cfg = activeRun.config || {};

  const gridLadder = Array.isArray(activeRun.grid_ladder) ? activeRun.grid_ladder : [];
  const activeSlices = Array.isArray(activeRun.active_slices) ? activeRun.active_slices : [];

  // Aggregate all latest closed trades across all slots and global history
  let tradeHistory = [];
  const allTradesMap = new Map();
  if (currentState && Array.isArray(currentState.history)) {
    currentState.history.forEach(t => { if (t && (t.trade_id || t.entry_time)) allTradesMap.set(t.trade_id || `${t.run_id}_${t.entry_time}`, t); });
  }
  if (Array.isArray(state.history)) {
    state.history.forEach(t => { if (t && (t.trade_id || t.entry_time)) allTradesMap.set(t.trade_id || `${t.run_id}_${t.entry_time}`, t); });
  }
  runs.forEach(r => {
    if (Array.isArray(r.trade_history)) {
      r.trade_history.forEach(t => { if (t && (t.trade_id || t.entry_time)) allTradesMap.set(t.trade_id || `${t.run_id}_${t.entry_time}`, t); });
    }
  });
  if (Array.isArray(state.closed_slices)) {
    state.closed_slices.forEach(t => { if (t && (t.trade_id || t.entry_time)) allTradesMap.set(t.trade_id || `${t.run_id}_${t.entry_time}`, t); });
  }
  if (activeRun && Array.isArray(activeRun.trade_history)) {
    activeRun.trade_history.forEach(t => { if (t && (t.trade_id || t.entry_time)) allTradesMap.set(t.trade_id || `${t.run_id}_${t.entry_time}`, t); });
  }
  tradeHistory = Array.from(allTradesMap.values());

  // Ensure latest closed trades are always sorted strictly by exit timestamp descending (newest on top)
  tradeHistory = tradeHistory.slice().sort((a, b) => {
    const timeA = new Date(a.exit_time || a.exited_at || a.filled_at || a.created_at || 0).getTime();
    const timeB = new Date(b.exit_time || b.exited_at || b.filled_at || b.created_at || 0).getTime();
    return timeB - timeA;
  });

  const auditEvents = Array.isArray(activeRun.audit_events) ? activeRun.audit_events : [];

  // Sync active instrument
  const isRunning = Boolean(activeRun.is_active);
  setStrategyControlsLock(isRunning);

  if (isRunning) {
    currentInstrument = (activeRun.instrument_name || cfg.instrument_name || 'NIFTY').toUpperCase();
    if (cfg.range_points != null && rangePointsInput) rangePointsInput.value = cfg.range_points;
    if (cfg.slicer_count != null && slicerCountInput) slicerCountInput.value = cfg.slicer_count;
    if (cfg.range_high != null && rangeHighInput) rangeHighInput.value = cfg.range_high;
    if (cfg.range_low != null && rangeLowInput) rangeLowInput.value = cfg.range_low;
    if (cfg.slice_interval != null && sliceSizeInput) sliceSizeInput.value = cfg.slice_interval;
    if (cfg.profit_point != null && profitPointInput) profitPointInput.value = cfg.profit_point;
    if (cfg.loss_point != null && lossPointInput) lossPointInput.value = cfg.loss_point;
    if (cfg.qty_per_slice_lots != null && qtyPerSliceInput) qtyPerSliceInput.value = cfg.qty_per_slice_lots;
    if (cfg.option_type && optionTypeSelect) optionTypeSelect.value = cfg.option_type;
    if (cfg.cutoff_time_ist) updateEodCutoffDisplay(cfg.cutoff_time_ist, false);
    updateSpanBadge();
  } else if (activeRun.config && activeRun.config.instrument_name) {
    currentInstrument = activeRun.config.instrument_name.toUpperCase();
  } else if (activeRun.instrument_name) {
    currentInstrument = activeRun.instrument_name.toUpperCase();
  }
  const instCfg = INSTRUMENT_CONFIG[currentInstrument] || INSTRUMENT_CONFIG.NIFTY;


  if (instrumentSelect && document.activeElement !== instrumentSelect) {
    instrumentSelect.value = currentInstrument;
  }
  if (headerAccentTitle) headerAccentTitle.innerText = currentInstrument;
  if (spotCardHeaderTitle) spotCardHeaderTitle.innerText = instCfg.headerTitle;
  if (spotInputLabel) spotInputLabel.innerText = instCfg.spotLabel;
  if (resStrikeLabel) resStrikeLabel.innerText = instCfg.strikeLabel;

  // Resolve live spot price strictly strictly for currentInstrument
  let spotLtp = null;
  const isSensex = currentInstrument === 'SENSEX';
  if (activeRun.spot_ltp != null) {
    if ((isSensex && activeRun.spot_ltp >= 50000) || (!isSensex && activeRun.spot_ltp < 50000)) {
      spotLtp = activeRun.spot_ltp;
    }
  }
  if (spotLtp == null && state.angel_feed?.spot_by_inst?.[currentInstrument] != null) {
    const s = state.angel_feed.spot_by_inst[currentInstrument];
    if ((isSensex && s >= 50000) || (!isSensex && s < 50000)) {
      spotLtp = s;
    }
  }
  if (spotLtp == null) {
    spotLtp = instCfg.defaultSpot;
  }

  // Resolve option LTP strictly for active contract
  let lastLtp = null;
  if (activeRun.last_ltp != null) {
    lastLtp = activeRun.last_ltp;
  }

  const currentSpotVal = (spotInput && document.activeElement === spotInput && spotInput.value)
    ? parseFloat(spotInput.value)
    : (spotLtp != null ? spotLtp : instCfg.defaultSpot);
  const nearestStrike = calculateNearestStrike(currentSpotVal, currentInstrument);

  const strike = isRunning
    ? (activeRun.locked_strike || activeRun.strike || nearestStrike)
    : ((selectedStrikeSource === 'manual' && manualSelectedStrike) ? manualSelectedStrike : nearestStrike);
  const optType = optionTypeSelect ? optionTypeSelect.value : (activeRun.option_type || cfg.option_type || 'CE');
  const contractSymbol = isRunning && (activeRun.contract_symbol || activeRun.locked_contract_symbol)
    ? (activeRun.contract_symbol || activeRun.locked_contract_symbol)
    : `${currentInstrument} ${strike} ${optType}`;

  if (symbolText) symbolText.innerText = contractSymbol;

  if (headerSpotLtp && spotLtp != null) headerSpotLtp.innerText = fmt(spotLtp, 2);
  if (headerActiveStrike && strike != null) headerActiveStrike.innerText = strike;
  if (headerLtp) headerLtp.innerText = lastLtp != null ? `₹${fmt(lastLtp, 2)}` : '--';
  if (headerTotalPnl) renderPnl(headerTotalPnl, activeRun.total_pnl || 0);

  if (spotLtp != null && document.activeElement !== spotInput) {
    if (spotInput) spotInput.value = fmt(spotLtp, 2);
  }
  const displayStrike = strike;
  const displayContract = isRunning ? contractSymbol : `${currentInstrument} ${strike} ${optType}`;

  const currentSource = isRunning
    ? (activeRun.strike_source || activeRun.config?.strike_source || 'auto')
    : selectedStrikeSource;
  const srcTag = currentSource === 'manual' ? ' [MANUAL]' : ' [AUTO]';

  if (strikeBadge) strikeBadge.innerText = (isRunning ? `LOCKED: ${displayStrike}` : `ATM: ${nearestStrike}`) + srcTag;
  if (resStrikeVal) resStrikeVal.innerText = nearestStrike;
  if (resContractVal) resContractVal.innerText = `Active Contract: ${displayContract}`;
  if (resOptLtpVal) resOptLtpVal.innerText = lastLtp != null ? `₹${fmt(lastLtp, 2)}` : '--';

  if (strikeSourceBadge) {
    strikeSourceBadge.className = 'strike-source-tag ' + (currentSource === 'manual' ? 'manual' : 'auto');
    strikeSourceBadge.innerText = currentSource === 'manual' ? 'MANUAL' : 'AUTO';
  }

  populateStrikeDropdown(currentSpotVal, currentInstrument, displayStrike);

  // Option LTP input
  if (lastLtp != null && optLtpInput && document.activeElement !== optLtpInput) {
    optLtpInput.value = fmt(lastLtp, 2);
  }

  // Live Option LTP Metric Card
  if (metricLiveLtp) metricLiveLtp.innerText = lastLtp != null ? `₹${fmt(lastLtp, 2)}` : '--';
  if (metricLiveLtpSub) metricLiveLtpSub.innerText = contractSymbol || 'Option Premium';

  // Run Slicer button & EOD Squareoff button state
  const btn = document.getElementById('btnRunSlicer');
  const btnEod = document.getElementById('btnEodSquareoff');

  if (btn) {
    if (isRunning) {
      btn.innerHTML = '⏹ Stop Slicer';
      btn.className = 'btn btn-run-slicer running';
      btn.style.background = 'linear-gradient(135deg, #e11d48 0%, #be123c 100%)';
      btn.style.boxShadow = '0 4px 15px rgba(225, 29, 72, 0.45)';
    } else {
      btn.innerHTML = '🚀 Run Slicer';
      btn.className = 'btn btn-run-slicer';
      btn.style.background = '';
      btn.style.boxShadow = '';
    }
  }

  if (btnEod) {
    btnEod.style.display = isRunning ? 'block' : 'none';
    const currentTimeStr = (cfg && cfg.cutoff_time_ist) ? cfg.cutoff_time_ist : (eodCutoffInput ? eodCutoffInput.value : '15:22');
    btnEod.innerHTML = `⏹ EOD Squareoff (${formatTime12h(currentTimeStr)})`;
  }

  // Feed status indicator
  if (connectionStatus && activeRun.feed_status === 'STALE') {
    connectionStatus.innerHTML = '<span class="pulse-indicator" style="background:#f59e0b;box-shadow:none;"></span><span class="status-text" style="color:#f59e0b;">Feed STALE</span>';
    connectionStatus.style.borderColor = 'rgba(245, 158, 11, 0.5)';
  }

  // Filter trades matching currently selected instrument (NIFTY vs SENSEX)
  const filteredTrades = tradeHistory.filter(rec => {
    return getTradeInstrument(rec) === currentInstrument;
  });

  // Separate Paper vs Live Closed Trades & P&L
  const paperTrades = filteredTrades.filter(t => (t.mode || 'paper').toLowerCase() === 'paper');
  const liveTrades = filteredTrades.filter(t => (t.mode || 'paper').toLowerCase() === 'live');

  const grossPaperPnl = paperTrades.reduce((acc, t) => acc + (t.pnl_rupees || 0), 0);
  const grossLivePnl = liveTrades.reduce((acc, t) => acc + (t.pnl_rupees || 0), 0);

  // Today's Closed Trades & Realized P&L
  const todayTrades = filteredTrades.filter(rec => {
    const exitTime = rec.exit_time || rec.exited_at || rec.entry_time || rec.filled_at || rec.created_at;
    return isTodayIST(exitTime);
  });
  const todayPaperTrades = todayTrades.filter(t => (t.mode || 'paper').toLowerCase() === 'paper');
  const todayLiveTrades = todayTrades.filter(t => (t.mode || 'paper').toLowerCase() === 'live');

  const todayPaperPnl = todayPaperTrades.reduce((acc, t) => acc + (t.pnl_rupees || 0), 0);
  const todayLivePnl = todayLiveTrades.reduce((acc, t) => acc + (t.pnl_rupees || 0), 0);

  // Update Separated Telemetry Elements
  const elGrossPaper = document.getElementById('metricGrossPaperPnl');
  const elGrossLive = document.getElementById('metricGrossLivePnl');
  const elGrossPaperCount = document.getElementById('metricGrossPaperCount');
  const elGrossLiveCount = document.getElementById('metricGrossLiveCount');

  if (elGrossPaper) renderPnl(elGrossPaper, grossPaperPnl);
  if (elGrossLive) renderPnl(elGrossLive, grossLivePnl);
  if (elGrossPaperCount) elGrossPaperCount.innerText = `${paperTrades.length} Closed`;
  if (elGrossLiveCount) elGrossLiveCount.innerText = `${liveTrades.length} Closed`;

  const elRealizedPaper = document.getElementById('metricRealizedPaperPnl');
  const elRealizedLive = document.getElementById('metricRealizedLivePnl');
  const elPaperClosedCount = document.getElementById('metricPaperClosedCount');
  const elLiveClosedCount = document.getElementById('metricLiveClosedCount');

  if (elRealizedPaper) renderPnl(elRealizedPaper, todayPaperPnl);
  if (elRealizedLive) renderPnl(elRealizedLive, todayLivePnl);
  if (elPaperClosedCount) elPaperClosedCount.innerText = `${todayPaperTrades.length} Today`;
  if (elLiveClosedCount) elLiveClosedCount.innerText = `${todayLiveTrades.length} Today`;

  // Determine current active mode realized values
  const isLiveActive = (state.trading_mode || currentClientTradingMode || 'paper').toLowerCase() === 'live';
  const todayRealizedPnl = isLiveActive ? todayLivePnl : todayPaperPnl;
  const grossRealizedPnl = isLiveActive ? grossLivePnl : grossPaperPnl;

  // Synchronize Top Mode Banner and HUD Pill
  updateTradingModeUI(state.trading_mode || 'paper');


  // Today's Open Slices & Unrealized P&L
  const todayActiveSlices = activeSlices.filter(s => {
    const fillTime = s.filled_at || s.entry_time;
    return !fillTime || isTodayIST(fillTime);
  });
  let todayUnrealizedPnl = 0;
  if (lastLtp != null && todayActiveSlices.length > 0) {
    todayUnrealizedPnl = todayActiveSlices.reduce((acc, s) => {
      const fill = s.fill_price != null ? s.fill_price : s.level_price;
      return acc + ((lastLtp - fill) * (s.quantity || 0));
    }, 0);
  } else if (todayActiveSlices.length === activeSlices.length) {
    todayUnrealizedPnl = activeRun.unrealized_pnl_rupees || 0;
  }

  const totalInstTodayPnl = todayRealizedPnl + todayUnrealizedPnl;
  const lossTrigger = activeRun.loss_trigger_price;
  const cycleCount = activeRun.cycle_count || 1;

  // Update Selected Slot Telemetry Pod
  const canonicalSlot = SLOT_CANONICAL_MAP[currentSlotId] || currentSlotId;
  const activeSlotMatrixData = (state.slots_matrix && (state.slots_matrix[canonicalSlot] || state.slots_matrix[currentSlotId])) || {};
  const slotTodayVal = Number(activeSlotMatrixData.today_pnl ?? activeSlotMatrixData.today_realized_pnl ?? activeRun.today_realized_pnl ?? 0);
  const slotOverallVal = Number(activeSlotMatrixData.overall_pnl ?? activeSlotMatrixData.overall_realized_pnl ?? activeSlotMatrixData.realized_pnl ?? activeRun.accumulated_realized_pnl ?? 0);

  const slotTrades = tradeHistory.filter(t => tradeMatchesSlot(t, canonicalSlot));
  const todaySlotTrades = slotTrades.filter(t => isTodayIST(t.exit_time || t.exited_at || t.entry_time || t.filled_at || t.created_at));

  const elSlotTag = document.getElementById('metricSlotTag');
  const elSlotBadge = document.getElementById('metricSlotBadge');
  const elSlotTodayPnl = document.getElementById('metricSlotTodayPnl');
  const elSlotOverallPnl = document.getElementById('metricSlotOverallPnl');
  const elSlotTodayCount = document.getElementById('metricSlotTodayCount');
  const elSlotOverallCount = document.getElementById('metricSlotOverallCount');

  if (elSlotTag) elSlotTag.innerText = `SLOT P&L [${canonicalSlot}]`;
  if (elSlotBadge) elSlotBadge.innerText = (activeRun.is_active || activeSlotMatrixData.is_active) ? 'RUNNING' : 'STANDBY';
  if (elSlotTodayPnl) renderPnl(elSlotTodayPnl, slotTodayVal);
  if (elSlotOverallPnl) renderPnl(elSlotOverallPnl, slotOverallVal);
  if (elSlotTodayCount) elSlotTodayCount.innerText = `${todaySlotTrades.length} Today`;
  if (elSlotOverallCount) elSlotOverallCount.innerText = `${slotTrades.length} Closed`;

  if (headerTotalPnl) renderPnl(headerTotalPnl, totalInstTodayPnl);
  if (metricGrossPnl) renderPnl(metricGrossPnl, grossRealizedPnl);
  if (metricGrossClosedCount) metricGrossClosedCount.innerText = `${filteredTrades.length} Closed (${currentInstrument})`;
  renderPnl(metricRealizedPnl, todayRealizedPnl);
  if (metricClosedCount) metricClosedCount.innerText = `${todayTrades.length} Closed Today`;
  renderPnl(metricUnrealizedPnl, todayUnrealizedPnl);
  if (metricOpenCount) metricOpenCount.innerText = `${todayActiveSlices.length} Open Slices`;
  const span = (cfg.range_high || 150) - (cfg.range_low || 100);
  const step = cfg.slice_interval || cfg.slice_size || 10;
  if (metricSpanStep) metricSpanStep.innerText = `${span.toFixed(1)} pts / ${step} pts`;
  if (metricTotalLevels) metricTotalLevels.innerText = isRunning ? `${gridLadder.length} Total Levels` : '--';

  if (metricLossTrigger) {
    if (lossTrigger != null && isRunning) {
      metricLossTrigger.innerText = `₹${fmt(lossTrigger)}`;
      metricLossTrigger.className = 'm-value loss-text';
    } else {
      const defLoss = currentInstrument === 'SENSEX' ? 30 : 8;
      const defaultRangeSl = (cfg.range_low || 100) - (cfg.loss_point || defLoss);
      metricLossTrigger.innerText = isRunning ? `₹${fmt(lossTrigger || defaultRangeSl)}` : `₹${fmt(defaultRangeSl)}`;
      metricLossTrigger.className = 'm-value loss-text';
    }
  }
  if (metricLossDetail) {
    const rangeBottom = activeRun.range_bottom != null ? activeRun.range_bottom : (cfg.range_low || 100);
    const defLoss = currentInstrument === 'SENSEX' ? 30 : 8;
    metricLossDetail.innerText = `Range Bottom (${fmt(rangeBottom)}) – ${cfg.loss_point || defLoss} pts`;
  }

  if (ladderNote) {
    if (isRunning) {
      ladderNote.innerText = `Cycle #${cycleCount}  (${cfg.range_high || 150} → ${cfg.range_low || 100})`;
      ladderNote.style.color = 'var(--accent-emerald)';
    } else {
      ladderNote.innerText = 'Slicer Stopped';
      ladderNote.style.color = 'var(--text-muted)';
    }
  }

  // Render sub-views
  renderLadder(gridLadder, activeSlices, lastLtp, lossTrigger, isRunning);
  renderOpenSlices(activeSlices, lastLtp);
  renderClosedSlices(filteredTrades);

  // Render Fixed 4-Slots Matrix and Risk Scope
  renderFixedSlotsMatrix(
    state.slots_matrix || {},
    state.active_signal,
    (state.astro_state && state.astro_state.eligible_slots),
    state.basket_risk_scope,
    state.instrument_pnl || (state.astro_state && state.astro_state.instrument_pnl),
    runs
  );

  // Update Slot Fill & Exit Log if active or if events provided
  if (currentLedgerTab === 'slot_events' && state.slot_events) {
    refreshSlotEvents(state.slot_events);
  }

  if (state.astro_state) {
    renderAstroSection({
      active_file: {
        filename: state.astro_state.filename,
        file_id: state.astro_state.file_id,
        row_count: state.astro_state.row_count,
        uploaded_at: state.astro_state.uploaded_at,
      },
      auto_trigger_by_slot: state.astro_state.auto_trigger_by_slot,
      last_astro_direction: state.astro_state.last_astro_direction,
      preview: state.astro_state.cluster_preview || window._lastAstroPreview,
    });
  }
}

// ── P&L Color Renderer ─────────────────────────────────────────────────────
function renderPnl(el, val) {
  if (!el) return;
  const num = parseFloat(val) || 0;
  el.innerText = fmtPnl(num);
  el.style.color = num > 0 ? 'var(--accent-emerald)' : num < 0 ? 'var(--accent-rose)' : 'var(--text-primary)';
}

// ── Ladder Renderer ────────────────────────────────────────────────────────
function renderLadder(gridLadder, activeSlices, lastLtp, lossTrigger, isRunning) {
  if (!ladderContainer) return;
  if (!isRunning || !gridLadder || gridLadder.length === 0) {
    ladderContainer.innerHTML = '<div style="text-align:center;padding:45px 20px;color:var(--text-muted);font-size:0.85rem;line-height:1.6;"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="margin:0 auto 10px;opacity:0.4;display:block;"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>Ladder is inactive.<br>Configure parameters and click <strong>🚀 Run Slicer</strong> to generate and visualize live ladder levels.</div>';
    return;
  }

  // Build a map of active slice info by level_price for quick lookup
  const activeByLevel = {};
  activeSlices.forEach(s => {
    activeByLevel[s.level_price] = s;
  });

  let html = '';
  gridLadder.forEach(lvl => {
    const isFilled = lvl.status === 'FILLED';
    const isUsed = lvl.status === 'USED';
    const isUnused = lvl.status === 'UNUSED' || lvl.status === 'PENDING';
    const isCurrentLtp = lastLtp != null && Math.abs(lastLtp - lvl.level_price) < 0.5;

    let stepClass = 'unused';
    let statusLabel = 'UNUSED';
    let statusBadgeClass = 'status-badge-unused';
    let extraInfo = '';

    if (isFilled) {
      stepClass = 'filled';
      statusLabel = lvl.label ? `[${lvl.label}] FILLED` : 'FILLED';
      statusBadgeClass = 'status-badge-filled';
      if (lvl.fill_price != null) {
        extraInfo = `<span class="ladder-fill-info">Filled @ ${fmt(lvl.fill_price)} → Target ${fmt(lvl.profit_target)}</span>`;
      }
    } else if (isUsed) {
      stepClass = 'used';
      statusLabel = 'USED';
      statusBadgeClass = 'status-badge-used';
      extraInfo = '<span class="ladder-used-info">Traded (One-Shot Done)</span>';
    }

    const currentClass = isCurrentLtp ? 'current-ltp' : '';

    html += `
      <div class="ladder-step ${stepClass} ${currentClass}" data-level="${lvl.level_price}">
        <div class="ladder-price-group">
          <span class="price-indicator-dot"></span>
          <span class="ladder-price">${fmt(lvl.level_price)}</span>
          ${extraInfo}
        </div>
        <div class="ladder-status-badge ${statusBadgeClass}">${statusLabel}</div>
      </div>
    `;
  });

  if (lossTrigger != null) {
    html += `
      <div class="ladder-loss-line" title="Range Stop-Loss: Shared exit trigger across all levels in this range">
        <span>⚠ RANGE STOP-LOSS</span>
        <span>₹${fmt(lossTrigger)}</span>
      </div>
    `;
  }

  ladderContainer.innerHTML = html;
}

// ── Open Slices Table ──────────────────────────────────────────────────────
function renderOpenSlices(activeSlices, lastLtp) {
  if (openSlicesBadge) openSlicesBadge.innerText = `${activeSlices.length} Open`;
  if (!openSlicesTableBody) return;

  if (activeSlices.length === 0) {
    openSlicesTableBody.innerHTML = `
      <tr class="empty-row">
        <td colspan="9">No active slice positions. Send ticks or Run Slicer to trigger ladder levels.</td>
      </tr>
    `;
    return;
  }

  // Determine the lowest filled slice
  const fills = activeSlices.map(s => s.fill_price).filter(v => v != null);
  const lowestFill = fills.length ? Math.min(...fills) : null;

  let html = '';
  const sorted = [...activeSlices].sort((a, b) => (b.fill_price || 0) - (a.fill_price || 0));

  sorted.forEach(slice => {
    const fillPrice = slice.fill_price != null ? slice.fill_price : slice.level_price;
    const profitTarget = slice.profit_target;
    const qty = slice.quantity || 65;
    const isLowest = lowestFill != null && fillPrice === lowestFill;

    let pnlPts = 0, pnlRup = 0;
    if (lastLtp != null) {
      pnlPts = lastLtp - fillPrice;
      pnlRup = pnlPts * qty;
    }
    const distToTarget = profitTarget != null ? (profitTarget - (lastLtp || fillPrice)) : null;

    const isLoss = pnlRup < 0 || pnlPts < 0;
    const isProfit = pnlRup > 0 || pnlPts > 0;
    const pnlClass = isLoss ? 'pnl-neg' : isProfit ? 'pnl-pos' : '';
    const pnlColor = isLoss ? 'var(--accent-rose, #f43f5e)' : isProfit ? 'var(--accent-emerald, #10b981)' : 'var(--text-primary)';
    const ptsSign = isProfit ? '+' : isLoss ? '-' : '';
    const rupSign = isProfit ? '+' : isLoss ? '-' : '';
    const pnlDisplay = `${ptsSign}${Math.abs(pnlPts).toFixed(2)} pts / ${rupSign}₹${Math.abs(pnlRup).toFixed(2)}`;
    const sliceId = slice.order_id || slice.label || slice.level_price;
    const sliceLabel = slice.label || '';

    html += `
      <tr>
        <td style="color:var(--accent-cyan);font-weight:800;letter-spacing:0.5px;">${slice.label || '—'}</td>
        <td>${fmt(slice.level_price)}</td>
        <td style="font-weight:700;">${fmt(fillPrice)}</td>
        <td class="target-badge">${profitTarget != null ? fmt(profitTarget) : '—'}</td>
        <td>${qty}</td>
        <td class="${pnlClass}" style="color:${pnlColor};font-weight:700;">${pnlDisplay}</td>
        <td style="color:var(--text-muted);">${distToTarget != null ? `${Math.max(0, distToTarget).toFixed(2)} pts` : '—'}</td>
        <td>${isLowest ? '<span class="lowest-slice-tag">LOWEST</span>' : '<span style="color:var(--text-muted);font-size:0.7rem;">Active</span>'}</td>
        <td style="text-align:center;">
          <button class="btn-exit-slice" onclick="exitOpenSlice('${activeSlotId}', '${sliceId}', '${sliceLabel}', this)" title="Exit slice ${sliceLabel} now at market LTP">
            Exit
          </button>
        </td>
      </tr>
    `;
  });

  openSlicesTableBody.innerHTML = html;
}

// ── Closed Trades History Table ────────────────────────────────────────────
function renderClosedSlices(tradeHistory) {
  let displayTrades = tradeHistory;
  if (currentLedgerModeFilter === 'paper') {
    displayTrades = tradeHistory.filter(t => (t.mode || 'paper').toLowerCase() === 'paper');
  } else if (currentLedgerModeFilter === 'live') {
    displayTrades = tradeHistory.filter(t => (t.mode || 'paper').toLowerCase() === 'live');
  }

  if (closedSlicesBadge) {
    const filterTag = currentLedgerModeFilter === 'all' ? '' : ` [${currentLedgerModeFilter.toUpperCase()}]`;
    closedSlicesBadge.innerText = `${displayTrades.length} Closed (${currentInstrument})${filterTag}`;
  }
  if (!closedSlicesTableBody) return;

  if (displayTrades.length === 0) {
    const modeDesc = currentLedgerModeFilter === 'all' ? '' : `${currentLedgerModeFilter.toUpperCase()} `;
    closedSlicesTableBody.innerHTML = `
      <tr class="empty-row">
        <td colspan="14">No closed ${modeDesc}${currentInstrument} trades yet.</td>
      </tr>
    `;
    return;
  }

  let html = '';
  displayTrades.forEach(rec => {
    const mode = (rec.mode || 'paper').toLowerCase();
    const modeTag = mode === 'live'
      ? '<span class="badge-mode-live">LIVE</span>'
      : '<span class="badge-mode-paper">PAPER</span>';

    const pnlRup = rec.pnl_rupees || 0;
    const pnlPts = rec.pnl_points || 0;
    const isLoss = pnlRup < 0 || pnlPts < 0;
    const isProfit = pnlRup > 0 || pnlPts > 0;
    const pnlClass = isLoss ? 'pnl-neg' : isProfit ? 'pnl-pos' : '';
    const pnlColor = isLoss ? 'var(--accent-rose, #f43f5e)' : isProfit ? 'var(--accent-emerald, #10b981)' : 'var(--text-primary)';
    const ptsSign = isProfit ? '+' : isLoss ? '-' : '';
    const rupSign = isProfit ? '+' : isLoss ? '-' : '';
    const pnlDisplay = `${ptsSign}${Math.abs(pnlPts).toFixed(2)} pts / ${rupSign}₹${Math.abs(pnlRup).toFixed(2)}`;

    let reasonTag = '';
    const reason = (rec.exit_reason || '').toUpperCase();
    if (reason === 'LOSS_EXIT' || reason === 'STOP_LOSS') {
      reasonTag = '<span style="color:var(--accent-rose);font-weight:700;">STOP LOSS</span>';
    } else if (reason === 'EOD_SQUAREOFF' || reason === 'EOD') {
      reasonTag = pnlRup >= 0
        ? '<span style="color:var(--accent-emerald);font-weight:700;">EOD PROFIT</span>'
        : '<span style="color:var(--accent-rose);font-weight:700;">EOD EXIT (LOSS)</span>';
    } else if (reason === 'MANUAL' || reason === 'MANUAL_STOP' || reason === 'USER_STOP') {
      reasonTag = pnlRup >= 0
        ? '<span style="color:var(--accent-emerald);font-weight:700;">MANUAL EXIT</span>'
        : '<span style="color:var(--accent-rose);font-weight:700;">MANUAL (LOSS)</span>';
    } else if (pnlRup < 0) {
      reasonTag = '<span style="color:var(--accent-rose);font-weight:700;">LOSS</span>';
    } else {
      reasonTag = '<span style="color:var(--accent-emerald);font-weight:700;">PROFIT</span>';
    }
    const entryTimeStr = formatISTDateTime(rec.entry_time || rec.filled_at);
    const exitTimeStr = formatISTDateTime(rec.exit_time || rec.exited_at);
    const contractSym = rec.contract_symbol || (rec.symbol ? `${rec.symbol} ${rec.strike || ''} ${rec.option_type || ''}`.trim() : '—');
    const runNum = rec.run_number != null ? rec.run_number : 1;

    const orderDisplay = rec.exit_order_id || rec.entry_order_id || (rec.trade_id ? rec.trade_id.replace('TRD-', '') : '—');
    const orderTip = `Entry Order ID: ${rec.entry_order_id || '—'} | Exit Order ID: ${rec.exit_order_id || '—'}`;
    const targetTradeId = String(rec.trade_id || rec._id || rec.id || '').replace(/'/g, "\\'");

    html += `
      <tr>
        <td>${modeTag}</td>
        <td style="font-weight:700;color:var(--accent-indigo,#818cf8);">${runNum}</td>
        <td style="color:var(--accent-cyan);font-weight:800;" title="${rec.contract_symbol || ''}">${rec.label || '—'}</td>
        <td style="font-family:var(--font-mono);font-size:0.75rem;color:var(--accent-amber);font-weight:600;">${contractSym}</td>
        <td style="font-family:var(--font-mono);font-size:0.72rem;color:var(--text-muted);white-space:nowrap;" title="${orderTip}">${orderDisplay}</td>
        <td>${fmt(rec.level_price)}</td>
        <td>${fmt(rec.fill_price)}</td>
        <td style="font-weight:700;">${fmt(rec.exit_price)}</td>
        <td>${rec.quantity || 65}</td>
        <td class="${pnlClass}" style="color:${pnlColor};font-weight:700;">${pnlDisplay}</td>
        <td>${reasonTag}</td>
        <td style="color:var(--text-muted);font-size:0.75rem;" title="Entry (IST): ${rec.entry_time || rec.filled_at || '--'}">${entryTimeStr}</td>
        <td style="color:var(--text-muted);font-size:0.75rem;" title="Exit (IST): ${rec.exit_time || rec.exited_at || '--'}">${exitTimeStr}</td>
        <td style="text-align:center;">
          <button class="btn-delete-trade" onclick="deleteClosedTrade('${targetTradeId}', this)" title="Delete this closed trade record">
            🗑 Delete
          </button>
        </td>
      </tr>
    `;
  });

  closedSlicesTableBody.innerHTML = html;
}

// ── Delete Individual Closed Trade ──────────────────────────────────────────
async function deleteClosedTrade(tradeId, btn) {
  if (!tradeId) {
    alert('Unable to identify trade record ID.');
    return;
  }
  if (!confirm(`Delete closed trade record "${tradeId}"? This will permanently remove it from trade history and recalculate realized P&L.`)) {
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.innerText = 'Deleting...';
  }
  try {
    const res = await apiFetch(`/api/history/${encodeURIComponent(tradeId)}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.status === 'success') {
      // Instantly remove from local in-memory state for snappiest UI response
      if (currentState) {
        if (Array.isArray(currentState.history)) {
          currentState.history = currentState.history.filter(t => (t.trade_id !== tradeId && t._id !== tradeId && t.id !== tradeId));
        }
        if (Array.isArray(currentState.closed_slices)) {
          currentState.closed_slices = currentState.closed_slices.filter(t => (t.trade_id !== tradeId && t._id !== tradeId && t.id !== tradeId));
        }
        if (currentState.active_run && Array.isArray(currentState.active_run.trade_history)) {
          currentState.active_run.trade_history = currentState.active_run.trade_history.filter(t => (t.trade_id !== tradeId && t._id !== tradeId && t.id !== tradeId));
        }
        if (Array.isArray(currentState.runs)) {
          currentState.runs.forEach(r => {
            if (Array.isArray(r.trade_history)) {
              r.trade_history = r.trade_history.filter(t => (t.trade_id !== tradeId && t._id !== tradeId && t.id !== tradeId));
            }
          });
        }
        renderState(currentState);
      }
      // Trigger full state refresh from server
      fetchInitialState();
    } else {
      alert(`Failed to delete trade: ${data.message || data.error || 'Server error'}`);
      if (btn) {
        btn.disabled = false;
        btn.innerText = '🗑 Delete';
      }
    }
  } catch (err) {
    console.error('Error deleting closed trade:', err);
    alert(`Error deleting trade: ${err.message || err}`);
    if (btn) {
      btn.disabled = false;
      btn.innerText = '🗑 Delete';
    }
  }
}

// ── Clear Trade History ───────────────────────────────────────────────────
async function clearTradeHistory() {
  if (!confirm(`Clear all closed trade history for ${currentInstrument} and reset realized P&L to ₹0.00?`)) {
    return;
  }
  try {
    const res = await fetch(`/api/history/clear?instrument=${encodeURIComponent(currentInstrument)}`, { method: 'POST' });
    const data = await res.json();
    if (data.status === 'success') {
      if (currentState) {
        if (Array.isArray(currentState.history)) {
          currentState.history = currentState.history.filter(t => getTradeInstrument(t) !== currentInstrument);
        }
        if (Array.isArray(currentState.closed_slices)) {
          currentState.closed_slices = currentState.closed_slices.filter(t => getTradeInstrument(t) !== currentInstrument);
        }
        if (currentState.active_run && Array.isArray(currentState.active_run.trade_history)) {
          currentState.active_run.trade_history = currentState.active_run.trade_history.filter(t => getTradeInstrument(t) !== currentInstrument);
        }
        renderState(currentState);
      } else {
        if (closedSlicesTableBody) {
          closedSlicesTableBody.innerHTML = `<tr class="empty-row"><td colspan="10">No closed ${currentInstrument} trades yet.</td></tr>`;
        }
        if (closedSlicesBadge) closedSlicesBadge.innerText = `0 Closed (${currentInstrument})`;
      }
    }
  } catch (e) {
    console.error('Error clearing trade history:', e);
  }
}

// ── Auto Spot Fetch ────────────────────────────────────────────────────────
async function fetchAndApplyAutoSpot() {
  selectedStrikeSource = 'auto';
  manualSelectedStrike = null;
  updateStrikeSourceBadge();
  saveCurrentInputParams();

  // Notify backend that strike mode was reset to auto for this slot
  try {
    const spotVal = parseFloat(spotInput?.value) || 0;
    const atmStrike = calculateNearestStrike(spotVal, currentInstrument);
    await apiFetch(`/api/runs/${activeSlotId}/strike`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ strike: atmStrike, strike_source: 'auto' }),
    });
  } catch (e) { }

  const btn = document.getElementById('btnAutoSpot');
  if (btn) { btn.classList.add('loading'); btn.innerText = 'Syncing...'; }
  try {
    const res = await apiFetch('/api/angel/fetch_spot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: activeSlotId, instrument: currentInstrument }),
    });
    const data = await res.json();
    if (data.status === 'success' && data.spot_ltp) {
      if (spotInput) spotInput.value = parseFloat(data.spot_ltp).toFixed(2);
      updateSpotPreview();
      if (btn) { btn.innerText = '✓ Synced'; setTimeout(() => { btn.innerText = '⚡ Auto'; btn.classList.remove('loading'); }, 1200); }
    } else {
      alert(data.message || 'Live Spot not available from Angel One.');
      if (btn) { btn.innerText = '⚡ Auto'; btn.classList.remove('loading'); }
    }
  } catch (e) {
    console.error(e);
    if (btn) { btn.innerText = '⚡ Auto'; btn.classList.remove('loading'); }
  }
}

// ── Auto Option LTP Fetch ──────────────────────────────────────────────────
async function fetchAndApplyAutoOptLtp() {
  const btn = document.getElementById('btnAutoOptLtp');
  if (btn) { btn.classList.add('loading'); btn.innerText = 'Syncing...'; }
  try {
    const currentSlot = getActiveSlotRun();
    const isRunning = Boolean(currentSlot?.is_active);
    const lockedStrike = isRunning ? (currentSlot.strike || currentSlot.config?.strike) : null;
    const lockedOptType = isRunning ? (currentSlot.option_type || currentSlot.config?.option_type) : null;
    const lockedInst = isRunning ? (currentSlot.instrument_name || currentSlot.config?.instrument_name) : null;

    const inst = lockedInst || currentInstrument;
    const optType = lockedOptType || (optionTypeSelect ? optionTypeSelect.value : currentSelectedSide);
    const cfg = INSTRUMENT_CONFIG[inst] || INSTRUMENT_CONFIG.NIFTY;
    let spotVal = parseFloat(spotInput ? spotInput.value : 0) || cfg.defaultSpot;
    const isSensex = inst.toUpperCase() === 'SENSEX';
    if (isSensex && spotVal < 50000) spotVal = cfg.defaultSpot;
    if (!isSensex && spotVal > 50000) spotVal = cfg.defaultSpot;

    const nearestAtm = calculateNearestStrike(spotVal, inst);

    // Determine target strike: if manual strike is selected, preserve it!
    let targetStrike = lockedStrike;
    if (!targetStrike) {
      if (selectedStrikeSource === 'manual' && manualSelectedStrike != null) {
        targetStrike = manualSelectedStrike;
      } else if (strikeSelect && strikeSelect.value && selectedStrikeSource === 'manual') {
        targetStrike = parseInt(strikeSelect.value);
        manualSelectedStrike = targetStrike;
      } else {
        targetStrike = nearestAtm;
      }
    }

    const isManual = selectedStrikeSource === 'manual' && targetStrike != null;
    const strikeSource = isRunning ? (currentSlot.strike_source || 'auto') : (isManual ? 'manual' : 'auto');

    const res = await apiFetch('/api/angel/fetch_option', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        run_id: activeSlotId,
        instrument: inst,
        option_type: optType,
        strike: targetStrike,
        strike_source: strikeSource,
      }),
    });
    const data = await res.json();
    if (data.status === 'success' && data.option_ltp != null) {
      const resolvedStrike = data.strike || targetStrike;
      const contractLabel = data.contract || `${inst} ${resolvedStrike} ${optType}`;
      if (optLtpInput) optLtpInput.value = parseFloat(data.option_ltp).toFixed(2);
      if (resOptLtpVal) resOptLtpVal.innerText = `₹${parseFloat(data.option_ltp).toFixed(2)}`;
      if (metricLiveLtp) metricLiveLtp.innerText = `₹${parseFloat(data.option_ltp).toFixed(2)}`;
      if (headerLtp) headerLtp.innerText = `₹${parseFloat(data.option_ltp).toFixed(2)}`;
      if (resStrikeVal) resStrikeVal.innerText = nearestAtm;
      const srcText = isManual ? ' [MANUAL]' : ' [AUTO]';
      if (strikeBadge) strikeBadge.innerText = (isRunning ? `LOCKED: ${resolvedStrike}` : `ATM: ${nearestAtm}`) + srcText;
      if (resContractVal) resContractVal.innerText = `Active Contract: ${contractLabel}`;
      if (headerActiveStrike) headerActiveStrike.innerText = resolvedStrike;
      if (symbolText) symbolText.innerText = contractLabel;
      if (strikeSelect && resolvedStrike) strikeSelect.value = resolvedStrike;
      updateStrikeSourceBadge();
      saveCurrentInputParams();
      if (btn) { btn.innerText = '✓ Synced'; setTimeout(() => { btn.innerText = '⚡ Auto'; btn.classList.remove('loading'); }, 1200); }
    } else {
      alert(data.message || 'Live Option LTP not available.');
      if (btn) { btn.innerText = '⚡ Auto'; btn.classList.remove('loading'); }
    }
  } catch (e) {
    console.error(e);
    if (btn) { btn.innerText = '⚡ Auto'; btn.classList.remove('loading'); }
  }
}

// ── Manual Option Tick Send ────────────────────────────────────────────────
async function sendManualOptTick() {
  if (!optLtpInput) return;
  const val = parseFloat(optLtpInput.value);
  if (isNaN(val) || val <= 0) { alert('Enter a valid Option Premium LTP.'); return; }
  try {
    await apiFetch(`/runs/${activeSlotId}/tick`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ price: val }),
    });
  } catch (e) { console.error(e); }
}

// ── Run / Stop Slicer ──────────────────────────────────────────────────────
async function runSlicer() {
  const btn = document.getElementById('btnRunSlicer');
  const runs = currentState && Array.isArray(currentState.runs) ? currentState.runs : [];
  const currentSlot = runs.find(r => r.run_id === activeSlotId) || currentState?.active_run;
  const isRunning = currentSlot && currentSlot.is_active;

  // STOP
  if (isRunning) {
    if (btn) btn.innerHTML = '<span class="pulse-indicator"></span> Stopping...';
    try {
      await apiFetch(`/runs/${activeSlotId}/stop`, { method: 'POST' });
    } catch (e) { console.error(e); }
    return;
  }

  // START – dynamic range & slicer grid inputs
  const range_points = parseFloat(rangePointsInput ? rangePointsInput.value : 0) || (currentInstrument === 'SENSEX' ? 150 : 40);
  const slicer_count = parseInt(slicerCountInput ? slicerCountInput.value : 5) || 5;
  const profit_point = parseFloat(profitPointInput.value);
  const loss_point = parseFloat(lossPointInput.value);
  const qty_per_slice_lots = parseInt(qtyPerSliceInput.value) || 1;
  const option_type = optionTypeSelect ? optionTypeSelect.value : currentSelectedSide;
  const cfg = INSTRUMENT_CONFIG[currentInstrument] || INSTRUMENT_CONFIG.NIFTY;
  let spot_ltp = parseFloat(spotInput.value) || cfg.defaultSpot;
  const isSensex = currentInstrument.toUpperCase() === 'SENSEX';
  if (isSensex && spot_ltp < 50000) spot_ltp = cfg.defaultSpot;
  if (!isSensex && spot_ltp > 50000) spot_ltp = cfg.defaultSpot;
  const strike = calculateNearestStrike(spot_ltp, currentInstrument);
  const strikeToUse = (selectedStrikeSource === 'manual' && manualSelectedStrike) ? manualSelectedStrike : strike;
  const manual_opt_ltp = optLtpInput ? parseFloat(optLtpInput.value) : null;

  if (range_points <= 0) { alert('Range (Points) must be > 0.'); return; }
  if (slicer_count < 1) { alert('Slicer count must be at least 1.'); return; }
  if ([profit_point, loss_point].some(isNaN)) {
    alert('Please fill in all Strategy Parameters.'); return;
  }

  // Derive slice interval and snapshot range_high from option LTP
  const slice_interval = range_points / slicer_count;
  const optLtpSnapshot = (manual_opt_ltp && !isNaN(manual_opt_ltp) && manual_opt_ltp > 0)
    ? manual_opt_ltp
    : (parseFloat(optLtpInput ? optLtpInput.value : 0) || (isSensex ? 250 : 140));
  const range_high = optLtpSnapshot;
  const range_low = Math.max(0, range_high - range_points);

  const activeClientId = getStoredClientId();
  if (!activeClientId || !getAuthToken()) {
    showLoginModal('Please authenticate before starting a slot.');
    return;
  }

  // Target slot id is the currently selected slot tab
  const runId = activeSlotId || 'run01';
  const canonicalCode = SLOT_CANONICAL_MAP[runId] || runId;

  // Explicit confirmation step showing client_id before starting
  const confirmed = window.confirm(`You are about to start Slot [${canonicalCode}] (${currentInstrument} ${strikeToUse} ${option_type}) under client: "${activeClientId}".\n\nProceed?`);
  if (!confirmed) {
    return;
  }

  if (btn) {
    btn.classList.add('running');
    btn.innerHTML = '<span class="pulse-indicator"></span> Starting Slicer...';
  }

  try {
    const res = await apiFetch('/api/runs/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        run_id: runId,
        instrument_name: currentInstrument,
        option_type,
        strike: strikeToUse,
        strike_source: selectedStrikeSource,
        range_points,
        slicer_count,
        range_high,
        range_low,
        slice_interval,
        profit_point,
        loss_point,
        qty_per_slice_lots,
        spot_ltp,
        manual_opt_ltp: (manual_opt_ltp && !isNaN(manual_opt_ltp) && manual_opt_ltp > 0) ? manual_opt_ltp : null,
        gap_fill_mode: 'all_crossed',
        lock_strike_on_entry: true,
        cutoff_time_ist: (eodCutoffInput && eodCutoffInput.value) ? eodCutoffInput.value : '15:22',
      }),
    });
    const data = await res.json();
    if (data.error) {
      alert('Error starting slicer: ' + data.error);
      if (btn) { btn.innerHTML = '🚀 Run Slicer'; btn.classList.remove('running'); }
    } else {
      activeSlotId = runId;
    }
  } catch (e) {
    console.error(e);
    alert('Failed to start slicer.');
    if (btn) { btn.innerHTML = '🚀 Run Slicer'; btn.classList.remove('running'); }
  }
}

async function triggerEodSquareoff() {
  const btn = document.getElementById('btnEodSquareoff');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="pulse-indicator"></span> Squaring Off...';
  }
  try {
    const res = await apiFetch(`/runs/${activeSlotId}/eod_squareoff`, { method: 'POST' });
    if (res.ok) {
      await fetchInitialState();
    }
  } catch (e) {
    console.error('EOD squareoff error:', e);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '⏹ EOD SQUAREOFF';
    }
  }
}

// ── Manual Single Slice Exit ───────────────────────────────────────────────
async function exitOpenSlice(slotId, sliceId, sliceLabel, btnEl) {
  const targetSlot = slotId || activeSlotId || 'run01';
  const name = sliceLabel ? `Slice ${sliceLabel}` : `Slice (${sliceId})`;

  if (btnEl) {
    btnEl.disabled = true;
    btnEl.innerHTML = '<span class="pulse-indicator"></span> Exiting...';
  }

  try {
    const res = await apiFetch(`/runs/${targetSlot}/slices/exit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        slice_id: String(sliceId || ''),
        label: String(sliceLabel || sliceId || ''),
        order_id: String(sliceId || '')
      }),
    });
    const data = await res.json();
    if (res.ok && (data.status === 'success' || data.trade)) {
      await fetchInitialState();
    } else if (data.error || data.message) {
      alert(`Error exiting ${name}: ${data.error || data.message}`);
      if (btnEl) {
        btnEl.disabled = false;
        btnEl.innerText = 'Exit';
      }
    }
  } catch (e) {
    console.error('Error exiting slice:', e);
    alert(`Failed to exit ${name}. Please check connection.`);
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.innerText = 'Exit';
    }
  }
}
window.exitOpenSlice = exitOpenSlice;

// ── Legacy helper for send tick via API ────────────────────────────────────
async function sendTick(ltp) {
  try {
    await apiFetch(`/runs/${activeSlotId}/tick`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ price: parseFloat(ltp) }),
    });
  } catch (e) { console.error(e); }
}

function clearLocalLogs() {
  // deprecated
}

// ── Astro Signal Engine Handling ───────────────────────────────────────────
async function handleAstroCsvSelected(inputEl) {
  if (!inputEl || !inputEl.files || inputEl.files.length === 0) return;
  const file = inputEl.files[0];
  const feedbackEl = document.getElementById('astroUploadFeedback');

  if (feedbackEl) {
    feedbackEl.className = 'astro-upload-feedback';
    feedbackEl.innerText = `Uploading ${file.name}...`;
    feedbackEl.style.display = 'block';
  }

  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await apiFetch('/api/astro/upload', {
      method: 'POST',
      body: formData,
    });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      if (feedbackEl) {
        feedbackEl.className = 'astro-upload-feedback success';
        feedbackEl.innerText = `✓ Loaded ${data.filename} (${data.row_count} rows)`;
        setTimeout(() => { feedbackEl.style.display = 'none'; }, 4000);
      }
      await refreshAstroStatus();
    } else {
      if (feedbackEl) {
        feedbackEl.className = 'astro-upload-feedback error';
        feedbackEl.innerText = `Upload failed: ${data.detail || data.error || 'Server error'}`;
      }
    }
  } catch (e) {
    console.error('Error uploading Astro CSV:', e);
    if (feedbackEl) {
      feedbackEl.className = 'astro-upload-feedback error';
      feedbackEl.innerText = `Error uploading Astro CSV: ${e.message || e}`;
    }
  } finally {
    inputEl.value = '';
  }
}

let _cachedActiveAstroFile = null;

async function deleteActiveAstroFile() {
  const activeFileId = window._currentAstroFileId || (_cachedActiveAstroFile && _cachedActiveAstroFile.file_id);
  if (!activeFileId) return;
  if (!confirm('Delete active Astro CSV file? This resets the astro signal engine for your account.')) {
    return;
  }
  try {
    window._currentAstroFileId = null;
    _cachedActiveAstroFile = null;
    const res = await apiFetch(`/api/astro/files/${activeFileId}`, { method: 'DELETE' });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      window._currentAstroFileId = null;
      _cachedActiveAstroFile = null;
      if (currentState && currentState.astro_state) {
        currentState.astro_state.filename = null;
        currentState.astro_state.file_id = null;
        currentState.astro_state.has_active_file = false;
      }
      await refreshAstroStatus();
    } else {
      alert(`Could not delete file: ${data.detail || data.error || 'Unknown error'}`);
    }
  } catch (e) {
    console.error('Delete Astro file error:', e);
  }
}

function getPersistedAstroArmed(slotId) {
  try {
    const masterVal = localStorage.getItem('slicer_astro_auto_armed_master');
    if (masterVal === '1') return true;
    if (masterVal === '0') return false;
    const key = 'slicer_astro_auto_armed_' + (slotId || activeSlotId || 'run01');
    const v = localStorage.getItem(key);
    if (v === '1') return true;
    if (v === '0') return false;
  } catch (e) { }
  return null;
}

function setPersistedAstroArmed(val, slotId) {
  try {
    localStorage.setItem('slicer_astro_auto_armed_master', val ? '1' : '0');
    const key = 'slicer_astro_auto_armed_' + (slotId || activeSlotId || 'run01');
    localStorage.setItem(key, val ? '1' : '0');
  } catch (e) { }
}

async function toggleAstroAutoExecution(enabled) {
  const isBool = Boolean(enabled);
  setPersistedAstroArmed(isBool);
  _userAstroArmed = isBool;

  const toggleEl = document.getElementById('astroAutoToggle');
  if (toggleEl) toggleEl.checked = isBool;
  const slotTagEl = document.getElementById('astroSlotTag');
  if (slotTagEl) {
    safeSetText(slotTagEl, isBool
      ? 'Astro Auto-Execution: ARMED (Call & Put Active)'
      : 'Auto-Trigger: OFF (All Slots Inactive)');
  }

  if (currentState) {
    if (!currentState.astro_state) currentState.astro_state = {};
    if (!currentState.astro_state.auto_trigger_by_slot) currentState.astro_state.auto_trigger_by_slot = {};
    ['all', 'run01', 'run02', 'run03', 'run04', 'N-C', 'N-P', 'S-C', 'S-P'].forEach(k => {
      currentState.astro_state.auto_trigger_by_slot[k] = isBool;
    });
  }

  try {
    const res = await apiFetch('/api/astro/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slot_id: 'all', enabled: isBool }),
    });
    if (res.ok) {
      const data = await res.json();
      if (currentState && currentState.astro_state && data.auto_trigger_by_slot) {
        currentState.astro_state.auto_trigger_by_slot = data.auto_trigger_by_slot;
      }
    }
  } catch (e) {
    console.error('Toggle Astro error:', e);
  }
}

async function refreshAstroStatus() {
  if (!getAuthToken()) return;
  try {
    const res = await apiFetch('/api/astro/status');
    if (res.ok) {
      const data = await res.json();
      renderAstroSection(data);
    }
  } catch (e) { }
}

function safeSetText(el, text) {
  if (el && el.innerText !== text) el.innerText = text;
}
function safeSetClass(el, cls) {
  if (el && el.className !== cls) el.className = cls;
}

function renderAstroSection(astroData) {
  if (!astroData) return;
  let activeFile = astroData.active_file;
  if (activeFile && (activeFile.filename || activeFile.file_id)) {
    _cachedActiveAstroFile = activeFile;
  } else if (astroData.has_active_file === false && !astroData.active_file) {
    _cachedActiveAstroFile = null;
  } else if (_cachedActiveAstroFile) {
    activeFile = _cachedActiveAstroFile;
  }

  const incomingPreview = astroData.preview || astroData.cluster_preview;
  if (incomingPreview && (incomingPreview.window || incomingPreview.status)) {
    window._lastAstroPreview = incomingPreview;
  }
  const preview = incomingPreview || window._lastAstroPreview || {};
  const activeSlot = activeSlotId || 'run01';

  // Active File Card
  const activeFileCard = document.getElementById('astroActiveFileCard');
  const activeFileName = document.getElementById('astroActiveFileName');
  const activeFileMeta = document.getElementById('astroActiveFileMeta');
  const astroStatusBadge = document.getElementById('astroStatusBadge');

  if (activeFile && (activeFile.filename || activeFile.file_id)) {
    window._currentAstroFileId = activeFile._id || activeFile.file_id;
    if (activeFileCard && activeFileCard.style.display !== 'flex') activeFileCard.style.display = 'flex';
    safeSetText(activeFileName, activeFile.filename || 'astro_report.csv');
    const rowCnt = activeFile.row_count || (preview.window ? preview.window.length : (preview.window_rows ? preview.window_rows.length : 0)) || 0;
    safeSetText(activeFileMeta, `${rowCnt} rows · Active in Scalper.Astro`);
    if (astroStatusBadge) {
      safeSetText(astroStatusBadge, 'Astro: Active');
      astroStatusBadge.style.color = '#34d399';
    }
  } else {
    window._currentAstroFileId = null;
    if (activeFileCard && activeFileCard.style.display !== 'none') activeFileCard.style.display = 'none';
    if (astroStatusBadge) {
      safeSetText(astroStatusBadge, 'Astro: Standby');
      astroStatusBadge.style.color = '#c084fc';
    }
  }

  // Auto Toggle Switch for Astro auto-execution — Master 4-Slot Trigger
  const toggleEl = document.getElementById('astroAutoToggle');
  const slotTagEl = document.getElementById('astroSlotTag');
  const autoMap = astroData.auto_trigger_by_slot || (currentState?.astro_state?.auto_trigger_by_slot) || {};

  const serverArmed = Boolean(
    autoMap['all'] ||
    (autoMap['run01'] && autoMap['run03']) ||
    autoMap['run01'] ||
    autoMap['N-C'] ||
    autoMap['N-P']
  );
  let isArmed;
  const localSaved = getPersistedAstroArmed();
  if (localSaved !== null) {
    isArmed = localSaved;
  } else {
    isArmed = serverArmed;
    setPersistedAstroArmed(isArmed);
  }
  _userAstroArmed = isArmed;

  if (toggleEl && toggleEl.checked !== isArmed) {
    toggleEl.checked = isArmed;
  }
  if (slotTagEl) {
    safeSetText(slotTagEl, isArmed
      ? 'Astro Auto-Execution: ARMED (Call & Put Active)'
      : 'Auto-Trigger: OFF (All Slots Inactive)');
  }

  // 3-Row Cluster Window
  const windowRows = preview.window || preview.window_rows || preview.window_summary || [];
  const evalTime = preview.evaluated_at_ist || (preview.timestamp ? formatISTTime(preview.timestamp) : formatISTTime(new Date().toISOString()));
  const clusterTimeEl = document.getElementById('astroClusterTime');
  safeSetText(clusterTimeEl, evalTime);

  for (let i = 0; i < 3; i++) {
    const timeEl = document.getElementById(`clusterTime${i}`);
    const dirEl = document.getElementById(`clusterDir${i}`);
    const r = windowRows[i];

    if (r) {
      const t = r.time || r.time_str || (r.timestamp ? r.timestamp.substring(11, 16) : '--:--');
      const rawDir = (r.normalized || r.direction || r.raw || 'Neutral').toLowerCase();
      const dirText = rawDir === 'upside' ? 'UPSIDE' : rawDir === 'downside' ? 'DOWNSIDE' : 'NEUTRAL';
      safeSetText(timeEl, t);
      safeSetText(dirEl, dirText);
      safeSetClass(dirEl, 'chip-dir ' + (dirText === 'UPSIDE' ? 'dir-upside' : dirText === 'DOWNSIDE' ? 'dir-downside' : 'dir-neutral'));
    } else {
      safeSetText(timeEl, '--:--');
      safeSetText(dirEl, 'WAITING');
      safeSetClass(dirEl, 'chip-dir dir-neutral');
    }
  }

  // Consensus Banner
  const consensusBanner = document.getElementById('astroConsensusBanner');
  const consensusIcon = document.getElementById('astroConsensusIcon');
  const consensusLabel = document.getElementById('astroConsensusLabel');

  const consensus = (preview.consensus || 'NONE').toUpperCase();
  const optType = (preview.option_type || (preview.signal && preview.signal.includes('PE') ? 'PE' : preview.signal && preview.signal.includes('CE') ? 'CE' : null) || '').toUpperCase();
  const status = preview.status;

  if (consensusBanner && consensusIcon && consensusLabel) {
    if ((status === 'APPROVED' && optType === 'CE') || (consensus === 'ALL_UPSIDE' && optType === 'CE')) {
      safeSetClass(consensusBanner, 'cluster-consensus-banner consensus-ce');
      safeSetText(consensusIcon, '🟢');
      safeSetText(consensusLabel, 'BUY CE SIGNAL CONFIRMED (All 3 Upside)');
    } else if ((status === 'APPROVED' && optType === 'PE') || (consensus === 'ALL_DOWNSIDE' && optType === 'PE')) {
      safeSetClass(consensusBanner, 'cluster-consensus-banner consensus-pe');
      safeSetText(consensusIcon, '🔴');
      safeSetText(consensusLabel, 'BUY PE SIGNAL CONFIRMED (All 3 Downside)');
    } else if (consensus === 'ALL_UPSIDE' || (optType === 'CE' && status !== 'NO_SIGNAL')) {
      safeSetClass(consensusBanner, 'cluster-consensus-banner consensus-ce');
      safeSetText(consensusIcon, '🟢');
      safeSetText(consensusLabel, `Consensus BUY CE (3 Upside) · [${preview.reason || preview.filter || 'Pending Filter'}]`);
    } else if (consensus === 'ALL_DOWNSIDE' || (optType === 'PE' && status !== 'NO_SIGNAL')) {
      safeSetClass(consensusBanner, 'cluster-consensus-banner consensus-pe');
      safeSetText(consensusIcon, '🔴');
      safeSetText(consensusLabel, `Consensus BUY PE (3 Downside) · [${preview.reason || preview.filter || 'Pending Filter'}]`);
    } else {
      safeSetClass(consensusBanner, 'cluster-consensus-banner consensus-neutral');
      safeSetText(consensusIcon, '⚪');
      safeSetText(consensusLabel, (activeFile && (activeFile.filename || activeFile.file_id))
        ? 'Cluster Mixed / Waiting for 3 matching rows'
        : 'Upload weekly Astro CSV to activate signal window');
    }
  }

  // Filter Pills
  const filterHours = document.getElementById('filterHoursPill');
  const filterTrade = document.getElementById('filterTradePill');

  if (filterHours) {
    if (preview.filter === 'trading_hours') {
      filterHours.className = 'filter-pill pill-blocked';
      filterHours.innerText = 'OUTSIDE HOURS';
    } else {
      filterHours.className = 'filter-pill pill-pass';
      filterHours.innerText = 'ACTIVE (09:16-Cutoff)';
    }
  }

  if (filterTrade) {
    if (preview.filter === 'active_trade') {
      filterTrade.className = 'filter-pill pill-blocked';
      filterTrade.innerText = 'ACTIVE TRADE OPEN';
    } else {
      filterTrade.className = 'filter-pill pill-pass';
      filterTrade.innerText = 'CLEAN';
    }
  }
}

// ── Init ────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  updateUserBadge();
  loadInstrumentParams(currentInstrument);

  // Instantly apply saved master toggle preference to DOM before any network tick
  const savedArmed = getPersistedAstroArmed();
  if (savedArmed !== null) {
    const toggleEl = document.getElementById('astroAutoToggle');
    if (toggleEl) toggleEl.checked = savedArmed;
    const slotTagEl = document.getElementById('astroSlotTag');
    if (slotTagEl) {
      safeSetText(slotTagEl, savedArmed
        ? 'Astro Auto-Execution: ARMED (Call & Put Active)'
        : 'Auto-Trigger: OFF (All Slots Inactive)');
    }
  }

  const token = getAuthToken();
  if (!token) {
    showLoginModal();
    return;
  }

  // Validate cached token with backend to prevent stale / mismatched sessions
  fetch('/auth/me', {
    headers: { 'Authorization': `Bearer ${token}` }
  }).then(async (res) => {
    if (res.ok) {
      const meData = await res.json();
      if (meData.client_id) {
        setAuthToken(token, meData.client_id);
        updateUserBadge();
        connectSSE();
        fetchInitialState();
        refreshAstroStatus();
        setInterval(refreshAstroStatus, 5000);
        if (savedArmed !== null) {
          toggleAstroAutoExecution(savedArmed);
        }
        return;
      }
    }
    removeAuthToken();
    showLoginModal('Session expired. Please sign in.');
  }).catch(() => {
    removeAuthToken();
    showLoginModal('Could not verify session. Please sign in.');
  });
});

// ── Angel One Live Order History Modal ─────────────────────────────────────
async function openBrokerOrdersModal() {
  const modal = document.getElementById('brokerOrdersModal');
  if (modal) {
    modal.style.display = 'flex';
    await refreshBrokerOrders();
  }
}

function closeBrokerOrdersModal() {
  const modal = document.getElementById('brokerOrdersModal');
  if (modal) {
    modal.style.display = 'none';
  }
}

async function refreshBrokerOrders() {
  const tbody = document.getElementById('brokerOrdersTableBody');
  if (!tbody) return;

  tbody.innerHTML = `
    <tr class="empty-table-row empty-row">
      <td colspan="8"><span class="pulse-indicator"></span> Querying Angel One SmartAPI Order Book...</td>
    </tr>
  `;

  try {
    const res = await apiFetch('/api/broker/orders');
    const data = await res.json();
    const orders = (data && Array.isArray(data.orders)) ? data.orders : [];

    if (orders.length === 0) {
      tbody.innerHTML = `
        <tr class="empty-table-row empty-row">
          <td colspan="8">No orders found on Angel One today. (Mode: ${(data.trading_mode || 'paper').toUpperCase()})</td>
        </tr>
      `;
      return;
    }

    let rowsHtml = '';
    orders.forEach(o => {
      const isBuy = (o.transaction_type || 'BUY').toUpperCase() === 'BUY';
      const sideTag = isBuy
        ? '<span class="badge-opt-ce" style="background:rgba(16,185,129,0.15);color:#10b981;padding:2px 8px;border-radius:4px;font-weight:700;">BUY</span>'
        : '<span class="badge-opt-pe" style="background:rgba(244,63,94,0.15);color:#f43f5e;padding:2px 8px;border-radius:4px;font-weight:700;">SELL</span>';

      const status = (o.order_status || 'UNKNOWN').toUpperCase();
      let statusColor = '#94a3b8';
      if (status === 'COMPLETE' || status === 'FILLED') statusColor = '#10b981';
      else if (status === 'REJECTED' || status === 'CANCELLED') statusColor = '#ef4444';
      else if (status === 'OPEN' || status === 'PENDING') statusColor = '#06b6d4';

      const statusTag = `<span style="color:${statusColor};font-weight:700;">${status}</span>`;
      const priceStr = (o.price && o.price > 0) ? `₹${Number(o.price).toFixed(2)}` : 'MARKET';
      const fillQty = o.filled_quantity != null ? `${o.filled_quantity}/${o.quantity}` : `${o.quantity}`;

      rowsHtml += `
        <tr>
          <td style="font-family:var(--font-mono);font-size:0.75rem;color:var(--accent-cyan);font-weight:700;">${o.order_id || '—'}</td>
          <td style="font-family:var(--font-mono);font-weight:600;color:var(--accent-amber);">${o.tradingsymbol || '—'}</td>
          <td style="font-size:0.75rem;color:var(--text-muted);">${o.exchange || 'NFO'}</td>
          <td>${sideTag}</td>
          <td>${fillQty}</td>
          <td style="font-weight:700;">${priceStr}</td>
          <td>${statusTag}</td>
          <td style="font-size:0.75rem;color:var(--text-muted);">${o.updatetime || '—'}</td>
        </tr>
      `;
    });

    tbody.innerHTML = rowsHtml;
  } catch (err) {
    console.error('Error fetching broker orders:', err);
    tbody.innerHTML = `
      <tr class="empty-table-row empty-row">
        <td colspan="8" style="color:var(--accent-rose);">Failed to retrieve orders from Angel One: ${err.message}</td>
      </tr>
    `;
  }
}

window.openBrokerOrdersModal = openBrokerOrdersModal;
window.closeBrokerOrdersModal = closeBrokerOrdersModal;
window.refreshBrokerOrders = refreshBrokerOrders;


