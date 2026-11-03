const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function pageHarness(post, checks = {
    pending: () => false
}) {
    const nodes = new Map();
    const node = (id) => {
        if (!nodes.has(id)) nodes.set(id, {
            value: "",
            hidden: true,
            disabled: false,
            style: {},
            textContent: id === "selected-payment-account-id" ? '""' : '{}',
            scrollIntoView() {},
            focus() {
                this.focused = true;
            },
            listeners: {},
            addEventListener(event, handler) {
                this.listeners[event] = handler;
            },
            showModal() {
                this.open = true;
            },
            close() {
                this.open = false;
            },
            querySelectorAll: () => []
        });
        return nodes.get(id);
    };
    const intent = {
        value: "waiver"
    };
    const context = vm.createContext({
        document: {
            getElementById: node,
            querySelectorAll: () => [],
            createElement: () => ({
                textContent: "",
                get innerHTML() {
                    return this.textContent;
                }
            }),
            querySelector: (selector) => selector.includes("paymentIntent") ? (selector.includes(":checked") && intent.checked === false ? null : intent) : null,
            addEventListener() {}
        },
        window: {
            confirm: () => true,
            location: {
                search: ""
            }
        },
        URLSearchParams,
        gettext: (text) => text,
        FilingPayload: {},
        DocumentChecks: checks,
        apiUtils: {
            post,
            getCurrentJurisdiction: () => "illinois",
            fetchJSON: async () => ({
                success: true,
                data: []
            })
        }
    });
    // Only the checked-in browser script is evaluated; no external input.
    // eslint-disable-next-line sonarjs/code-eval
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../efile/static/js/payment.js"), "utf8") +
        "\nglobalThis.payment = PaymentPage;", context);
    return {
        context,
        payment: context.payment,
        window: context.window,
        apiUtils: context.apiUtils,
        node,
        intent
    };
}

const waiver = {
    success: true,
    data: {
        paymentAccountID: "waiver-1",
        accountName: "Existing waiver",
        paymentAccountTypeCode: "WV"
    }
};

test("waiver selection enables review without requesting a live fee quote", async () => {
    const calls = [];
    const {
        payment,
        node
    } = pageHarness(async (url) => {
        calls.push(url);
        return waiver;
    });
    await payment.chooseIntent();
    assert.deepEqual(calls, ["/api/waiver-account/"]);
    assert.equal(node("selected-payment-account").value, "waiver-1");
    assert.equal(node("selected-payment-account-type").value, "WV");
    assert.equal(node("submitButton").disabled, false);
    assert.equal(node("waiverHelp").hidden, false);
});

test("switching away from a pending waiver cannot restore its account", async () => {
    let resolve;
    const response = new Promise((done) => {
        resolve = done;
    });
    const {
        payment,
        node,
        intent
    } = pageHarness(() => response);
    const pending = payment.chooseIntent();
    intent.value = "pay";
    await payment.chooseIntent();
    resolve(waiver);
    await pending;
    assert.equal(node("selected-payment-account").value, "");
    assert.equal(node("submitButton").disabled, true);
    assert.equal(node("waiverHelp").hidden, true);
});

test("toggling back to waiver shares its pending creation request", async () => {
    let resolve;
    let calls = 0;
    const response = new Promise((done) => {
        resolve = done;
    });
    const {
        payment,
        intent,
        node
    } = pageHarness(() => {
        calls++;
        return response;
    });
    const first = payment.chooseIntent();
    intent.value = "pay";
    await payment.chooseIntent();
    intent.value = "waiver";
    const second = payment.chooseIntent();
    resolve(waiver);
    await Promise.all([first, second]);
    assert.equal(calls, 1);
    assert.equal(node("selected-payment-account").value, "waiver-1");
    assert.equal(node("submitButton").disabled, false);
});

test("waiver failures leave review disabled and can be retried", async () => {
    let attempts = 0;
    const {
        payment,
        node
    } = pageHarness(async () => {
        if (++attempts === 1) throw new Error("offline");
        return waiver;
    });
    await payment.chooseIntent();
    assert.equal(node("submitButton").disabled, true);
    assert.equal(node("errorMessage").hidden, false);
    await payment.chooseIntent();
    assert.equal(attempts, 2);
    assert.equal(node("submitButton").disabled, false);
});
test("free first filing without an account explains the next step without creating a waiver", async () => {
    const calls = [];
    const {
        payment,
        node,
        intent
    } = pageHarness(async (url) => calls.push(url));
    intent.value = "pay";
    await payment.chooseIntent();
    assert.deepEqual(calls, []);
    assert.match(node("paymentMethodsContainer").innerHTML, /Add a credit card or bank account to continue/);
    assert.match(node("paymentMethodsContainer").innerHTML, /Add credit card or bank account/);
    assert.equal(node("submitButton").disabled, true);
});

test("waiver setup explains the zero charge and court approval", async () => {
    const {
        payment,
        node
    } = pageHarness(async () => waiver);
    await payment.chooseIntent();
    assert.match(node("successText").textContent, /\$0 due now/);
    assert.match(node("successText").textContent, /court may ask you to pay/);
});

test("the only active paid account is selected and quoted", async () => {
    const {
        payment,
        node,
        apiUtils
    } = pageHarness();
    apiUtils.fetchJSON = async () => ({
        success: true,
        data: [{
            paymentAccountID: "card",
            paymentAccountTypeCode: "CC"
        }, {
            paymentAccountID: "old",
            paymentAccountTypeCode: "CC",
            active: false
        }, {
            paymentAccountID: "wv",
            paymentAccountTypeCode: "WV"
        }]
    });
    payment.accountLabel = () => "My card";
    let quotes = 0;
    payment.selectAndQuote = async () => quotes++;
    payment.quoteRequestId = 1;
    await payment.choosePaidAccount(1);
    assert.match(node("paymentMethodsContainer").innerHTML, /value="card"[^>]*checked/);
    assert.doesNotMatch(node("paymentMethodsContainer").innerHTML, /value="wv"|value="old"/);
    assert.equal(quotes, 1);
});

test("a zero estimate automatically opens paid accounts even with a saved waiver", async () => {
    const {
        payment,
        node,
        intent,
        apiUtils
    } = pageHarness();
    intent.checked = false;
    node("estimated-zero-fees").textContent = "true";
    node("selected-payment-account-id").textContent = '"wv"';
    apiUtils.fetchJSON = async () => ({
        success: true,
        data: [{
            paymentAccountID: "wv",
            paymentAccountTypeCode: "WV"
        }]
    });
    let opened = false;
    payment.chooseIntent = async () => {
        opened = true;
    };
    await payment.loadAccounts();
    assert.equal(intent.checked, true);
    assert.equal(opened, true);
});

test("a saved waiver alone does not request another waiver automatically", async () => {
    const {
        payment,
        node,
        intent,
        apiUtils
    } = pageHarness();
    intent.checked = false;
    node("estimated-zero-fees").textContent = "false";
    node("selected-payment-account-id").textContent = '"wv"';
    apiUtils.fetchJSON = async () => ({
        success: true,
        data: [{
            paymentAccountID: "wv",
            paymentAccountTypeCode: "WV"
        }]
    });
    payment.chooseIntent = async () => assert.fail("Waiver requires an explicit choice");
    await payment.loadAccounts();
    assert.equal(intent.checked, false);
});

test("removing a payment method clears the selection and reloads accounts", async () => {
    const {
        payment,
        apiUtils,
        node
    } = pageHarness();
    const calls = [];
    apiUtils.delete = async (url) => {
        calls.push(url);
        return {
            success: true
        };
    };
    node("selected-payment-account").value = "old-card";
    payment.feeQuoteReady = true;
    payment.chooseIntent = async () => calls.push("reload");
    await payment.removeAccount({
        dataset: {
            accountId: "old-card",
            accountLabel: "Old card"
        }
    });
    assert.deepEqual(calls, ["/api/payment-accounts/old-card/", "reload"]);
    assert.equal(node("selected-payment-account").value, "");
    assert.equal(node("submitButton").disabled, true);
    assert.equal(node("successText").textContent, "Payment method removed.");
});

test("cancelling removal keeps the payment selection", async () => {
    const {
        payment,
        apiUtils,
        node,
        window
    } = pageHarness();
    window.confirm = () => false;
    apiUtils.delete = async () => assert.fail("Cancelled removal");
    node("selected-payment-account").value = "card";
    await payment.removeAccount({
        dataset: {
            accountId: "card"
        }
    });
    assert.equal(node("selected-payment-account").value, "card");
});

test("failed removal keeps the existing payment selection usable and permits retry", async () => {
    const {
        payment,
        apiUtils,
        node
    } = pageHarness();
    node("selected-payment-account").value = "card";
    node("selected-payment-account-name").value = "My card";
    node("paymentSection").hidden = false;
    node("feeQuoteHelp").hidden = false;
    payment.feeQuoteReady = true;
    payment.quoteRequestId = 7;
    apiUtils.delete = async () => {
        assert.equal(node("submitButton").disabled, true);
        throw new Error("offline");
    };
    const button = {
        dataset: {
            accountId: "card"
        }
    };
    await payment.removeAccount(button);
    assert.equal(button.disabled, false);
    assert.equal(payment.removingAccount, false);
    assert.equal(node("errorMessage").hidden, false);
    assert.equal(node("feeQuoteHelp").hidden, true);
    assert.equal(node("selected-payment-account").value, "card");
    assert.equal(node("selected-payment-account-name").value, "My card");
    assert.equal(node("paymentSection").hidden, false);
    assert.equal(payment.feeQuoteReady, true);
    assert.equal(payment.quoteRequestId, 7);
    assert.equal(node("submitButton").disabled, false);
});
test("a copy still waiting to be checked keeps review disabled", async () => {
    let pending = true;
    const {
        payment,
        node
    } = pageHarness(async () => waiver, {
        pending: () => pending
    });
    await payment.chooseIntent();
    assert.equal(node("selected-payment-account").value, "waiver-1");
    assert.equal(node("submitButton").disabled, true);
    pending = false;
    payment.setFeesState(false);
    assert.equal(node("submitButton").disabled, false);
});

test("a rejected fee quote still lets the filer continue to Review", async () => {
    const {
        payment,
        node,
        apiUtils,
        context
    } = pageHarness(async () => {
        throw {
            serverMessage: "This court cannot accept an initial filing"
        };
    });
    context.ApiUtils = {
        FEE_TIMEOUT_MS: 1000
    };
    context.FilingPayload.userDataFromCaseData = () => ({});
    context.document.querySelector = () => ({
        value: "card",
        dataset: {
            name: "My card",
            type: "CC"
        }
    });
    apiUtils.getUploadData = async () => ({});
    payment.buildEFilingData = () => ({});
    await payment.selectAndQuote();
    assert.equal(payment.feeQuoteReady, false);
    assert.equal(node("errorMessage").hidden, false);
    assert.match(node("errorText").textContent, /cannot accept an initial filing/);
    assert.equal(node("feeQuoteHelp").hidden, false);
    assert.equal(node("selected-payment-account").value, "card");
    assert.equal(node("submitButton").disabled, false);
});
test("statutory waiver selection confirms an exemption without claiming financial hardship", async () => {
    const {
        payment,
        node
    } = pageHarness(async () => waiver);
    node("waiver-exemption").textContent = JSON.stringify({
        requires_waiver_account: true
    });
    await payment.chooseIntent();
    assert.equal(node("successText").textContent, "Fee exemption claimed: $0 due now. The court will review your filing.");
    assert.equal(node("selected-payment-account-type").value, "WV");
    assert.equal(node("submitButton").disabled, false);
});

test("adding an account waits for modal confirmation and cancellation does not start a handoff", async () => {
    const {
        payment,
        node,
        intent
    } = pageHarness();
    payment.loadAccountTypes = async () => {};
    payment.loadAccounts = async () => {};
    let handoffs = 0;
    payment.addAccount = async () => {
        handoffs += 1;
    };
    await payment.init();
    intent.value = "pay";
    await payment.chooseIntent();
    const dialog = node("payment-gateway-dialog");
    node("add-payment-method").listeners.click();
    assert.equal(dialog.open, true);
    assert.equal(handoffs, 0);
    node("cancel-payment-gateway").listeners.click();
    assert.equal(dialog.open, false);
    assert.equal(handoffs, 0);
    node("add-payment-method").listeners.click();
    await node("confirm-payment-gateway").listeners.click();
    assert.equal(dialog.open, false);
    assert.equal(handoffs, 1);
});

test("a pending handoff cannot start twice and a failed handoff can be retried", async () => {
    const {
        payment,
        node
    } = pageHarness();
    payment.loadAccountTypes = async () => {};
    payment.loadAccounts = async () => {};
    let reject;
    let calls = 0;
    payment.addAccount = () => {
        calls += 1;
        return new Promise((resolve, fail) => {
            reject = fail;
        });
    };
    await payment.init();
    const confirm = node("confirm-payment-gateway").listeners.click;
    node("add-payment-method").textContent = "Add credit card";
    const pending = confirm();
    assert.equal(node("add-payment-method").disabled, true);
    assert.equal(node("add-payment-method").textContent, "Opening payment provider…");
    await confirm();
    assert.equal(calls, 1);
    reject(new Error("Network unavailable"));
    await pending;
    assert.equal(node("add-payment-method").disabled, false);
    assert.equal(node("add-payment-method").textContent, "Add credit card");
    assert.match(node("errorText").textContent, /Please try again/);
    assert.equal(node("add-payment-method").focused, true);
    payment.addAccount = async () => {
        calls += 1;
    };
    await confirm();
    assert.equal(calls, 2);
});