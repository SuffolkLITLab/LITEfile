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
    asyncPreparation = false,
    fetch
}) {
    const nodes = new Map();

    function node(id) {
        if (!nodes.has(id)) nodes.set(id, {
            dataset: {},
            attributes: new Map(),
            listeners: {},
            children: [],
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
            append(...children) {
                this.children.push(...children);
            },
            replaceChildren() {
                this.children = [];
            },
            showModal() {
                this.open = true;
            },
            close() {
                this.open = false;
            },
            requestSubmit() {
                this.submission = this.listeners.submit({
                    preventDefault() {}
                });
            },
            querySelector() {
                if (id === "main-document-choice-form") return node("main-document-choices").children.map(label => label.children[0]).find(radio => radio.checked);
                if (id === "document-drop-zone") return node("drop-zone-label");
                if (id === "upload-state") return node("upload-spinner");
                return null;
            }
        });
        return nodes.get(id);
    }
    node("document-upload-form").dataset = {
        hasLead: String(hasLead),
        extractionPending: String(pending),
        extractionStatusUrl: "/status/",
        asyncPreparation: String(asyncPreparation),
        waitSeconds: String(waitSeconds)
    };
    node("continue-to-analysis").dataset.continueUrl = "/preview/?return_to=review";
    node("upload-state").hidden = !pending;
    const timers = [];
    let reloads = 0;
    const context = vm.createContext({
        document: {
            createElement: tag => node(Symbol(tag)),
            getElementById: node,
            querySelector: () => null,
            querySelectorAll: () => []
        },
        window: {
            location: {
                href: "https://upload.test/upload/",
                reload: () => reloads++
            },
            setTimeout: (callback, delay) => timers.push({
                callback,
                delay
            })
        },
        apiUtils: {
            getCSRFToken: () => "csrf"
        },
        AbortSignal,
        Headers,
        URL,
        DataTransfer: class {
            files = [];
            items = {
                add: file => this.files.push(file)
            };
        },
        FormData: class {
            constructor() {
                this.files = [...(node("documents-input").files || [])];
            }
            get() {
                return "b65359a8-c34b-44a7-8d2e-3422f765e71a";
            }
        },
        fetch
    });
    // eslint-disable-next-line sonarjs/code-eval
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../efile/static/js/document-requests.js"), "utf8"), context);
    // eslint-disable-next-line sonarjs/code-eval
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../efile/static/js/upload-documents.js"), "utf8"), context);
    return {
        node,
        timers,
        reloads: () => reloads,
        submit() {
            const input = node("documents-input");
            if (!input.files) {
                input.files = [pdf("petition.pdf", 100, 1)];
                input.listeners.change();
            }
            return node("document-upload-form").listeners.submit({
                preventDefault() {}
            });
        }
    };
}

function reply(data) {
    return Response.json({
        success: true,
        ...data
    });
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
    assert.equal(page.timers.filter(timer => timer.delay === 2500).length, 1);
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
    page.timers.find(timer => timer.delay === 2500).callback();
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
    assert.equal(page.node("upload-spinner").hidden, true);
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

test("an unavailable draft keeps the selected files and tells the filer where to reopen it", async () => {
    const page = uploadPage({
        fetch: async () => new Response("<!DOCTYPE html>", {
            status: 409
        })
    });
    await page.submit();
    assert.match(page.node("upload-error").textContent, /Open My drafts/);
    assert.equal(page.node("upload-button").disabled, true);
    assert.equal(page.node("documents-input").files[0].name, "petition.pdf");
    assert.equal(page.reloads(), 0);
});

for (const recorded of [true, false]) {
    test(`a lost upload response checks its token before a retry (recorded: ${recorded})`, async () => {
        const requests = [];
        const page = uploadPage({
            asyncPreparation: true,
            fetch: async (url, options) => {
                requests.push(options.method || "GET");
                if (options.method === "POST") {
                    return new Response("<!DOCTYPE html>", {
                        status: 502
                    });
                }
                assert.equal(url.pathname, "/status/");
                assert.match(url.searchParams.get("upload_request_token"), /^b65359a8-/);
                return reply({
                    upload_recorded: recorded
                });
            }
        });
        await page.submit();
        assert.deepEqual(requests, ["POST", "GET"]);
        assert.equal(page.reloads(), Number(recorded));
        assert.equal(page.node("upload-button").disabled, recorded);
        assert.equal(page.node("documents-input").files[0].name, "petition.pdf");
    });
}
for (const preparation of [true, false]) {
    test(`accepted upload keeps processing visible until reload (preparation: ${preparation})`, async () => {
        const page = uploadPage({
            hasLead: false,
            fetch: async () => reply({
                preparation_pending: preparation,
                extraction_pending: !preparation
            })
        });
        await page.submit();
        assert.equal(page.node("upload-state").hidden, false);
        assert.equal(page.node("upload-state-title").textContent, "Processing your documents…");
        assert.equal(page.node("continue-to-analysis").attributes.has("href"), false);
        assert.equal(page.reloads(), 0);
        page.timers.find(timer => timer.delay === 300).callback();
        assert.equal(page.reloads(), 1);
    });
}
test("uploading again restores the activity indicator after analysis completed", async () => {
    let finishUpload;
    const page = uploadPage({
        pending: true,
        fetch: url => {
            if (url === "/status/") return Promise.resolve(reply({
                ready: true,
                status: "complete"
            }));
            return new Promise(resolve => {
                finishUpload = resolve;
            });
        }
    });
    await flush();
    assert.equal(page.node("upload-spinner").hidden, true);
    const upload = page.submit();
    assert.equal(page.node("upload-spinner").hidden, false);
    finishUpload(reply({
        preparation_pending: true
    }));
    await upload;
});

test("a failed later upload restores the completed analysis indicator", async () => {
    const page = uploadPage({
        pending: true,
        fetch: async url => {
            if (url === "/status/") return reply({
                ready: true,
                status: "complete"
            });
            throw new Error("Upload failed");
        }
    });
    await flush();
    assert.equal(page.node("upload-spinner").hidden, true);
    await page.submit();
    assert.equal(page.node("upload-spinner").hidden, true);
    assert.equal(page.node("upload-state-title").textContent, "Document analysis is ready");
    assert.equal(page.node("upload-error").hidden, false);
});

test("multiple initial files wait for an explicit main-document choice", async () => {
    const sent = [];
    const page = uploadPage({
        hasLead: false,
        fetch: async (_url, options) => {
            sent.push(options.body.files);
            return reply({
                preparation_pending: true
            });
        }
    });
    const input = page.node("documents-input");
    input.files = [pdf("exhibit.pdf", 100, 1), pdf("petition.pdf", 200, 2), pdf("waiver.pdf", 300, 3)];
    input.listeners.change();
    await page.submit();
    assert.equal(sent.length, 0);
    assert.equal(page.node("main-document-dialog").open, true);
    const radios = page.node("main-document-choices").children.map(label => label.children[0]);
    assert.equal(radios.length, 3);
    assert.ok(radios.every(radio => radio.required && !radio.checked));
    await page.node("main-document-choice-form").listeners.submit({
        preventDefault() {}
    });
    assert.equal(sent.length, 0);
    radios[1].checked = true;
    page.node("main-document-choice-form").listeners.submit({
        preventDefault() {}
    });
    await page.node("document-upload-form").submission;
    assert.equal(page.node("main-document-dialog").open, false);
    assert.deepEqual(Array.from(sent[0], file => file.name), ["petition.pdf", "exhibit.pdf", "waiver.pdf"]);
});

test("cancel keeps selected files and asks again without starting an upload", async () => {
    let uploads = 0;
    const page = uploadPage({
        hasLead: false,
        fetch: async () => {
            uploads++;
            return reply({});
        }
    });
    const input = page.node("documents-input");
    input.files = [pdf("one.pdf", 100, 1), pdf("two.pdf", 200, 2)];
    input.listeners.change();
    await page.submit();
    page.node("main-document-cancel").listeners.click();
    assert.equal(page.node("main-document-dialog").open, false);
    assert.equal(input.files.length, 2);
    assert.equal(uploads, 0);
    await page.submit();
    assert.equal(page.node("main-document-dialog").open, true);
});

test("identical filenames still select the intended file by identity", async () => {
    let sent;
    const page = uploadPage({
        hasLead: false,
        fetch: async (_url, options) => {
            sent = options.body.files;
            return reply({});
        }
    });
    const input = page.node("documents-input");
    input.files = [pdf("same.pdf", 100, 1), pdf("same.pdf", 200, 2)];
    input.listeners.change();
    await page.submit();
    page.node("main-document-choices").children[1].children[0].checked = true;
    page.node("main-document-choice-form").listeners.submit({
        preventDefault() {}
    });
    await page.node("document-upload-form").submission;
    assert.deepEqual(Array.from(sent, file => file.size), [200, 100]);
});

test("a single file or an existing main document does not require a new choice", async () => {
    for (const hasLead of [false, true]) {
        let uploads = 0;
        const page = uploadPage({
            hasLead,
            fetch: async () => {
                uploads++;
                return reply({});
            }
        });
        const input = page.node("documents-input");
        input.files = [pdf("one.pdf", 100, 1)];
        if (hasLead) input.files.push(pdf("two.pdf", 200, 2));
        input.listeners.change();
        await page.submit();
        assert.equal(uploads, 1);
        assert.ok(!page.node("main-document-dialog").open);
    }
});