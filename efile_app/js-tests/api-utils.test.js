/**
 * Unit tests for ApiUtils caching behavior.
 *
 * Runs on Node's built-in test runner (`node --test`) with no extra deps.
 * Kept out of ./tests so Playwright's E2E runner does not pick it up.
 *
 * The rule these tests lock in: the localStorage cache is only for
 * jurisdiction-scoped, shareable reference data (dropdowns, form config).
 * Per-user / per-draft state (case data, uploads, profile, payment accounts,
 * tokens, fees) is server-owned and must always be fetched fresh.
 */

const test = require("node:test");
const assert = require("node:assert");

// ApiUtils constructs a singleton and touches window/document/localStorage at
// module load, so stub those globals before requiring it.
const store = new Map();
globalThis.localStorage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
};
globalThis.window = {
    location: {
        origin: "http://localhost"
    }
};
globalThis.document = {
    getElementById: () => null,
    querySelector: () => ({
        value: "test-csrf-token"
    }),
    cookie: ""
};

const {
    ApiUtils
} = require("../efile/static/js/api-utils.js");

function makeClient() {
    const client = new ApiUtils();
    client.clearAllCache();
    let calls = 0;
    // Replace the network layer so we can count real round trips.
    client.makeRequest = async (endpoint) => {
        calls += 1;
        return {
            success: true,
            endpoint,
            call: calls
        };
    };
    return {
        client,
        calls: () => calls
    };
}

test("reference-data GETs are cached: repeated reads hit the network once", async () => {
    const {
        client,
        calls
    } = makeClient();
    await client.get("/api/dropdowns/courts", {
        jurisdiction: "illinois"
    });
    await client.get("/api/dropdowns/courts", {
        jurisdiction: "illinois"
    });
    assert.strictEqual(calls(), 1);
});

test("cleanOptionText removes recommendation markers before saving labels", () => {
    const {
        client
    } = makeClient();
    assert.strictEqual(client.cleanOptionText("Cook County *"), "Cook County");
    assert.strictEqual(client.cleanOptionText("Cook County (Recommended)"), "Cook County");
});

test("reference-data GETs are isolated by the current jurisdiction", async () => {
    const {
        client,
        calls
    } = makeClient();

    client.getCurrentJurisdiction = () => "illinois";
    await client.get("/api/dropdowns/courts");

    client.getCurrentJurisdiction = () => "massachusetts";
    await client.get("/api/dropdowns/courts");

    // Returning to Illinois reuses only the Illinois entry, not the
    // Massachusetts one.
    client.getCurrentJurisdiction = () => "illinois";
    await client.get("/api/dropdowns/courts");

    assert.strictEqual(calls(), 2);
});

test("draft state (case + upload data) is never cached", async () => {
    const {
        client,
        calls
    } = makeClient();
    await client.getCaseData();
    await client.getCaseData();
    await client.getUploadData();
    await client.getUploadData();
    assert.strictEqual(calls(), 4);
});

test("per-user reads via fetchJSON (profile, payment accounts, token) are not cached", async () => {
    const {
        client,
        calls
    } = makeClient();
    await client.fetchJSON("/api/payment-accounts", "GET", {
        jurisdiction: "illinois"
    });
    await client.fetchJSON("/api/payment-accounts", "GET", {
        jurisdiction: "illinois"
    });
    assert.strictEqual(calls(), 2);
});

test("saving case data does not populate the read cache", async () => {
    const {
        client,
        calls
    } = makeClient();
    await client.saveCaseData({
        court: "cook:cd"
    });
    await client.getCaseData();
    await client.getCaseData();
    // 1 save + 2 uncached reads
    assert.strictEqual(calls(), 3);
});

// --- error reporting ---------------------------------------------------------
//
// The server answers a rejected filing with {success: false, error: "..."} that
// names what the filer must correct -- a missing required party type, a document
// the EFSP could not fetch. Collapsing every 400 to "Invalid request. Please
// check your input." hid two real, separately-diagnosable failures behind one
// unactionable sentence.

function stubFetch(response) {
    const client = new ApiUtils();
    globalThis.fetch = async () => response;
    return client;
}

function jsonResponse({
    ok,
    status,
    body
}) {
    const response = {
        ok,
        status,
        json: async () => body,
    };
    response.clone = () => response;
    return response;
}

test("a failed request surfaces the server's own error message", async () => {
    const client = stubFetch(jsonResponse({
        ok: false,
        status: 400,
        body: {
            success: false,
            error: "This case type requires a Plaintiff.",
            error_code: "example_error",
            error_actions: [{
                url: "/jurisdiction/illinois/party-details/?party=2&focus=party_type",
                label: "Edit role"
            }]
        },
    }));

    await assert.rejects(
        () => client.post("/api/payment-fees/", {}),
        (error) => {
            assert.strictEqual(error.message, "This case type requires a Plaintiff.");
            assert.strictEqual(error.serverMessage, "This case type requires a Plaintiff.");
            assert.strictEqual(error.errorActions[0].label, "Edit role");
            assert.strictEqual(error.errorCode, "example_error");
            assert.strictEqual(error.status, 400);
            return true;
        },
    );
});

test("a failure with no parseable body still reports the status", async () => {
    const response = {
        ok: false,
        status: 500,
        json: async () => {
            throw new Error("not JSON");
        },
    };
    response.clone = () => response;
    const client = stubFetch(response);

    await assert.rejects(
        () => client.post("/api/payment-fees/", {}),
        (error) => {
            assert.match(error.message, /Server error/);
            // Callers still need the status to tell a refusal from no answer.
            assert.strictEqual(error.status, 500);
            return true;
        },
    );
});

test("a successful response is returned unchanged", async () => {
    const client = stubFetch(jsonResponse({
        ok: true,
        status: 200,
        body: {
            success: true,
            api_response: {
                feesCalculationAmount: {
                    value: 0
                }
            }
        },
    }));

    const result = await client.post("/api/payment-fees/", {});

    assert.strictEqual(result.success, true);
});


// --- long-running filing calls ----------------------------------------------

test("long-running EFSP calls use endpoint-specific request timeouts", async () => {
    // The generic 30s default reported a timeout for an Adams County fee quote
    // the server answered successfully after 43s. Callers pass the longer budget
    // explicitly rather than raising it for every request on the page.
    const client = new ApiUtils();
    let seenTimeout = null;
    client.makeRequest = async (endpoint, options) => {
        seenTimeout = options.timeout;
        return {
            success: true
        };
    };

    await client.post("/api/payment-fees/", {}, {}, {
        timeout: ApiUtils.FEE_TIMEOUT_MS
    });

    assert.strictEqual(seenTimeout, ApiUtils.FEE_TIMEOUT_MS);
    assert.ok(ApiUtils.FEE_TIMEOUT_MS > 60000, "must accommodate slow fee quotes");

    await client.post("/api/submit-final-filing/", {}, {}, {
        timeout: ApiUtils.SUBMISSION_TIMEOUT_MS
    });

    assert.strictEqual(seenTimeout, ApiUtils.SUBMISSION_TIMEOUT_MS);
    assert.ok(ApiUtils.SUBMISSION_TIMEOUT_MS > 300000, "must outlast the server's default read timeout");
});
test("a plain-text refusal keeps its 4xx status", async () => {
    const response = {
        ok: false,
        status: 403,
        json: async () => {
            throw new Error("not JSON");
        },
    };
    response.clone = () => response;
    const client = stubFetch(response);

    await assert.rejects(
        () => client.post("/api/submit-final-filing/", {}),
        (error) => {
            assert.match(error.message, /Access denied/);
            assert.strictEqual(error.status, 403);
            assert.strictEqual(error.serverMessage, undefined);
            return true;
        },
    );
});