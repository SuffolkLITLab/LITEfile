const documentRequests = (() => {
    class RequestError extends Error {
        constructor(message, kind = "rejected") {
            super(message);
            this.kind = kind;
        }
    }

    async function json(url, fallback, options = {}) {
        const headers = new Headers(options.headers);
        headers.set("Accept", "application/json");
        let response;
        try {
            response = await fetch(url, {
                ...options,
                headers
            });
        } catch {
            throw new RequestError(fallback, "uncertain");
        }
        if (response.status === 401 || (response.redirected && /\/login\/?$/.test(new URL(response.url).pathname))) {
            throw new RequestError("Sign in again, then reopen this filing from My drafts.", "reopen");
        }
        return readResponse(response, fallback);
    }

    async function readResponse(response, fallback) {
        const contentType = response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase();
        const data = contentType === "application/json" ? await response.json().catch(() => null) : null;
        if (response.status === 409 && (!data || data.error_code === "draft_unavailable")) {
            throw new RequestError("This draft is no longer available here. Open My drafts to choose a filing.", "reopen");
        }
        if (response.status === 403 && !data) {
            throw new RequestError("Reopen this filing from My drafts before continuing. Sign in again if prompted.", "reopen");
        }
        if (response.status === 413 && !data) {
            throw new RequestError("These files are too large to send together. Try uploading fewer or smaller files.");
        }
        if (response.status === 429 || (response.status >= 500 && data?.error_code !== "workers_unavailable")) {
            throw new RequestError(fallback, "uncertain");
        }
        if (!response.ok || data?.success === false) {
            throw new RequestError(typeof data?.error === "string" && data.error ? data.error : fallback);
        }
        if (data?.success !== true) throw new RequestError(fallback, "uncertain");
        return data;
    }

    function status(url, fallback) {
        return json(url, fallback, {
            signal: AbortSignal.timeout(10000)
        });
    }

    return {
        json,
        status
    };
})();

if (typeof module !== "undefined" && module.exports) module.exports = documentRequests;