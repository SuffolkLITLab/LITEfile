const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

// Review after a failed submit request (#445): the browser's error never proves
// that nothing reached the court, so the page asks the server what happened.
function loadReview({
    submit,
    statuses
}) {
    const nodes = new Map();
    const node = (id) => {
        if (!nodes.has(id)) nodes.set(id, {
            id,
            hidden: true,
            disabled: false,
            checked: id === "confirm-filing",
            style: {},
            textContent: {
                "disclaimer-token": "\"token\"",
                "payment-account-id": "\"account\"",
                loadingSpinnerText: "Sending"
            } [id] || "",
            scrollIntoView() {},
            replaceChildren() {}
        });
        return nodes.get(id);
    };
    const statusCalls = [];
    const rendered = [];
    const window = {
        location: {
            href: "/jurisdiction/illinois/review/?draft=7"
        },
        FilingErrorActions: {
            render: (_id, actions) => rendered.push(actions)
        }
    };
    const context = vm.createContext({
        window,
        document: {
            getElementById: node,
            addEventListener() {}
        },
        gettext: (text) => text,
        setTimeout: (callback) => callback(),
        FilingPayload: {},
        ApiUtils: {
            SUBMISSION_TIMEOUT_MS: 330000
        },
        apiUtils: {
            post: submit,
            async makeRequest(url, options) {
                statusCalls.push([url, options.method]);
                const next = statuses.shift();
                if (next instanceof Error) throw next;
                return next;
            },
            getCurrentJurisdiction: () => "illinois"
        }
    });
    // Only the checked-in script runs in this test.
    // eslint-disable-next-line sonarjs/code-eval
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../efile/static/js/review.js"), "utf8") +
        " globalThis.handler = FilingHandler;", context);
    const handler = context.handler;
    handler.feeQuoteState = "current";
    handler.outcomePollDelayMs = 0;
    handler.buildCurrentFilingData = () => ({});
    handler.handleSubmissionResult = (result) => {
        handler.succeeded = result;
    };
    handler.refreshFeeQuote = async () => {
        handler.refreshedFees = true;
    };
    return {
        handler,
        node,
        window,
        statusCalls,
        rendered
    };
}

const proxyError = () => Object.assign(new Error("HTTP error! status: 502"), {
    status: 502
});

test("a lost response for a filing the court received goes to its confirmation", async () => {
    const page = loadReview({
        submit: async () => {
            throw proxyError();
        },
        statuses: [{
            outcome: "pending"
        }, {
            outcome: "submitted",
            redirect_url: "/jurisdiction/illinois/filing-confirmation/?draft=7"
        }]
    });

    await page.handler.submitFiling();

    assert.equal(page.window.location.href, "/jurisdiction/illinois/filing-confirmation/?draft=7");
    assert.deepEqual(page.statusCalls, [
        ["/api/submission-status/", "GET"],
        ["/api/submission-status/", "GET"]
    ]);
    assert.equal(page.node("errorMessage").hidden, true);
});

test("only a filing the server released is offered another try", async () => {
    const page = loadReview({
        submit: async () => {
            throw Object.assign(new Error("Fees changed"), {
                status: 412,
                serverMessage: "Check the new total",
                errorCode: "fee_quote_stale"
            });
        },
        statuses: [{
            outcome: "not_sent"
        }]
    });

    await page.handler.submitFiling();

    assert.equal(page.node("errorText").textContent, "Check the new total");
    assert.equal(page.node("submitButton").disabled, false);
    assert.equal(page.handler.refreshedFees, true);
});

test("an unknown outcome keeps Submit off and points to My filings", async () => {
    const page = loadReview({
        submit: async () => {
            throw proxyError();
        },
        statuses: Array.from({
            length: 20
        }, () => ({
            outcome: "pending"
        }))
    });

    page.handler.outcomePollAttempts = 12;
    await page.handler.submitFiling();

    assert.equal(page.statusCalls.length, 12);
    assert.match(page.node("errorText").textContent, /Do not submit it again/);
    assert.doesNotMatch(page.node("errorText").textContent, /try again/i);
    assert.equal(page.node("submitButton").disabled, true);
    assert.deepEqual(JSON.parse(JSON.stringify(page.rendered.at(-1))), [{
        url: "/jurisdiction/illinois/filing_statuses/",
        label: "Open My filings"
    }]);
    // Re-ticking the confirmation (or a fee refresh) must not turn it back on.
    page.handler.setSubmissionState(false);
    assert.equal(page.node("submitButton").disabled, true);
    assert.equal(page.node("loadingSpinnerText").textContent, "Sending");
});

test("an unreachable status check is treated as unknown, not as a failure", async () => {
    const page = loadReview({
        submit: async () => {
            throw new Error("Request timed out. Please check your connection and try again.");
        },
        statuses: [new Error("offline"), new Error("offline"), new Error("offline"), {
            outcome: "not_sent"
        }]
    });

    await page.handler.submitFiling();

    assert.equal(page.statusCalls.length, 3);
    assert.match(page.node("errorText").textContent, /not sure whether your filing reached the court/);
    assert.equal(page.node("submitButton").disabled, true);
});

test("a refused claim or an unsuccessful answer also checks before advising", async () => {
    for (const submit of [
            async () => ({
                    success: false,
                    error: "This filing can't be submitted again automatically."
                }),
                async () => {
                    throw Object.assign(new Error("conflict"), {
                        status: 409,
                        serverMessage: "This filing can't be submitted again automatically."
                    });
                }
        ]) {
        const page = loadReview({
            submit,
            statuses: [{
                outcome: "unknown"
            }]
        });

        await page.handler.submitFiling();

        assert.equal(page.statusCalls.length, 1);
        assert.match(page.node("errorText").textContent, /Do not submit it again/);
        assert.equal(page.node("submitButton").disabled, true);
    }
});

test("a successful submit does not check the status", async () => {
    const page = loadReview({
        submit: async () => ({
            success: true,
            redirect_url: "/done"
        }),
        statuses: []
    });

    await page.handler.submitFiling();

    assert.equal(page.handler.succeeded.redirect_url, "/done");
    assert.equal(page.statusCalls.length, 0);
});
test("an error before anything is sent needs no status check", async () => {
    let posts = 0;
    const page = loadReview({
        submit: async () => {
            posts += 1;
        },
        statuses: []
    });
    page.handler.buildCurrentFilingData = () => {
        throw new Error("Missing party type");
    };

    await page.handler.submitFiling();

    assert.equal(posts, 0);
    assert.equal(page.statusCalls.length, 0);
    assert.equal(page.node("errorText").textContent, "We could not submit the filing. Please try again.");
    assert.equal(page.node("submitButton").disabled, false);
});

test("nothing re-enables Submit while a submit is in flight", async () => {
    let posts = 0;
    let answer;
    const page = loadReview({
        submit: () => {
            posts += 1;
            return new Promise((resolve) => {
                answer = resolve;
            });
        },
        statuses: []
    });

    const first = page.handler.submitFiling();
    // The filer unticks and reticks the confirmation box, then clicks again.
    page.handler.refreshSubmitButton();
    assert.equal(page.node("submitButton").disabled, true);
    assert.equal(page.node("loadingSpinner").style.display, "block");
    await page.handler.submitFiling();
    assert.equal(posts, 1);

    answer({
        success: true,
        redirect_url: "/done"
    });
    await first;
    page.handler.refreshSubmitButton();
    assert.equal(page.node("submitButton").disabled, true);
});

test("after no reply, an unclaimed draft must stay unclaimed before advising a retry", async () => {
    const settled = loadReview({
        submit: async () => {
            throw proxyError();
        },
        statuses: Array.from({
            length: 10
        }, () => ({
            outcome: "not_sent"
        }))
    });

    await settled.handler.submitFiling();

    assert.equal(settled.statusCalls.length, settled.handler.draftSettleChecks);
    assert.equal(settled.node("errorText").textContent, "We could not submit the filing. Please try again.");
    assert.equal(settled.node("submitButton").disabled, false);

    // The first request was still checking the payment account, then claimed.
    const late = loadReview({
        submit: async () => {
            throw proxyError();
        },
        statuses: [{
            outcome: "not_sent"
        }, {
            outcome: "not_sent"
        }, {
            outcome: "pending"
        }, {
            outcome: "submitted",
            redirect_url: "/jurisdiction/illinois/filing-confirmation/?draft=7"
        }]
    });

    await late.handler.submitFiling();

    assert.equal(late.window.location.href, "/jurisdiction/illinois/filing-confirmation/?draft=7");
});

test("a refused status check is not retried", async () => {
    const page = loadReview({
        submit: async () => {
            throw proxyError();
        },
        statuses: [Object.assign(new Error("signed out"), {
            status: 409
        }), {
            outcome: "not_sent"
        }]
    });

    await page.handler.submitFiling();

    assert.equal(page.statusCalls.length, 1);
    assert.match(page.node("errorText").textContent, /Do not submit it again/);
});

test("a released draft after a refused second submit gets general advice", async () => {
    const page = loadReview({
        submit: async () => {
            throw Object.assign(new Error("conflict"), {
                status: 409,
                serverMessage: "This filing can't be submitted again automatically."
            });
        },
        statuses: [{
            outcome: "not_sent"
        }]
    });

    await page.handler.submitFiling();

    assert.equal(page.node("errorText").textContent, "We could not submit the filing. Please try again.");
    assert.equal(page.node("submitButton").disabled, false);
});