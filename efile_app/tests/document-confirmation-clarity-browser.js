const assert = require("node:assert/strict");
const fs = require("node:fs");
const {
    chromium,
    expect
} = require("@playwright/test");
const {
    default: AxeBuilder
} = require("@axe-core/playwright");

(async () => {
    const config = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
    const browser = await chromium.launch();
    try {
        for (const width of [1280, 390]) {
            const context = await browser.newContext({
                viewport: {
                    width,
                    height: 900
                }
            });
            await context.addCookies([{
                name: "sessionid",
                value: config.cookie,
                url: config.baseUrl
            }]);
            const page = await context.newPage();
            const errors = [];
            page.on("pageerror", error => errors.push(error.message));
            await page.route("**/api/**", async route => {
                const path = new URL(route.request().url()).pathname;
                let data = [];
                if (path.endsWith("/court-selector/")) data = {
                    available: false
                };
                if (path.endsWith("/courts/")) data = [{
                    value: "court",
                    text: "Example court"
                }];
                if (path.endsWith("/case-categories/")) data = [{
                    value: "civil",
                    text: "Civil"
                }];
                if (path.endsWith("/case-types/")) data = [{
                    value: "contract",
                    text: "Contract"
                }];
                if (path.endsWith("/filing-types/")) data = [{
                    value: "complaint",
                    text: "Complaint"
                }, {
                    value: "exhibit",
                    text: "Exhibit"
                }];
                await route.fulfill({
                    json: {
                        success: true,
                        data,
                        available: true
                    }
                });
            });
            await page.goto(config.baseUrl + config.url);
            if (config.inferred) {
                await expect(page.locator("#path-question")).toBeHidden();
                await expect(page.locator("#docket_number")).toBeVisible();
                await expect(page.locator("#docket_number")).toHaveValue("26-CV-123");
                await page.locator("#change-filing-path").click();
                await page.locator('input[name="existing_case"][value="new"]').check();
            }
            const supporting = page.locator('[data-confirmation-document][data-is-source="supporting"] select');
            await expect(supporting).toBeEnabled();
            await expect(supporting).toHaveValue("exhibit");
            await supporting.selectOption("complaint");
            await expect(page.locator("#filing_type_code")).toHaveValue("complaint");
            await expect(page.getByLabel("Exhibit.pdf — Type of document (filing code)")).toHaveValue("complaint");
            await expect(page.locator('[data-confirmation-document][data-is-source="lead"]')).toBeHidden();
            const audit = await new AxeBuilder({
                page
            }).include("#extraction-review-form").analyze();
            assert.deepEqual(audit.violations, []);
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
            assert.deepEqual(errors, []);
            await context.close();
        }
    } finally {
        await browser.close();
    }
})().catch(error => {
    process.stderr.write(`${error.stack}\n`);
    process.exitCode = 1;
});