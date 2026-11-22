/**
 * API Utilities - Common functions for API communication
 * Features: CSRF handling, request building, error handling
 */
class ApiUtils {
    // Fee quotes can exceed the generic 30-second request budget at some courts.
    static FEE_TIMEOUT_MS = 120000;

    // Filing submission may involve several downstream court operations. Keep
    // this longer than the server's default 300-second read timeout so the server
    // reports the outcome before the browser gives up. The browser-side timer
    // does not abort an in-flight filing request.
    static SUBMISSION_TIMEOUT_MS = 330000;

    constructor() {
        this.baseUrl = window.location.origin;
        this.csrfToken = this.getCSRFToken();
        this.cache = this.getCache();
        //this.cacheExpiry = 0; // Use for Development
        this.cacheExpiry = 24 * 60 * 60 * 1000; // 24 hours in milliseconds

        // Clear expired cache entries on initialization
        this.clearExpiredCache();
    }

    /**
     * Get the current jurisdiction from the page context.
     * @returns {string} Current jurisdiction code
     */
    getCurrentJurisdiction() {
        // Try to get from jurisdiction selector first
        const jurisdiction = document.getElementById("currentJurisdiction");
        if (jurisdiction && jurisdiction.textContent.trim()) {
            return jurisdiction.textContent.trim().toLowerCase();
        }

        // Default to null if nothing found; rather that than weird bugs from a default state
        console.warn("Returning null for current, likely shouldn't")
        return null;
    }

    cleanOptionText(value) {
        return String(value || "").replace(/ \(Recommended\)$/, "").replace(/ \*$/, "");
    }

    // The court's name for a select's choice, or "" when nothing is chosen.
    selectedOptionText(select) {
        return select.value ? this.cleanOptionText(select.selectedOptions[0]?.textContent) : "";
    }

    getCache() {
        try {
            const cached = localStorage.getItem('apiResponseCache');
            return cached ? JSON.parse(cached) : {};
        } catch (error) {
            console.warn('Error loading API cache:', error);
            return {};
        }
    }

    saveCache() {
        try {
            const cacheString = JSON.stringify(this.cache);
            localStorage.setItem('apiResponseCache', cacheString);
        } catch (error) {
            console.error('❌ Error saving API cache:', error);
        }
    }

    getCacheKey(endpoint, params = {}) {
        // Reference data is still state-specific even when a caller relies on
        // the server-side session to choose the jurisdiction. Without this
        // context, an Illinois court list can be reused on a Massachusetts
        // page because both requests have the same endpoint and no params.
        const cacheParams = {
            ...params
        };
        if (!Object.prototype.hasOwnProperty.call(cacheParams, "jurisdiction")) {
            const jurisdiction = this.getCurrentJurisdiction();
            if (jurisdiction) cacheParams.jurisdiction = jurisdiction;
        }

        const sortedParams = Object.keys(cacheParams).sort().reduce((result, key) => {
            result[key] = cacheParams[key];
            return result;
        }, {});
        return `${endpoint}_${JSON.stringify(sortedParams)}`;
    }

    isCacheValid(cacheEntry) {
        if (!cacheEntry || !cacheEntry.timestamp) {
            return false;
        }
        const age = Date.now() - cacheEntry.timestamp;
        return age < this.cacheExpiry;
    }

    getCachedResponse(endpoint, params = {}) {
        const cacheKey = this.getCacheKey(endpoint, params);
        const cacheEntry = this.cache[cacheKey];

        if (this.isCacheValid(cacheEntry)) {
            return cacheEntry.data;
        }

        return null;
    }

    setCachedResponse(endpoint, params = {}, data) {
        const cacheKey = this.getCacheKey(endpoint, params);
        this.cache[cacheKey] = {
            data: data,
            timestamp: Date.now()
        };
        this.saveCache();
    }

    getCSRFToken() {
        let token = document.querySelector('[name=csrfmiddlewaretoken]')?.value;
        if (!token) {
            token = this.getCookie('csrftoken');
            if (!token) {
                console.warn('CSRF token not found. Some API requests may fail.');
            }
        }
        return token;
    }

    // Method to refresh CSRF token if needed
    async refreshCSRFToken() {
        try {
            const response = await fetch('/api/csrf-token/', {
                method: 'GET',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest'
                }
            });

            if (response.ok) {
                const data = await response.json();
                this.csrfToken = data.csrf_token;

                // Update the token in the form if it exists
                const tokenInput = document.querySelector('[name=csrfmiddlewaretoken]');
                if (tokenInput) {
                    tokenInput.value = this.csrfToken;
                }
            }
        } catch (error) {
            console.warn('Could not refresh CSRF token:', error);
        }
    }

    getCookie(name) {
        if (document.cookie && document.cookie !== '') {
            const cookies = document.cookie.split(';');
            for (let cookie of cookies) {
                const [cookieName, value] = cookie.trim().split('=');
                if (cookieName === name) {
                    return decodeURIComponent(value);
                }
            }
        }
        return null;
    }

    buildUrl(endpoint, params = {}) {
        const url = new URL(endpoint, this.baseUrl);
        Object.keys(params).forEach(key => {
            if (params[key] !== null && params[key] !== undefined) {
                url.searchParams.append(key, params[key]);
            }
        });
        return url;
    }

    getDefaultHeaders() {
        const headers = {
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest'
        };

        if (this.csrfToken) {
            headers['X-CSRFToken'] = this.csrfToken;
        }

        return headers;
    }

    async makeRequest(endpoint, options = {}) {
        const {
            method = 'GET',
                params = {},
                data = null,
                headers = {},
                timeout = 30000
        } = options;

        try {
            const url = this.buildUrl(endpoint, params);

            const requestOptions = {
                method,
                headers: {
                    ...this.getDefaultHeaders(),
                    ...headers
                }
            };

            if (data && method !== 'GET') {
                requestOptions.body = JSON.stringify(data);
            }

            // Create a timeout promise. The timer is cleared once the race
            // settles: it is not cancelled by losing, and an uncleared one keeps
            // a pending task alive for the full budget after the response is
            // already in hand -- up to two minutes on a filing call.
            let timeoutId;
            const timeoutPromise = new Promise((_, reject) => {
                timeoutId = setTimeout(() => reject(new Error('Request timeout')), timeout);
            });

            // Race between fetch and timeout
            let response;
            try {
                response = await Promise.race([
                    fetch(url, requestOptions),
                    timeoutPromise
                ]);
            } finally {
                clearTimeout(timeoutId);
            }

            if (!response.ok) {
                // Prefer the server's own message. Our API answers failures with
                // {success: false, error: "..."}, and that text is often the only
                // actionable thing the filer can be told -- which required party
                // is missing, which document the EFSP could not fetch. Collapsing
                // every 400 to "Invalid request" throws exactly that away.
                const serverBody = await response.clone().json()
                    .catch(() => null);
                const serverMessage = serverBody?.error;
                const error = new Error(serverMessage || `HTTP error! status: ${response.status}`);
                error.status = response.status;
                error.serverMessage = serverMessage || null;
                error.errorActions = serverBody?.error_actions || [];
                error.errorCode = serverBody?.error_code || null;
                throw error;
            }

            const result = await response.json();
            return result;

        } catch (error) {
            console.error('API request failed:', error);
            const handled = this.handleApiError(error);
            // The friendlier message replaces the error, but callers still need
            // to know whether the server answered and how (e.g. a 4xx refusal).
            if (handled !== error && error?.status !== undefined) handled.status = error.status;
            throw handled;
        }
    }

    async fetchJSON(endpoint, method = 'GET', params = {}, data = null) {
        return this.makeRequest(endpoint, {
            method,
            params,
            data,
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': this.getCSRFToken(),
            }
        });
    }

    handleApiError(error) {
        // A message the server wrote is always more useful than the status-code
        // wording below, so it passes through untouched.
        if (error.serverMessage) {
            return error;
        }
        if (error.message === 'Request timeout') {
            return new Error('Request timed out. Please check your connection and try again.');
        } else if (error.message.includes('Failed to fetch')) {
            return new Error('Network error. Please check your connection.');
        } else if (error.message.includes('HTTP error')) {
            const status = error.message.match(/status: (\d+)/)?.[1];
            switch (status) {
                case '400':
                    return new Error('Invalid request. Please check your input.');
                case '401':
                    return new Error('Authentication required. Please log in.');
                case '403':
                    return new Error('Access denied. You don\'t have permission for this action.');
                case '404':
                    return new Error('Resource not found.');
                case '429':
                    return new Error('Too many requests. Please wait a moment and try again.');
                case '500':
                    return new Error('Server error. Please try again later.');
                default:
                    return new Error(`An unexpected error occurred (${status}).`);
            }
        }
        return error;
    }

    // Convenience methods for common HTTP verbs
    async get(endpoint, params = {}, use_csrf = false) {
        // Check cache first
        const cachedResponse = this.getCachedResponse(endpoint, params);
        if (cachedResponse !== null) {
            return cachedResponse;
        }

        let headers = {};
        if (use_csrf) {
            headers = {
                "Content-Type": "application/json",
                "X-CSRFToken": apiUtils.getCSRFToken(),
            };
        }

        // Make API request if not cached
        const response = await this.makeRequest(endpoint, {
            params,
            headers
        });

        // Cache the response
        this.setCachedResponse(endpoint, params, response);

        return response;
    }


    async post(endpoint, data = {}, params = {}, options = {}) {
        return this.makeRequest(endpoint, {
            method: 'POST',
            data,
            params,
            ...options
        });
    }

    async put(endpoint, data = {}, params = {}) {
        return this.makeRequest(endpoint, {
            method: 'PUT',
            data,
            params
        });
    }

    async patch(endpoint, data = {}, params = {}) {
        return this.makeRequest(endpoint, {
            method: 'PATCH',
            data,
            params
        });
    }

    async delete(endpoint, params = {}) {
        return this.makeRequest(endpoint, {
            method: 'DELETE',
            params
        });
    }

    // Draft state (case/upload data) is owned by the server-side FilingDraft
    // model. It must never be cached in localStorage: a stale blob could survive
    // a submit/reset and leak into the next filing. These always hit the server.
    async getCaseData() {
        return this.fetchJSON("/api/get-case-data", "GET");
    }

    async saveCaseData(body) {
        return this.fetchJSON("/api/save-case-data/", "POST", {}, body);
    }

    async getPartyTypes(params) {
        return this.fetchJSON("/api/get-party-types", "GET", params);
    }

    async getUploadData() {
        return this.fetchJSON("/api/get-upload-data", "GET");
    }

    // Cache management methods
    clearAllCache() {
        this.cache = {};
        localStorage.removeItem('apiResponseCache');
    }

    clearCache(key) {
        delete this.cache[key];
        this.saveCache();
    }

    clearExpiredCache() {
        let cleared = 0;

        Object.keys(this.cache).forEach(key => {
            const entry = this.cache[key];
            if (!this.isCacheValid(entry)) {
                delete this.cache[key];
                cleared++;
            }
        });

        if (cleared > 0) {
            this.saveCache();
        }
    }

    // Debug and testing methods
    getCacheStats() {
        const stats = {
            totalEntries: Object.keys(this.cache).length,
            validEntries: 0,
            expiredEntries: 0,
            cacheSize: JSON.stringify(this.cache).length,
            entries: []
        };

        Object.keys(this.cache).forEach(key => {
            const entry = this.cache[key];
            const isValid = this.isCacheValid(entry);
            const age = Date.now() - entry.timestamp;

            if (isValid) {
                stats.validEntries++;
            } else {
                stats.expiredEntries++;
            }

            stats.entries.push({
                key: key.substring(0, 50) + (key.length > 50 ? '...' : ''),
                ageMinutes: Math.round(age / 60000),
                isValid,
                dataSize: JSON.stringify(entry.data).length
            });
        });

        return stats;
    }

    logCacheStats() {
        const stats = this.getCacheStats();
        return stats;
    }

}

// Create global instance
const apiUtils = new ApiUtils();

// Global cache testing functions for browser console
window.cacheStats = () => apiUtils.logCacheStats();
window.clearCache = () => apiUtils.clearCache();
window.getCacheData = () => apiUtils.cache;


// Export for module use or make globally available
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        ApiUtils,
        apiUtils
    };
} else {
    window.ApiUtils = ApiUtils;
    window.apiUtils = apiUtils;
}