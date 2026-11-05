const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function harness() {
    const pending = [];
    const select = {
        value: "",
        dataset: {},
        disabled: false,
        options: [],
        replaceChildren(option) {
            this.options = [option];
        },
        add(option) {
            this.options.push(option);
        }
    };
    const status = {};
    const retryButton = {};
    const root = {
        querySelector: selector => selector === "[data-document-type-select]" ? select : (selector === "[data-document-type-retry]" ? retryButton : status)
    };
    const context = vm.createContext({
        window: {},
        document: {
            querySelectorAll: () => []
        },
        gettext: text => text,
        Option: class {
            constructor(text, value) {
                this.text = text;
                this.value = value;
            }
        },
        fetch: () => new Promise(resolve => pending.push(resolve))
    });
    // eslint-disable-next-line sonarjs/code-eval
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../efile/static/js/document-type-picker.js"), "utf8"), context);
    return {
        root,
        select,
        status,
        retryButton,
        pending,
        picker: context.window.DocumentTypePicker
    };
}

test("late court choices cannot overwrite the newer case's document types", async () => {
    const h = harness();
    const earlier = h.picker.load(h.root, "/old");
    const later = h.picker.load(h.root, "/new");
    h.pending[1]({
        ok: true,
        json: async () => ({
            success: true,
            data: [{
                value: "new",
                text: "New choice"
            }]
        })
    });
    await later;
    h.pending[0]({
        ok: true,
        json: async () => ({
            success: true,
            data: [{
                value: "old",
                text: "Old choice"
            }]
        })
    });
    await earlier;
    assert.equal(h.select.options[1].value, "new");
    assert.equal(h.select.disabled, false);
});

test("failed choices remain disabled and a retry can recover", async () => {
    const h = harness();
    const failed = h.picker.load(h.root, "/choices");
    h.pending[0]({
        ok: false,
        json: async () => ({
            success: false
        })
    });
    await failed;
    assert.equal(h.select.disabled, true);
    assert.match(h.status.textContent, /Try again/);
    assert.equal(h.retryButton.hidden, false);
    const retry = h.picker.load(h.root, "/choices");
    assert.equal(h.retryButton.hidden, true);
    h.pending[1]({
        ok: true,
        json: async () => ({
            success: true,
            data: [{
                value: "ok",
                text: "Allowed"
            }]
        })
    });
    await retry;
    assert.equal(h.select.disabled, false);
});