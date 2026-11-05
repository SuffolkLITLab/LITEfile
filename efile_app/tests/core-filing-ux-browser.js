const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
    chromium
} = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

// Massachusetts asks its guided court questions only when the case number
// does not settle the court, and opens them on the inferred court when the
// filer asks to change it.
async function checkCourtLookup(browser, config) {
    const errors = [];
    async function open(scenario) {
        const context = await browser.newContext({
            viewport: {
                width: 390,
                height: 844
            }
        });
        await context.addCookies([{
            name: "sessionid",
            value: scenario.cookie,
            url: config.baseUrl
        }]);
        const page = await context.newPage();
        page.on("pageerror", error => errors.push(`${scenario.name}: ${error.message}`));
        await page.route("**/api/filing-availability/**", route => route.fulfill({
            json: {
                success: true,
                available: true,
                message: ""
            }
        }));
        await page.goto(config.baseUrl + scenario.url);
        return {
            context,
            page
        };
    }
    const lookup = config.courtLookup;
    const questions = "#court-selector .court-selector__steps";
    const isVisible = (page, selector) => page.locator(selector).isVisible();
    const screenshot = (page, name) => page.screenshot({
        path: path.join(config.evidence, `court-lookup-${name}.png`),
        fullPage: true
    });

    let {
        context,
        page
    } = await open(lookup.blank);
    try {
        await page.locator(questions).waitFor();
        assert.equal(await isVisible(page, "#court"), false);
        assert.equal(await isVisible(page, "#inferred-court"), false);
        await screenshot(page, "blank");
        await page.locator("#case-number").fill("1448CV001026");
        await page.locator("#inferred-court:not([hidden])").waitFor();
        assert.equal(await isVisible(page, "#manual-court-field"), false);
        assert.equal(await page.locator("#court").inputValue(), "336");
        await page.getByRole("button", {
            name: "Change court"
        }).click();
        await page.locator("#manual-court-field:not([hidden])").waitFor();
        await page.waitForFunction(() => document.getElementById("court-selector").contains(document.activeElement));
        assert.equal(await page.locator("#court").inputValue(), "336");
        // A court the filer chose is not replaced by a later number.
        await page.locator("#case-number").fill("ES15A0064AD");
        await page.waitForTimeout(800);
        assert.equal(await isVisible(page, "#inferred-court"), false);
        assert.equal(await page.locator("#court").inputValue(), "336");
        await screenshot(page, "changed");
    } finally {
        await context.close();
    }

    ({
        context,
        page
    } = await open(lookup.verified));
    try {
        await page.locator("#inferred-court:not([hidden])").waitFor();
        assert.equal(await page.locator(questions).count(), 0);
        await page.locator("#case-number").fill("98-1234");
        await page.locator(questions).waitFor();
        assert.equal(await isVisible(page, "#inferred-court"), false);
        assert.notEqual(await page.locator("#court").inputValue(), "336");
        await page.locator("#case-number").fill("1473CV00213");
        await page.locator("#inferred-court:not([hidden])").waitFor();
        assert.equal(await page.locator("#court").inputValue(), "1126");
        assert.equal(await isVisible(page, "#manual-court-field"), false);
        await screenshot(page, "reinferred");
    } finally {
        await context.close();
    }
    assert.deepEqual(errors, []);
    console.log("court lookup passed");
}

async function main() {
    const config = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
    const browser = await chromium.launch();
    fs.mkdirSync(config.evidence, {
        recursive: true
    });
    try {
        for (const scenario of config.scenarios) {
            const context = await browser.newContext({
                viewport: {
                    width: 390,
                    height: 844
                }
            });
            await context.addCookies([{
                name: "sessionid",
                value: scenario.cookie,
                url: config.baseUrl
            }]);
            const page = await context.newPage();
            const errors = [];
            page.on("pageerror", error => errors.push(error.message));
            await page.route("**/api/dropdowns/**", route => {
                const endpoint = new URL(route.request().url()).pathname;
                if (endpoint.endsWith("court-selector/")) return route.fulfill({
                    json: {
                        success: true,
                        data: {
                            available: false
                        }
                    }
                });
                let data = [];
                if (endpoint.endsWith("courts/")) data = [{
                    value: scenario.court,
                    text: scenario.courtName
                }];
                if (endpoint.endsWith("document-types/")) data = [{
                    value: "public",
                    text: "Public"
                }];
                return route.fulfill({
                    json: {
                        success: true,
                        data
                    }
                });
            });
            await page.route("**/api/suffolk/lookup-case/**", route => route.fulfill({
                json: {
                    success: true,
                    caseInfo: {
                        caseTrackingID: "verified",
                        caseDocketID: scenario.number,
                        caseTitle: "Synthetic case",
                        caseCategoryCode: "civil",
                        caseCategoryName: "Civil",
                        caseTypeCode: "contract",
                        caseTypeName: "Contract"
                    }
                }
            }));
            await page.route("**/api/get-filing-components/**", route => route.fulfill({
                json: {
                    success: true,
                    data: []
                }
            }));
            await page.route("**/your-information/**", route => route.fulfill({
                body: "Saved synthetic filing"
            }));
            await page.route("**/api/filing-availability/**", route => route.fulfill({
                json: {
                    success: true,
                    available: true,
                    message: ""
                }
            }));
            try {
                await page.goto(config.baseUrl + scenario.url);
                assert.equal(await page.locator("#new-case-details").isVisible(), false);
                assert.equal(await page.locator("#review-parties").count(), 0);
                await page.screenshot({
                    path: path.join(config.evidence, `${scenario.name}-confirm.png`),
                    fullPage: true
                });
                assert.equal(await page.locator("#filing-proposal input[type='text']").count(), 0);
                if (scenario.rejectGuess) await page.locator("#reject-filing-proposal").check();
                await page.getByRole("button", {
                    name: "Continue",
                    exact: true
                }).click();
                await page.waitForURL(/case-lookup/);
                if (scenario.name === "modern") {
                    await page.locator("#inferred-court:not([hidden])").waitFor();
                    assert.equal(await page.locator("#court").inputValue(), "336");
                    await page.getByRole("button", {
                        name: "Change court"
                    }).click();
                }
                await page.locator("#court").selectOption(scenario.court);
                await page.getByRole("button", {
                    name: "Find my case"
                }).click();
                await page.waitForURL(/case-confirmation/);
                assert.match(await page.locator(".case-summary").innerText(), /Synthetic case/);
                assert.equal(await page.locator(".case-summary input").count(), 0);
                await page.getByRole("button", {
                    name: "Yes, this is my case"
                }).click();
                await page.waitForURL(/document-checklist/);
                await page.getByRole("link", {
                    name: "Find my document type",
                    exact: true
                }).click();
                await page.waitForURL(/organize-documents/);
                await page.locator(".filing-type option[value='motion']").waitFor({
                    state: "attached"
                });
                if (scenario.name === "modern") {
                    assert.match(await page.locator(".filing-suggestion-message").innerText(), /not on the court’s list/);
                    assert.equal(await page.locator(".filing-type").inputValue(), "");
                } else if (scenario.rejectGuess) {
                    assert.equal(await page.locator(".filing-suggestion-message").isVisible(), false);
                    assert.equal(await page.locator(".filing-type").inputValue(), "");
                } else {
                    assert.equal(await page.locator(".filing-type").inputValue(), "motion");
                }
                await page.locator(".filing-search").fill("motion");
                if (scenario.name === "modern") {
                    await page.locator(".filing-search").press("Tab");
                    await page.keyboard.press("Enter");
                    assert.equal(await page.locator(".filing-type").evaluate(element => element === document.activeElement), true);
                } else {
                    await page.locator(".filing-search-results").getByRole("button", {
                        name: "Motion",
                        exact: true
                    }).click();
                }
                assert.equal(await page.locator(".filing-suggestion-message").isVisible(), false);
                await page.locator(".document-type-options input[value='public']").check();
                const a11y = await new AxeBuilder({
                    page
                }).include(".workflow-card").analyze();
                assert.deepEqual(a11y.violations.map(item => item.id), []);
                assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
                await page.screenshot({
                    path: path.join(config.evidence, `${scenario.name}.png`),
                    fullPage: true
                });
                const saved = page.waitForResponse(response => response.url().includes("organize-documents") && response.request().method() === "POST");
                await page.getByRole("button", {
                    name: "Save and continue"
                }).click();
                assert.equal((await saved).status(), 200);
                await page.waitForURL(/your-information/);
                await page.goto(config.baseUrl + scenario.url);
                assert.equal(await page.locator("#reject-filing-proposal").isChecked(), scenario.rejectGuess);
                assert.deepEqual(errors, []);
                console.log(`${scenario.name} passed`);
            } catch (error) {
                await page.screenshot({
                    path: path.join(config.evidence, `${scenario.name}-failure.png`),
                    fullPage: true
                });
                throw error;
            } finally {
                await context.close();
            }
        }
        if (config.courtLookup) await checkCourtLookup(browser, config);
    } finally {
        await browser.close();
    }
}
main().catch(error => {
    console.error(error);
    process.exitCode = 1;
});