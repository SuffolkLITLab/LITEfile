const test = require("node:test");
const assert = require("node:assert");

// The browser-only initializer exits before touching the DOM in this test.
globalThis.document = {
    getElementById: () => null
};

const {
    lookupTracker,
    preferredConfidentialityValue
} = require("../efile/static/js/organize-documents.js");

const choices = [{
    value: "PUBLIC",
    confidentiality: "non_confidential"
}, {
    value: "SEALED",
    confidentiality: "confidential"
}, ];

test("the jurisdiction default selects non-confidential", () => {
    assert.strictEqual(preferredConfidentialityValue(choices, "", "non_confidential"), "PUBLIC");
});

test("a saved filer choice takes precedence over the jurisdiction default", () => {
    assert.strictEqual(preferredConfidentialityValue(choices, "SEALED", "non_confidential"), "SEALED");
});

test("two choices stay unselected when a jurisdiction configures no default", () => {
    assert.strictEqual(preferredConfidentialityValue(choices, "", ""), "");
});

test("only the newest lookup for a list may apply its answer", () => {
    const startLookup = lookupTracker();
    const section = {};
    const earlier = startLookup(section);
    assert.strictEqual(earlier(), true);
    const later = startLookup(section);
    assert.strictEqual(earlier(), false);
    assert.strictEqual(later(), true);
});

test("a lookup for one list does not cancel another list's lookup", () => {
    const startLookup = lookupTracker();
    const confidentiality = startLookup({});
    startLookup({});
    assert.strictEqual(confidentiality(), true);
});