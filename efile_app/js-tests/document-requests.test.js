const assert = require("node:assert/strict");
const {
    test
} = require("node:test");
const documentRequests = require("../efile/static/js/document-requests.js");

const fallback = "We could not check document analysis. We’ll try again shortly.";

test("requests JSON and preserves structured upload validation", async (t) => {
    t.mock.method(globalThis, "fetch", async (_url, options) => {
        assert.equal(options.headers.get("Accept"), "application/json");
        assert.equal(options.headers.get("X-CSRFToken"), "csrf-value");
        return Response.json({
            success: false,
            error: "Choose a PDF or Word file."
        }, {
            status: 400
        });
    });
    await assert.rejects(documentRequests.json("/upload/", fallback, {
        method: "POST",
        headers: {
            "X-CSRFToken": "csrf-value"
        }
    }), {
        kind: "rejected",
        message: "Choose a PDF or Word file."
    });
});

test("uses plain-language errors for unavailable drafts and expired sessions", async (t) => {
    t.mock.method(globalThis, "fetch", async (url) => url === "/draft/" ?
        new Response("<!DOCTYPE html>", {
            status: 409,
            headers: {
                "Content-Type": "text/html"
            }
        }) :
        Response.json({
            success: false
        }, {
            status: 401
        }));
    await assert.rejects(documentRequests.status("/draft/", fallback), {
        kind: "reopen",
        message: /Open My drafts/
    });
    await assert.rejects(documentRequests.status("/session/", fallback), {
        kind: "reopen",
        message: /Sign in again/
    });
});

test("treats HTML service errors and malformed JSON as uncertain", async (t) => {
    t.mock.method(globalThis, "fetch", async (url) => url === "/outage/" ?
        new Response("<!DOCTYPE html>", {
            status: 502,
            headers: {
                "Content-Type": "text/html"
            }
        }) :
        new Response("<!DOCTYPE html>", {
            status: 200,
            headers: {
                "Content-Type": "application/json"
            }
        }));
    await assert.rejects(documentRequests.status("/outage/", fallback), {
        kind: "uncertain",
        message: fallback
    });
    await assert.rejects(documentRequests.status("/malformed/", fallback), {
        kind: "uncertain",
        message: fallback
    });
});

test("keeps the retry message when the service rejected the upload before storage", async (t) => {
    t.mock.method(globalThis, "fetch", async () => Response.json({
        success: false,
        error_code: "workers_unavailable",
        error: "Try uploading again shortly."
    }, {
        status: 503
    }));
    await assert.rejects(documentRequests.json("/upload/", fallback), {
        kind: "rejected",
        message: "Try uploading again shortly."
    });
});