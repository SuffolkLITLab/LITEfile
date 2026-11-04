const assert = require("node:assert/strict");
const fs = require("node:fs");
const {
    chromium
} = require("@playwright/test");

const FILING_TYPES = [{
    value: "initial",
    text: "Initial Filing"
}, {
    value: "initial-confidential",
    text: "Initial Filing – Confidential"
}, {
    value: "affidavit",
    text: "Affidavit"
}];

const WAIT = {
    timeout: 5000
};

function deferred() {
    let release;
    const released = new Promise((resolve) => {
        release = resolve;
    });
    return {
        released,
        release
    };
}

const documentTypes = (filingType) => [{
    value: `${filingType}-public`,
    text: "No (Public)",
    confidentiality: "non_confidential"
}, {
    value: `${filingType}-sealed`,
    text: "Yes (Sealed)",
    confidentiality: "confidential"
}];

const components = (filingType) => [{
    code: `${filingType}-lead`,
    name: "Lead Document",
    required: true
}, {
    code: `${filingType}-attachment`,
    name: "Attachment"
}];

const services = (filingType) => [{
    code: `${filingType}-certified`,
    name: "Certified copy",
    fee: "5.00"
}];

// Stands in for the court. Each lookup asks `handlers` how to answer, so a
// scenario can hold, fail or reorder exactly the requests it is about.
async function openPage(browser, config, handlers = {}, {
    waitForFilingCodes = true
} = {}) {
    const context = await browser.newContext();
    await context.addCookies([{
        name: "sessionid",
        value: config.cookie,
        url: config.baseUrl
    }]);
    const page = await context.newPage();
    const posts = [];
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));

    const lookup = (pattern, name, parameter, build) => page.route(pattern, async (route) => {
        const value = new URL(route.request().url()).searchParams.get(parameter);
        const answer = await handlers[name]?.(value, route);
        if (answer === "handled") return;
        await route.fulfill({
            json: {
                success: true,
                data: build(value)
            }
        });
    });
    await page.route("**/api/filing-availability/**", (route) => route.fulfill({
        json: {
            success: true,
            available: true,
            message: ""
        }
    }));
    await lookup("**/api/dropdowns/filing-types/**", "filingTypes", "court", () => FILING_TYPES);
    await lookup("**/case-filing-types/**", "filingTypes", "draft", () => FILING_TYPES);
    await lookup("**/api/dropdowns/document-types/**", "documentTypes", "parent", documentTypes);
    await lookup("**/api/get-filing-components/**", "components", "filing_type", components);
    await lookup("**/api/dropdowns/optional-services/**", "services", "filing_type_id", services);
    await page.route("**/organize-documents/**", async (route) => {
        if (route.request().method() !== "POST") {
            await route.continue();
            return;
        }
        posts.push(route.request().postDataJSON());
        await route.fulfill({
            status: 400,
            json: {
                success: false,
                error: "Captured by the test."
            }
        });
    });
    await page.goto(config.baseUrl + config.url);
    const cards = page.locator(".organize-card");
    if (waitForFilingCodes) {
        await cards.nth(1).locator(".filing-type option[value='affidavit']").waitFor({
            state: "attached",
            ...WAIT
        });
    }
    return {
        context,
        page,
        posts,
        pageErrors,
        lead: cards.nth(0),
        supporting: cards.nth(1)
    };
}

async function choose(card, filingType) {
    await card.locator(".filing-type").selectOption(filingType);
}

async function chooseAndAnswer(card, filingType, confidentiality = "public") {
    await choose(card, filingType);
    await card.locator(`.document-type-options input[value="${filingType}-${confidentiality}"]`).check(WAIT);
    await card.locator(`.filing-component-options input[value="${filingType}-lead"]`).waitFor({
        state: "attached",
        ...WAIT
    });
}

async function save(page) {
    await page.locator("#save-document-details").click();
}

async function savedDetails(page, posts) {
    const count = posts.length;
    await save(page);
    await page.waitForFunction(() => !document.getElementById("save-document-details").disabled, null, WAIT);
    assert.equal(posts.length, count + 1, "Save should reach the server");
    return posts.at(-1).documents;
}

const retryButton = (section) => section.getByRole("button", {
    name: "Try again"
});

const scenarios = {
    async "a normal response saves the selected choices"(browser, config) {
        const view = await openPage(browser, config);
        await chooseAndAnswer(view.lead, "initial");
        await chooseAndAnswer(view.supporting, "initial-confidential", "sealed");
        const documents = await savedDetails(view.page, view.posts);
        assert.equal(documents[1].filing_type, "initial-confidential");
        assert.equal(documents[1].document_type, "initial-confidential-sealed");
        assert.equal(documents[1].filing_component, "initial-confidential-lead");
        return view;
    },

    async "a late answer for an earlier filing code cannot replace the current choices"(browser, config) {
        const earlier = deferred();
        const answered = deferred();
        const holdEarlier = async (value) => {
            if (value !== "initial-confidential") return;
            answered.release();
            await earlier.released;
        };
        const view = await openPage(browser, config, {
            documentTypes: holdEarlier,
            components: holdEarlier,
            services: holdEarlier
        });
        await chooseAndAnswer(view.lead, "initial");
        await choose(view.supporting, "initial-confidential");
        await answered.released;
        await chooseAndAnswer(view.supporting, "affidavit", "sealed");
        earlier.release();
        await view.page.waitForTimeout(300);
        const sealed = view.supporting.locator('.document-type-options input[value="affidavit-sealed"]');
        assert.equal(await sealed.isChecked(), true, "the filer's answer should survive the late response");
        const documents = await savedDetails(view.page, view.posts);
        assert.equal(documents[1].filing_type, "affidavit");
        assert.equal(documents[1].document_type, "affidavit-sealed");
        assert.equal(documents[1].filing_component, "affidavit-lead");
        return view;
    },

    async "a late failure for an earlier filing code is ignored"(browser, config) {
        const earlier = deferred();
        const answered = deferred();
        const view = await openPage(browser, config, {
            documentTypes: async (value, route) => {
                if (value !== "initial-confidential") return undefined;
                answered.release();
                await earlier.released;
                await route.fulfill({
                    status: 400,
                    json: {
                        success: false,
                        error: "API request failed: court timeout"
                    }
                });
                return "handled";
            }
        });
        await choose(view.supporting, "initial-confidential");
        await answered.released;
        await chooseAndAnswer(view.supporting, "affidavit");
        earlier.release();
        await view.page.waitForTimeout(300);
        assert.equal(await view.page.locator("#organize-error").isVisible(), false);
        assert.equal(await retryButton(view.supporting).count(), 0);
        return view;
    },

    async "saving while choices load waits instead of sending an empty choice"(browser, config) {
        const hold = deferred();
        const view = await openPage(browser, config, {
            documentTypes: async (value) => {
                if (value === "initial-confidential") await hold.released;
            }
        });
        await chooseAndAnswer(view.lead, "initial");
        await choose(view.supporting, "initial-confidential");
        await save(view.page);
        await view.page.locator("#organize-error:not([hidden])").waitFor(WAIT);
        assert.equal(view.posts.length, 0);
        hold.release();
        await view.supporting.locator('.document-type-options input[value="initial-confidential-sealed"]').check(WAIT);
        const documents = await savedDetails(view.page, view.posts);
        assert.equal(documents[1].document_type, "initial-confidential-sealed");
        return view;
    },

    async "a failed confidentiality lookup can be retried without losing other answers"(browser, config) {
        let failures = 1;
        const view = await openPage(browser, config, {
            documentTypes: async (value, route) => {
                if (value !== "initial-confidential" || failures-- <= 0) return undefined;
                await route.fulfill({
                    status: 400,
                    json: {
                        success: false,
                        error: "API request failed: HTTPSConnectionPool(host='internal')"
                    }
                });
                return "handled";
            }
        });
        await chooseAndAnswer(view.lead, "initial", "sealed");
        await view.supporting.locator(".document-name").fill("Affidavit of Jordan Example");
        await choose(view.supporting, "initial-confidential");
        const section = view.supporting.locator(".document-type-options");
        await retryButton(section).waitFor(WAIT);
        assert.doesNotMatch(await section.innerText(), /HTTPSConnectionPool/);

        await view.supporting.locator(".filing-component-options input").first().waitFor({
            state: "attached",
            ...WAIT
        });
        await save(view.page);
        await view.page.locator("#organize-error:not([hidden])").waitFor(WAIT);
        assert.equal(view.posts.length, 0, "a failed required choice must not be saved as blank");

        await retryButton(section).click();
        await section.locator('input[value="initial-confidential-sealed"]').check(WAIT);
        const documents = await savedDetails(view.page, view.posts);
        assert.equal(documents[0].document_type, "initial-sealed");
        assert.equal(documents[1].name, "Affidavit of Jordan Example");
        assert.equal(documents[1].filing_type, "initial-confidential");
        assert.equal(documents[1].document_type, "initial-confidential-sealed");
        return view;
    },

    async "a failed component lookup leaves the confidentiality answer in place"(browser, config) {
        let failures = 1;
        const view = await openPage(browser, config, {
            components: async (value, route) => {
                if (value !== "initial-confidential" || failures-- <= 0) return undefined;
                await route.fulfill({
                    status: 503,
                    json: {
                        success: false,
                        error: "Failed to connect to filing components API"
                    }
                });
                return "handled";
            }
        });
        await chooseAndAnswer(view.lead, "initial");
        await choose(view.supporting, "initial-confidential");
        const sealed = view.supporting.locator('.document-type-options input[value="initial-confidential-sealed"]');
        await sealed.check(WAIT);
        const section = view.supporting.locator(".filing-component-options");
        await retryButton(section).click(WAIT);
        await section.locator('input[value="initial-confidential-lead"]').waitFor({
            state: "attached",
            ...WAIT
        });
        assert.equal(await sealed.isChecked(), true);
        const documents = await savedDetails(view.page, view.posts);
        assert.equal(documents[1].filing_component, "initial-confidential-lead");
        return view;
    },

    async "a proxy error page reads as a plain failure"(browser, config) {
        const view = await openPage(browser, config, {
            documentTypes: async (value, route) => {
                await route.fulfill({
                    status: 502,
                    contentType: "text/html",
                    body: "<html>Bad gateway</html>"
                });
                return "handled";
            }
        });
        await choose(view.supporting, "initial-confidential");
        const section = view.supporting.locator(".document-type-options");
        await retryButton(section).waitFor(WAIT);
        assert.doesNotMatch(await view.page.locator("body").innerText(), /Unexpected token|not valid JSON/);
        return view;
    },

    async "the confidentiality field reappears while the next filing code loads"(browser, config) {
        const hold = deferred();
        const view = await openPage(browser, config, {
            documentTypes: async (value, route) => {
                if (value === "initial") {
                    await route.fulfill({
                        json: {
                            success: true,
                            data: []
                        }
                    });
                    return "handled";
                }
                await hold.released;
                return undefined;
            }
        });
        const section = view.supporting.locator(".document-type-options");
        await choose(view.supporting, "initial");
        await section.locator("xpath=ancestor::fieldset").waitFor({
            state: "hidden",
            ...WAIT
        });
        await choose(view.supporting, "affidavit");
        await section.waitFor({
            state: "visible",
            ...WAIT
        });
        hold.release();
        await section.locator('input[value="affidavit-sealed"]').waitFor({
            state: "attached",
            ...WAIT
        });
        return view;
    },

    async "the code finder on an existing case loads that code's choices"(browser, config) {
        const view = await openPage(browser, {
            ...config,
            ...config.existingCase
        });
        await chooseAndAnswer(view.lead, "initial");
        await view.supporting.locator(".filing-type-finder summary").click();
        await view.supporting.locator(".filing-search").fill("affidavit");
        await view.supporting.locator(".filing-search-results button").click(WAIT);
        await view.supporting.locator('.document-type-options input[value="affidavit-sealed"]').check(WAIT);
        const documents = await savedDetails(view.page, view.posts);
        assert.equal(documents[1].filing_type, "affidavit");
        assert.equal(documents[1].document_type, "affidavit-sealed");
        return view;
    },

    async "the filing code list is asked for once and can be retried"(browser, config) {
        let requests = 0;
        const view = await openPage(browser, config, {
            filingTypes: async (value, route) => {
                requests += 1;
                if (requests > 1) return undefined;
                await route.fulfill({
                    status: 503,
                    json: {
                        success: false,
                        error: "unavailable"
                    }
                });
                return "handled";
            }
        }, {
            waitForFilingCodes: false
        });
        await retryButton(view.supporting).waitFor(WAIT);
        assert.equal(requests, 1, "every document should share one request for the list");
        await view.lead.locator(".document-name").fill("Complaint for Jordan Example");
        await retryButton(view.lead).click(WAIT);
        await view.supporting.locator(".filing-type option[value='affidavit']").waitFor({
            state: "attached",
            ...WAIT
        });
        await view.lead.locator(".filing-type option[value='affidavit']").waitFor({
            state: "attached",
            ...WAIT
        });
        assert.equal(await view.lead.locator(".document-name").inputValue(), "Complaint for Jordan Example");
        await chooseAndAnswer(view.lead, "initial");
        await chooseAndAnswer(view.supporting, "affidavit");
        const documents = await savedDetails(view.page, view.posts);
        assert.equal(documents[0].name, "Complaint for Jordan Example");
        return view;
    },
};

(async () => {
    const config = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
    const browser = await chromium.launch();
    const failures = [];
    for (const [name, scenario] of Object.entries(scenarios)) {
        try {
            const view = await scenario(browser, config);
            assert.deepEqual(view.pageErrors, []);
            await view.context.close();
            console.log(`ok - ${name}`);
        } catch (error) {
            failures.push(name);
            console.log(`not ok - ${name}\n    ${String(error.message).split("\n")[0]}`);
        }
    }
    await browser.close();
    if (failures.length) process.exit(1);
})();