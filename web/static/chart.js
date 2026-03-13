/**
 * Stake AutoBot — Trading-style balance chart
 * Powered by Lightweight Charts (TradingView)
 */

(function () {
    'use strict';

    // ── Colour helpers ────────────────────────────────────────────────────────
    const isDark = () => document.documentElement.classList.contains('dark');

    const COLORS = {
        win:       '#22c55e',  // green-500
        loss:      '#ef4444',  // red-500
        lineAbove: '#22c55e',
        lineBelow: '#ef4444',
        baseline:  '#6b7280',  // gray-500
    };

    function chartTheme() {
        const dark = isDark();
        return {
            layout: {
                background:  { color: dark ? '#1f2937' : '#ffffff' },   // gray-800 / white
                textColor:   dark ? '#9ca3af' : '#374151',              // gray-400 / gray-700
                fontSize:    11,
            },
            grid: {
                vertLines: { color: dark ? '#374151' : '#f3f4f6' },    // gray-700 / gray-100
                horzLines: { color: dark ? '#374151' : '#f3f4f6' },
            },
            rightPriceScale: {
                borderColor: dark ? '#374151' : '#e5e7eb',
            },
            timeScale: {
                borderColor:      dark ? '#374151' : '#e5e7eb',
                timeVisible:      true,
                secondsVisible:   false,
                tickMarkFormatter: (time) => {
                    const d = new Date(time * 1000);
                    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                },
            },
            crosshair: {
                mode: 1, // MagnetToData
                vertLine: { color: dark ? '#4b5563' : '#9ca3af', labelBackgroundColor: dark ? '#374151' : '#6b7280' },
                horzLine: { color: dark ? '#4b5563' : '#9ca3af', labelBackgroundColor: dark ? '#374151' : '#6b7280' },
            },
        };
    }

    // ── State ─────────────────────────────────────────────────────────────────
    let chart       = null;
    let lineSeries  = null;
    let chartData   = [];   // [{time, value, bet_num, amount, profit, state, target, result}]
    let startValue  = null; // balance at very first bet — used to colour line green/red
    let wsClient    = null;
    let autoScroll  = true;

    // ── Range button helpers ──────────────────────────────────────────────────
    window.chartSetRange = function (range) {
        if (!chart) return;
        document.querySelectorAll('.chart-range-btn').forEach(b => {
            b.classList.remove('bg-brand-600', 'text-white');
            b.classList.add('bg-gray-100', 'dark:bg-gray-700', 'text-gray-600', 'dark:text-gray-300');
        });
        const active = document.getElementById(`range-btn-${range}`);
        if (active) {
            active.classList.add('bg-brand-600', 'text-white');
            active.classList.remove('bg-gray-100', 'dark:bg-gray-700', 'text-gray-600', 'dark:text-gray-300');
        }

        if (range === 'all' || chartData.length === 0) {
            chart.timeScale().fitContent();
            return;
        }
        const minutes = { '10m': 10, '30m': 30, '1h': 60 }[range] || 60;
        const lastTime = chartData[chartData.length - 1].time;
        chart.timeScale().setVisibleRange({
            from: lastTime - minutes * 60,
            to:   lastTime + 30,
        });
    };

    // ── Tooltip ───────────────────────────────────────────────────────────────
    function setupTooltip(container) {
        const tooltip = document.getElementById('chart-tooltip');
        if (!tooltip || !lineSeries) return;

        chart.subscribeCrosshairMove((param) => {
            if (!param.point || !param.seriesData || !param.seriesData.size) {
                tooltip.classList.add('hidden');
                return;
            }
            const seriesData = param.seriesData.get(lineSeries);
            if (!seriesData) { tooltip.classList.add('hidden'); return; }

            const time  = seriesData.time;
            const value = seriesData.value;
            const meta  = chartData.find(d => d.time === time);

            const d = new Date(time * 1000);
            const timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

            const profitColor = meta && meta.profit >= 0 ? 'color:#22c55e' : 'color:#ef4444';
            const stateLabel  = meta ? (meta.state === 'win' ? '✓ Win' : '✗ Loss') : '';
            const stateColor  = meta && meta.state === 'win' ? 'color:#22c55e' : 'color:#ef4444';

            tooltip.innerHTML = `
                <div style="font-size:11px;margin-bottom:4px;color:${isDark() ? '#9ca3af' : '#6b7280'}">${timeStr}${meta ? ' · Bet #' + meta.bet_num : ''}</div>
                <div style="font-size:13px;font-weight:600;${isDark() ? 'color:#f3f4f6' : 'color:#111827'}">Balance: ${value.toFixed(8)}</div>
                ${meta ? `
                <div style="margin-top:4px;border-top:1px solid ${isDark() ? '#374151' : '#e5e7eb'};padding-top:4px">
                    <div><span style="color:${isDark() ? '#9ca3af' : '#6b7280'}">Amount: </span>${meta.amount.toFixed(8)}</div>
                    <div><span style="color:${isDark() ? '#9ca3af' : '#6b7280'}">Target: </span>${meta.target}x</div>
                    <div><span style="color:${isDark() ? '#9ca3af' : '#6b7280'}">Result: </span>${meta.result}</div>
                    <div><span style="color:${isDark() ? '#9ca3af' : '#6b7280'}">Profit: </span><span style="${profitColor}">${meta.profit >= 0 ? '+' : ''}${meta.profit.toFixed(8)}</span></div>
                    <div style="${stateColor};font-weight:600">${stateLabel}</div>
                </div>` : ''}
            `;

            const rect = container.getBoundingClientRect();
            let x = param.point.x + rect.left + 12;
            let y = param.point.y + rect.top - 10;

            // Keep tooltip within viewport
            if (x + 200 > window.innerWidth) x -= 210;
            if (y + 200 > window.innerHeight) y -= 180;

            tooltip.style.left = x + 'px';
            tooltip.style.top  = y + 'px';
            tooltip.classList.remove('hidden');
            tooltip.style.position = 'fixed';
        });
    }

    // ── Build series data with win/loss markers ───────────────────────────────
    function buildMarkers(data) {
        return data
            .filter(d => d.state)
            .map(d => ({
                time:     d.time,
                position: d.state === 'win' ? 'aboveBar' : 'belowBar',
                color:    d.state === 'win' ? COLORS.win : COLORS.loss,
                shape:    d.state === 'win' ? 'circle' : 'circle',
                size:     0.6,
            }));
    }

    // ── Colour the line green when above start, red when below ───────────────
    function applyLineColour() {
        if (!lineSeries || chartData.length === 0) return;
        const latest = chartData[chartData.length - 1].value;
        const base   = startValue !== null ? startValue : chartData[0].value;
        lineSeries.applyOptions({
            color:     latest >= base ? COLORS.lineAbove : COLORS.lineBelow,
            lineWidth: 2,
        });
    }

    // ── Initialize chart ──────────────────────────────────────────────────────
    function initChart() {
        const container = document.getElementById('balance-chart');
        if (!container || typeof LightweightCharts === 'undefined') return;

        const uid       = container.dataset.userId;
        const sid       = container.dataset.sessionId;
        const isLive    = container.dataset.isLive === 'true';

        // Create chart
        chart = LightweightCharts.createChart(container, {
            ...chartTheme(),
            width:  container.clientWidth,
            height: container.clientHeight,
            handleScale: { axisPressedMouseMove: true, mouseWheel: true },
            handleScroll: true,
        });

        // Area/line series
        lineSeries = chart.addAreaSeries({
            lineColor:        COLORS.lineAbove,
            topColor:         'rgba(34,197,94,0.15)',
            bottomColor:      'rgba(34,197,94,0.0)',
            lineWidth:        2,
            crosshairMarkerVisible: true,
            crosshairMarkerRadius:  4,
            priceFormat: { type: 'custom', formatter: v => v.toFixed(8) },
        });

        // Baseline (starting balance reference)
        const priceLine = { price: 0, color: COLORS.baseline, lineWidth: 1, lineStyle: 2, axisLabelVisible: false };
        lineSeries._baselinePriceLine = priceLine; // stash for later update

        // Tooltip
        setupTooltip(container);

        // ── Load historical data ──────────────────────────────────────────────
        fetch(`/api/users/${uid}/sessions/${sid}/chart`, { credentials: 'include' })
            .then(r => r.ok ? r.json() : [])
            .then(data => {
                if (!Array.isArray(data) || data.length === 0) return;
                chartData = data;
                startValue = data[0].value;

                lineSeries.setData(data.map(d => ({ time: d.time, value: d.value })));
                lineSeries.setMarkers(buildMarkers(data));

                // Baseline at starting balance
                lineSeries.createPriceLine({
                    price: startValue,
                    color: COLORS.baseline,
                    lineWidth: 1,
                    lineStyle: 2,  // dashed
                    axisLabelVisible: true,
                    title: 'start',
                });

                applyLineColour();
                chart.timeScale().fitContent();

                // Live mode: connect WebSocket for real-time updates
                if (isLive) {
                    connectLiveWS(uid, sid);
                }
            })
            .catch(() => {
                if (isLive) connectLiveWS(uid, sid);
            });

        // ── Responsive resize ─────────────────────────────────────────────────
        const ro = new ResizeObserver(() => {
            if (chart) chart.resize(container.clientWidth, container.clientHeight);
        });
        ro.observe(container);

        // ── Dark-mode observer ────────────────────────────────────────────────
        const htmlEl = document.documentElement;
        const mo = new MutationObserver(() => {
            if (chart) chart.applyOptions(chartTheme());
        });
        mo.observe(htmlEl, { attributes: true, attributeFilter: ['class'] });
    }

    // ── Live WebSocket ────────────────────────────────────────────────────────
    function connectLiveWS(uid, sid) {
        const token = getCookie('stake_admin_token') || getCookie('stake_token');
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url   = `${proto}//${location.host}/ws/session/${uid}/${sid}?token=${token}`;

        wsClient = new WebSocket(url);
        wsClient.onopen = () => {
            updateWsIndicator(true);
        };
        wsClient.onclose = () => {
            updateWsIndicator(false);
            // Reconnect after 3 s
            setTimeout(() => connectLiveWS(uid, sid), 3000);
        };
        wsClient.onerror = () => {};
        wsClient.onmessage = (evt) => {
            try {
                const msg = JSON.parse(evt.data);
                if (msg.type === 'new_bet') {
                    appendBet(msg.data);
                }
                // session_update: could refresh stats panel (future enhancement)
            } catch (e) {}
        };
    }

    function appendBet(point) {
        if (!lineSeries) return;

        // Deduplicate: skip if time already present (can happen on reconnect overlap)
        if (chartData.length > 0 && chartData[chartData.length - 1].time === point.time) return;

        chartData.push(point);
        if (startValue === null) {
            startValue = point.value;
        }

        // Update the series
        lineSeries.update({ time: point.time, value: point.value });

        // Append marker
        const markers = buildMarkers(chartData);
        lineSeries.setMarkers(markers);

        applyLineColour();

        // Auto-scroll to latest if user hasn't manually scrolled away
        if (autoScroll && chart) {
            chart.timeScale().scrollToRealTime();
        }
    }

    // ── WS indicator in page header ───────────────────────────────────────────
    function updateWsIndicator(connected) {
        const dot   = document.getElementById('ws-status');
        const label = document.getElementById('ws-label');
        if (dot && label) {
            dot.className   = 'ws-indicator ' + (connected ? 'ws-connected' : 'ws-disconnected');
            label.textContent = connected ? 'live' : 'offline';
        }
    }

    // ── Boot ──────────────────────────────────────────────────────────────────
    function boot() {
        if (document.getElementById('balance-chart')) {
            if (typeof LightweightCharts !== 'undefined') {
                initChart();
            } else {
                // LW Charts CDN not yet loaded — wait for it
                const s = document.getElementById('lw-charts-script');
                if (s) s.addEventListener('load', initChart);
            }
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
