// stake-proxy Worker — transparent proxy from your Worker URL to stake.com / stake.bet
//
// Why this works:
//   Cloudflare Workers run on CF's own network. fetch() from a Worker to a
//   CF-protected site (like stake.com) bypasses the IP-based bot scoring that
//   blocks datacenter IPs. The bot points at your Worker URL instead of stake.com.
//
// Cost: free tier = 100,000 requests/day, more than enough for any betting bot.
//
// Deployment (5 minutes):
//   1. Sign up free at https://dash.cloudflare.com/sign-up
//   2. Go to Workers & Pages → Create → Hello World template → Deploy
//   3. Edit the worker code: paste the contents of THIS file
//   4. Deploy. Note your URL: https://<name>.<account>.workers.dev
//   5. Set in the bot:  /set api_base https://<name>.<account>.workers.dev
//      (or use the wizard's API base override prompt)
//
// Security: this Worker is a public proxy — keep the URL secret. Anyone with
// the URL can hit stake.com through your Worker. The Worker forwards your
// access tokens unchanged, so a leaked Worker URL doesn't leak your tokens
// (those still come from the bot), but you'll burn through your free quota.

export default {
    async fetch(request) {
        const url = new URL(request.url);

        // Map paths onto the right upstream:
        //   /bet/...     -> stake.bet
        //   anything else -> stake.com
        let upstream;
        if (url.pathname.startsWith("/bet/")) {
            upstream = "https://stake.bet" + url.pathname.slice(4) + url.search;
        } else {
            upstream = "https://stake.com" + url.pathname + url.search;
        }

        // Forward the request, swapping Host
        const headers = new Headers(request.headers);
        headers.delete("Host");
        headers.delete("CF-Connecting-IP");
        headers.delete("CF-IPCountry");
        headers.delete("CF-Ray");
        headers.delete("CF-Visitor");
        headers.delete("X-Forwarded-For");
        headers.delete("X-Forwarded-Proto");

        const init = {
            method: request.method,
            headers,
            body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
            redirect: "manual",
        };

        const upstreamResp = await fetch(upstream, init);

        // Pass through response, stripping CF-specific headers
        const respHeaders = new Headers(upstreamResp.headers);
        respHeaders.delete("Set-Cookie");  // Strip if you don't want CF/stake cookies leaking
        // Actually we DO want them passed through:
        const setCookies = upstreamResp.headers.getAll
            ? upstreamResp.headers.getAll("Set-Cookie")
            : (upstreamResp.headers.get("Set-Cookie") ? [upstreamResp.headers.get("Set-Cookie")] : []);
        respHeaders.delete("Set-Cookie");
        for (const sc of setCookies) {
            respHeaders.append("Set-Cookie", sc);
        }

        return new Response(upstreamResp.body, {
            status: upstreamResp.status,
            statusText: upstreamResp.statusText,
            headers: respHeaders,
        });
    },
};
