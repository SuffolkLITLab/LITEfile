const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function harness(fetch, action = "confirm", feeToken = null) {
    const events = [];
    const status = {
        textContent: ""
    };
    const buttons = [{
        disabled: false
    }, {
        disabled: false
    }];
    const check = {
        removed: false,
        dataset: {
            documentId: "7",
            fingerprint: "abc"
        },
        querySelector: (selector) => selector === "[data-document-check-status]" ? status : null,
        querySelectorAll: () => buttons,
        remove() {
            this.removed = true;
        }
    };
    const container = {
        dataset: {
            url: "/document-checks/"
        },
        dispatchEvent(event) {
            events.push(event);
        }
    };
    const button = {
        hasAttribute: (name) => name === "data-document-remove" && action === "remove",
        closest: (selector) => selector === "[data-document-checks]" ? container : check
    };
    let click;
    const context = vm.createContext({
        document: {
            getElementById: () => feeToken === null ? null : {
                textContent: JSON.stringify(feeToken)
            },
            addEventListener(event, callback) {
                if (event === "click") click = callback;
            }
        },
        window: {
            withFilingDraft: (url) => `${url}?draft=42`
        },
        CustomEvent: class {
            constructor(type, options) {
                this.type = type;
                this.detail = options.detail;
            }
        },
        FormData,
        fetch,
        gettext: (text) => text,
        apiUtils: {
            getCSRFToken: () => "csrf"
        }
    });
    // Evaluates only the checked-in browser script.
    // eslint-disable-next-line sonarjs/code-eval
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../efile/static/js/document-checks.js"), "utf8"), context);
    return {
        context,
        click: () => context.window.DocumentChecks.submit(button),
        hasClick: () => typeof click === "function",
        check,
        status,
        buttons,
        events
    };
}

test("confirming posts the copy's fingerprint and tells the page", async () => {
    let request;
    const page = harness(async (url, options) => {
        request = {
            url,
            body: options.body
        };
        return {
            ok: true,
            json: async () => ({
                success: true,
                fee_inputs_token: "new"
            })
        };
    });
    assert.equal(page.hasClick(), true);
    await page.click();
    assert.equal(request.url, "/document-checks/?draft=42");
    assert.equal(request.body.get("action"), "confirm");
    assert.equal(request.body.get("document_id"), "7");
    assert.equal(request.body.get("preview_fingerprint"), "abc");
    assert.equal(page.check.removed, true);
    assert.equal(page.events.length, 1);
    assert.equal(page.events[0].type, "document-checks:change");
    assert.equal(page.events[0].detail.action, "confirm");
    assert.equal(page.events[0].detail.data.fee_inputs_token, "new");
});

test("removing sends the remove action", async () => {
    let action;
    const page = harness(async (_url, options) => {
        action = options.body.get("action");
        return {
            ok: true,
            json: async () => ({
                success: true
            })
        };
    }, "remove");
    await page.click();
    assert.equal(action, "remove");
    assert.equal(page.events[0].detail.action, "remove");
});

test("payment checks send the token for the case data the page still holds", async () => {
    for (const action of ["confirm", "remove"]) {
        let token;
        const page = harness(async (_url, options) => {
            token = options.body.get("fee_inputs_token");
            return {
                ok: false,
                json: async () => ({
                    error: "This filing changed. Reload this page."
                })
            };
        }, action, "page-inputs-token");
        await page.click();
        assert.equal(token, "page-inputs-token");
        assert.equal(page.check.removed, false);
        assert.equal(page.events.length, 0);
        assert.match(page.status.textContent, /Reload this page/);
    }
});

test("a refused check stays on the page with its reason and can be retried", async () => {
    const page = harness(async () => ({
        ok: false,
        json: async () => ({
            error: "This file changed. Reload this page and check it again."
        })
    }));
    await page.click();
    assert.equal(page.check.removed, false);
    assert.equal(page.events.length, 0);
    assert.match(page.status.textContent, /This file changed/);
    assert.deepEqual(page.buttons.map((button) => button.disabled), [false, false]);
});

test("pending finds any check still on the page", () => {
    const page = harness(async () => ({}));
    assert.equal(page.context.window.DocumentChecks.pending({
        querySelector: () => ({})
    }), true);
    assert.equal(page.context.window.DocumentChecks.pending({
        querySelector: () => null
    }), false);
});