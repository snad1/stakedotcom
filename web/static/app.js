/* Stake Admin Web App — Client-side JS */

// ── Cookie helper ──
function getCookie(name) {
    const m = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return m ? m.pop() : '';
}

// ── HTMX response handler ──
document.addEventListener('htmx:afterRequest', function(evt) {
    if (evt.detail.successful) {
        // Show a brief success toast
        showToast('Action completed successfully', 'success');
    } else if (evt.detail.failed) {
        showToast('Action failed. Check logs.', 'error');
    }
});

// ── Toast notifications ──
function showToast(message, type = 'info') {
    const colors = {
        success: 'bg-green-600',
        error: 'bg-red-600',
        info: 'bg-blue-600',
    };
    const toast = document.createElement('div');
    toast.className = `fixed bottom-4 right-4 ${colors[type] || colors.info} text-white px-4 py-2.5 rounded-lg shadow-lg text-sm font-medium z-50 fade-in`;
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ── Number formatting ──
function formatNumber(n) {
    return n.toLocaleString();
}

function formatCrypto(n, decimals = 8) {
    return parseFloat(n).toFixed(decimals);
}

// ── WebSocket helpers ──
class WSClient {
    constructor(path, onMessage, onStatus) {
        this.path = path;
        this.onMessage = onMessage;
        this.onStatus = onStatus || (() => {});
        this.ws = null;
        this.retries = 0;
        this.maxRetries = 10;
    }

    connect() {
        const token = getCookie('stake_admin_token');
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${proto}//${location.host}${this.path}?token=${token}`;

        this.ws = new WebSocket(url);
        this.ws.onopen = () => {
            this.retries = 0;
            this.onStatus('connected');
        };
        this.ws.onclose = () => {
            this.onStatus('disconnected');
            if (this.retries < this.maxRetries) {
                this.retries++;
                setTimeout(() => this.connect(), Math.min(1000 * this.retries, 10000));
            }
        };
        this.ws.onerror = () => {};
        this.ws.onmessage = (e) => {
            try {
                const msg = JSON.parse(e.data);
                this.onMessage(msg);
            } catch(err) {}
        };
    }

    close() {
        this.maxRetries = 0;
        if (this.ws) this.ws.close();
    }
}

// ── Time formatting ──
function timeAgo(isoString) {
    if (!isoString) return '—';
    const diff = (Date.now() - new Date(isoString).getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
}
