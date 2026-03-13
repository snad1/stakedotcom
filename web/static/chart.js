/**
 * Stake AutoBot — Trading-style balance chart
 * Powered by Lightweight Charts (TradingView)
 *
 * X-axis uses bet IDs (unique ascending integers) so every point is
 * guaranteed unique — no duplicate-timestamp issues with rapid bets.
 */

(function () {
    'use strict';

    const isDark = () => document.documentElement.classList.contains('dark');

    const COLORS = {
        win:       '#22c55e',
        loss:      '#ef4444',
        lineAbove: '#22c55e',
        lineBelow: '#ef4444',
        baseline:  '#6b7280',
    };

    function chartTheme() {
        const dark = isDark();
        return {
            layout: {
                background: { color: dark ? '#1f2937' : '#ffffff' },
                textColor:  dark ? '#9ca3af' : '#374151',
                fontSize:   11,
            },
            grid: {
                vertLines: { color: dark ? '#374151' : '#f3f4f6' },
                horzLines: { color: dark ? '#374151' : '#f3f4f6' },
            },
            rightPriceScale: {
                borderColor: dark ? '#374151' : '#e5e7eb',
            },
            timeScale: {
                borderColor: dark ? '#374151' : '#e5e7eb',
                timeVisible: false,
                tickMarkFormatter: (id) => '#' + id,
            },
            crosshair: {
                mode: 1,
                vertLine: { color: dark ? '#4b5563' : '#9ca3af', labelBackgroundColor: dark ? '#374151' : '#6b7280' },
                horzLine: { color: dark ? '#4b5563' : '#9ca3af', labelBackgroundColor: dark ? '#374151' : '#6b7280' },
            },
        };
    }

    // ── State ─────────────────────────────────────────────────────────────────
    let chart      = null;
    let lineSeries = null;
    let chartData  = [];
    let startValue = null;
    let wsClient   = null;
    let autoScroll = true;

    // ── Fast lookup: build a Map from time (bet id) → data point ────────────
    let dataMap = new Map();

    function rebuildMap() {
        dataMap = new Map();
        for (const d of chartData) dataMap.set(d.time, d);
    }

    // ── Range buttons ───────────────────────────────────────────────────────
    // "10m"/"30m"/"1h" show last N points (roughly matching bet counts)
    // since x-axis is bet IDs, we estimate ~2 bets/sec
    window.chartSetRange = function (range) {
        if (!chart || chartData.length === 0) return;

        document.querySelectorAll('.chart-range-btn').forEach(b => {
            b.classList.remove('bg-brand-600', 'text-white');
            b.classList.add('bg-gray-100', 'dark:bg-gray-700', 'text-gray-600', 'dark:text-gray-300');
        });
        const active = document.getElementById(`range-btn-${range}`);
        if (active) {
            active.classList.add('bg-brand-600', 'text-white');
            active.classList.remove('bg-gray-100', 'dark:bg-gray-700', 'text-gray-600', 'dark:text-gray-300');
        }

        if (range === 'all') {
            chart.timeScale().fitContent();
            return;
        }

        // Show last N points
        const counts = { '10m': 200, '30m': 500, '1h': 1000 };
        const n = counts[range] || 500;
        const start = Math.max(0, chartData.length - n);
        chart.timeScale().setVisibleRange({
            from: chartData[start].time,
            to:   chartData[chartData.length - 1].time + 1,
        });
    };

    // ── Tooltip ──────────────────────────────────────────────────────────────
    function setupTooltip(container) {
        const tooltip = document.getElementById('chart-tooltip');
        if (!tooltip || !lineSeries) return;

        chart.subscribeCrosshairMove((param) => {
            if (!param.point || !param.seriesData || !param.seriesData.size) {
                tooltip.classList.add('hidden');
                return;
            }
            const sd = param.seriesData.get(lineSeries);
            if (!sd) { tooltip.classList.add('hidden'); return; }

            const value = sd.value;
            const meta  = dataMap.get(sd.time);

            const dark = isDark();
            const sub  = dark ? '#9ca3af' : '#6b7280';
            const main = dark ? '#f3f4f6' : '#111827';
            const border = dark ? '#374151' : '#e5e7eb';

            const timeStr = meta ? meta.ts : '';
            const profitColor = meta && meta.profit >= 0 ? 'color:#22c55e' : 'color:#ef4444';
            const stateLabel  = meta ? (meta.state === 'win' ? '✓ Win' : '✗ Loss') : '';
            const stateColor  = meta && meta.state === 'win' ? 'color:#22c55e' : 'color:#ef4444';

            tooltip.innerHTML = `
                <div style="font-size:11px;margin-bottom:4px;color:${sub}">${timeStr}${meta ? ' · Bet #' + meta.bet_num : ''}</div>
                <div style="font-size:13px;font-weight:600;color:${main}">Balance: ${value.toFixed(8)}</div>
                ${meta ? `
                <div style="margin-top:4px;border-top:1px solid ${border};padding-top:4px">
                    <div><span style="color:${sub}">Amount: </span>${meta.amount.toFixed(8)}</div>
                    <div><span style="color:${sub}">Target: </span>${meta.target}x</div>
                    <div><span style="color:${sub}">Result: </span>${meta.result}</div>
                    <div><span style="color:${sub}">Profit: </span><span style="${profitColor}">${meta.profit >= 0 ? '+' : ''}${meta.profit.toFixed(8)}</span></div>
                    <div style="${stateColor};font-weight:600">${stateLabel}</div>
                </div>` : ''}
            `;

            const rect = container.getBoundingClientRect();
            let x = param.point.x + rect.left + 12;
            let y = param.point.y + rect.top - 10;
            if (x + 200 > window.innerWidth)  x -= 210;
            if (y + 200 > window.innerHeight) y -= 180;

            tooltip.style.left = x + 'px';
            tooltip.style.top  = y + 'px';
            tooltip.classList.remove('hidden');
            tooltip.style.position = 'fixed';
        });
    }

    // ── Markers (only for small datasets) ───────────────────────────────────
    const MARKER_LIMIT = 500;

    function applyMarkers() {
        if (!lineSeries || chartData.length > MARKER_LIMIT) {
            if (lineSeries) lineSeries.setMarkers([]);
            return;
        }
        const markers = chartData
            .filter(d => d.state)
            .map(d => ({
                time:     d.time,
                position: d.state === 'win' ? 'aboveBar' : 'belowBar',
                color:    d.state === 'win' ? COLORS.win : COLORS.loss,
                shape:    'circle',
                size:     0.6,
            }));
        lineSeries.setMarkers(markers);
    }

    // ── Line colour ─────────────────────────────────────────────────────────
    function applyLineColour() {
        if (!lineSeries || chartData.length === 0) return;
        const latest = chartData[chartData.length - 1].value;
        const base   = startValue !== null ? startValue : chartData[0].value;
        lineSeries.applyOptions({
            color:     latest >= base ? COLORS.lineAbove : COLORS.lineBelow,
            lineWidth: 2,
        });
    }

    // ── Initialize chart ────────────────────────────────────────────────────
    function initChart() {
        const container = document.getElementById('balance-chart');
        if (!container || typeof LightweightCharts === 'undefined') return;

        const uid    = container.dataset.userId;
        const sid    = container.dataset.sessionId;
        const isLive = container.dataset.isLive === 'true';

        chart = LightweightCharts.createChart(container, {
            ...chartTheme(),
            width:  container.clientWidth,
            height: container.clientHeight,
            handleScale:  { axisPressedMouseMove: true, mouseWheel: true },
            handleScroll: true,
        });

        lineSeries = chart.addAreaSeries({
            lineColor:   COLORS.lineAbove,
            topColor:    'rgba(34,197,94,0.15)',
            bottomColor: 'rgba(34,197,94,0.0)',
            lineWidth:   2,
            crosshairMarkerVisible: true,
            crosshairMarkerRadius:  4,
            priceFormat: { type: 'custom', formatter: v => v.toFixed(8) },
        });

        setupTooltip(container);

        // Load data
        fetch(`/api/users/${uid}/sessions/${sid}/chart`, { credentials: 'include' })
            .then(r => r.ok ? r.json() : [])
            .then(data => {
                if (!Array.isArray(data) || data.length === 0) {
                    if (isLive) connectLiveWS(uid, sid);
                    return;
                }
                chartData  = data;
                startValue = data[0].value;
                rebuildMap();

                lineSeries.setData(data.map(d => ({ time: d.time, value: d.value })));
                applyMarkers();

                lineSeries.createPriceLine({
                    price: startValue, color: COLORS.baseline,
                    lineWidth: 1, lineStyle: 2,
                    axisLabelVisible: true, title: 'start',
                });

                applyLineColour();
                chart.timeScale().fitContent();

                if (isLive) connectLiveWS(uid, sid);
            })
            .catch(() => { if (isLive) connectLiveWS(uid, sid); });

        // Responsive
        new ResizeObserver(() => {
            if (chart) chart.resize(container.clientWidth, container.clientHeight);
        }).observe(container);

        // Dark-mode observer
        new MutationObserver(() => {
            if (chart) chart.applyOptions(chartTheme());
        }).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    }

    // ── Live WebSocket ──────────────────────────────────────────────────────
    function connectLiveWS(uid, sid) {
        const token = getCookie('stake_admin_token') || getCookie('stake_token');
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url   = `${proto}//${location.host}/ws/session/${uid}/${sid}?token=${token}`;

        wsClient = new WebSocket(url);
        wsClient.onopen  = () => updateWsIndicator(true);
        wsClient.onclose = () => { updateWsIndicator(false); setTimeout(() => connectLiveWS(uid, sid), 3000); };
        wsClient.onerror = () => {};
        wsClient.onmessage = (evt) => {
            try {
                const msg = JSON.parse(evt.data);
                if (msg.type === 'new_bet') appendBet(msg.data);
            } catch (e) {}
        };
    }

    function appendBet(point) {
        if (!lineSeries) return;
        if (chartData.length > 0 && chartData[chartData.length - 1].time >= point.time) return;

        chartData.push(point);
        dataMap.set(point.time, point);
        if (startValue === null) startValue = point.value;

        lineSeries.update({ time: point.time, value: point.value });
        applyMarkers();
        applyLineColour();

        if (autoScroll && chart) chart.timeScale().scrollToRealTime();
    }

    function updateWsIndicator(connected) {
        const dot   = document.getElementById('ws-status');
        const label = document.getElementById('ws-label');
        if (dot && label) {
            dot.className     = 'ws-indicator ' + (connected ? 'ws-connected' : 'ws-disconnected');
            label.textContent = connected ? 'live' : 'offline';
        }
    }

    // ── Boot ────────────────────────────────────────────────────────────────
    function boot() {
        if (document.getElementById('balance-chart')) {
            if (typeof LightweightCharts !== 'undefined') initChart();
            else {
                const s = document.getElementById('lw-charts-script');
                if (s) s.addEventListener('load', initChart);
            }
        }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
    else boot();
})();
