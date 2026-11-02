const test = require("node:test");
const assert = require("node:assert/strict");

const {
    mergeFiles
} = require("../efile/static/js/upload-documents.js");

function pdf(name, size, lastModified) {
    return {
        name,
        size,
        lastModified,
        type: "application/pdf"
    };
}

test("sequential drops append files instead of replacing the first drop", () => {
    const firstDrop = mergeFiles([], [pdf("petition.pdf", 100, 1)]);
    const secondDrop = mergeFiles(firstDrop, [pdf("affidavit.pdf", 200, 2)]);

    assert.deepEqual(secondDrop.map(file => file.name), ["petition.pdf", "affidavit.pdf"]);
});

test("the same PDF can be selected again for a different filing code", () => {
    const petition = pdf("petition.pdf", 100, 1);
    const files = mergeFiles([petition], [petition, pdf("exhibit.pdf", 300, 3)]);

    assert.deepEqual(files.map(file => file.name), ["petition.pdf", "petition.pdf", "exhibit.pdf"]);
});

test("removing one pending file leaves the other selected", () => {
    const files = mergeFiles([], [pdf("petition.pdf", 100, 1), pdf("petition.pdf", 200, 2)]);
    const remaining = files.filter((_file, index) => index !== 0);

    assert.equal(remaining.length, 1);
    assert.equal(remaining[0].size, 200);
});
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function uploadPage({
    pending = false,
    hasLead = true,
    waitSeconds = pending ? 120 : 0,
    fetch
}) {
    const nodes = new Map();

    function node(id) {
        if (!nodes.has(id)) nodes.set(id, {
            dataset: {},
            attributes: new Map(),
            listeners: {},
            classList: {
                toggle() {},
                add() {},
                remove() {}
            },
            addEventListener(event, callback) {
                this.listeners[event] = callback;
            },
            setAttribute(name, value) {
                this.attributes.set(name, value);
            },
            removeAttribute(name) {
                this.attributes.delete(name);
            },
            querySelector() {
                return null;
            }
        });
        return nodes.get(id);
    }
    node("document-upload-form").dataset = {
        hasLead: String(hasLead),
        extractionPending: String(pending),
        extractionStatusUrl: "/status/",
        waitSeconds: String(waitSeconds)
    };
    node("continue-to-analysis").dataset.continueUrl = "/preview/?return_to=review";
    node("upload-state").hidden = !pending;
    const timers = [];
    const context = vm.createContext({
        document: {
            getElementById: node,
            querySelector: () => null,
            querySelectorAll: () => []
        },
        window: {
            location: {
                href: "/upload/"
            },
            setTimeout: (callback, delay) => timers.push({
                callback,
                delay
            })
        },
        apiUtils: {
            getCSRFToken: () => "csrf"
        },
        FormData: class {},
        fetch
    });
    // eslint-disable-next-line sonarjs/code-eval
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../efile/static/js/upload-documents.js"), "utf8"), context);
    return {
        node,
        timers,
        submit: () => node("document-upload-form").listeners.submit({
            preventDefault() {}
        })
    };
}

function reply(data) {
    return {
        ok: true,
        json: async () => ({
            success: true,
            ...data
        })
    };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test("pending analysis blocks navigation until complete or failed", async () => {
    for (const status of ["complete", "failed"]) {
        let finish;
        const page = uploadPage({
            pending: true,
            fetch: () => new Promise(resolve => {
                finish = resolve;
            })
        });
        const link = page.node("continue-to-analysis");
        assert.equal(link.attributes.get("aria-disabled"), "true");
        assert.equal(link.attributes.has("href"), false);
        let prevented = false;
        link.listeners.click({
            preventDefault() {
                prevented = true;
            }
        });
        assert.equal(prevented, true);
        finish(reply({
            ready: true,
            status
        }));
        await flush();
        assert.equal(link.attributes.has("aria-disabled"), false);
        assert.equal(link.attributes.get("href"), "/preview/?return_to=review");
    }
});

test("a poll finishing during an upload cannot unlock navigation", async () => {
    let finishPoll;
    let finishUpload;
    const page = uploadPage({
        pending: true,
        fetch: (url) => new Promise(resolve => {
            if (url === "/status/") finishPoll = resolve;
            else finishUpload = resolve;
        })
    });
    const upload = page.submit();
    finishPoll(reply({
        ready: true,
        status: "complete"
    }));
    await flush();
    assert.equal(page.node("continue-to-analysis").attributes.has("href"), false);
    finishUpload(reply({
        extraction_pending: false
    }));
    await upload;
    assert.equal(page.node("continue-to-analysis").attributes.has("href"), false);
});

test("an upload failure restores an already ready filing", async () => {
    const page = uploadPage({
        fetch: async () => {
            throw new Error("Upload failed");
        }
    });
    const upload = page.submit();
    assert.equal(page.node("continue-to-analysis").attributes.has("href"), false);
    await upload;
    assert.equal(page.node("continue-to-analysis").attributes.get("href"), "/preview/?return_to=review");
    assert.equal(page.node("upload-error").hidden, false);
});

test("poll failures leave navigation blocked and schedule another check", async () => {
    const page = uploadPage({
        pending: true,
        fetch: async () => {
            throw new Error("Offline");
        }
    });
    await flush();
    assert.equal(page.node("continue-to-analysis").attributes.has("href"), false);
    assert.equal(page.timers.filter(timer => timer.delay === 5000).length, 1);
});

test("navigation opens once the wait limit passes, even while checks fail", async () => {
    const page = uploadPage({
        pending: true,
        fetch: async () => {
            throw new Error("Offline");
        }
    });
    await flush();
    const link = page.node("continue-to-analysis");
    assert.equal(link.attributes.has("href"), false);
    page.timers.find(timer => timer.delay === 120000).callback();
    assert.equal(link.attributes.has("href"), false);
    page.timers.filter(timer => timer.delay === 5000).at(-1).callback();
    await flush();
    assert.equal(link.attributes.get("href"), "/preview/?return_to=review");
    assert.match(page.node("upload-state-detail").textContent, /taking longer than usual/);
});

test("the page's own timer does not open navigation while the server is still waiting", async () => {
    const page = uploadPage({
        pending: true,
        fetch: async () => reply({
            ready: false,
            status: "pending",
            wait_seconds: 4
        })
    });
    await flush();
    page.timers.find(timer => timer.delay === 120000).callback();
    page.timers.filter(timer => timer.delay === 2500).at(-1).callback();
    await flush();
    assert.equal(page.node("continue-to-analysis").attributes.has("href"), false);
});

test("the server ending the wait opens navigation and keeps checking", async () => {
    const page = uploadPage({
        pending: true,
        fetch: async () => reply({
            ready: false,
            status: "pending",
            wait_seconds: 0
        })
    });
    await flush();
    assert.equal(page.node("continue-to-analysis").attributes.get("href"), "/preview/?return_to=review");
    assert.equal(page.timers.filter(timer => timer.delay === 2500).length, 1);
});

test("an upload failure keeps a ready result that arrived during the upload", async () => {
    let finishPoll;
    let failUpload;
    const page = uploadPage({
        pending: true,
        fetch: (url) => new Promise((resolve, reject) => {
            if (url === "/status/") finishPoll = resolve;
            else failUpload = reject;
        })
    });
    const upload = page.submit();
    finishPoll(reply({
        ready: true,
        status: "complete",
        pages_analyzed: 10,
        total_pages: 30
    }));
    await flush();
    assert.equal(page.node("upload-state-title").textContent, "Uploading your files…");
    failUpload(new Error("Upload failed"));
    await upload;
    assert.equal(page.node("upload-state").hidden, false);
    assert.equal(page.node("upload-state-title").textContent, "Document analysis is ready");
    assert.match(page.node("upload-state-detail").textContent, /first 10 of 30 pages/);
    assert.equal(page.node("continue-to-analysis").attributes.get("href"), "/preview/?return_to=review");
});
test("long waits reduce polling without delaying the manual-entry deadline", () => {
    const {
        extractionPollDelay
    } = require("../efile/static/js/upload-documents.js");
    let now = 0;
    let requests = 1;
    while (now < 120000) {
        const wait = Math.ceil((120000 - now) / 1000);
        now += extractionPollDelay(requests - 1, wait, () => 0.5);
        requests += 1;
    }
    assert.ok(now >= 120000 && now < 121000); // Server rounds remaining seconds upward.
    assert.ok(requests <= 20, `Expected at most 20 checks, got ${requests}`);
    assert.ok(requests < 49); // Prior fixed 2.5-second polling.
    for (let attempt = 0; attempt < 100; attempt++) {
        const delay = extractionPollDelay(attempt, 0, () => 0.5);
        assert.ok(delay >= 2000 && delay <= 10000);
    }
});

test("a pending response is the only source of the next poll", async () => {
    let resolve;
    const page = uploadPage({
        pending: true,
        fetch: () => new Promise(done => {
            resolve = done;
        })
    });
    assert.equal(page.timers.length, 1); // Only the fallback clock, no overlapping requests.
    resolve(reply({
        ready: true,
        status: "complete"
    }));
    await flush();
    assert.equal(page.timers.length, 1); // Terminal status stops polling.
});