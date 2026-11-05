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
            await expect(supporting).toHaveAccessibleName("Exhibit.pdf Type of document");
            await expect(page.locator('[data-confirmation-document][data-is-source="lead"]')).toBeHidden();
            await expect(page.locator("#document-type-definition")).toBeVisible();
            await expect(page.locator("#new-case-document")).toBeVisible();
            await page.locator(".extracted-details summary").click();
            if (width > 700) {
                const category = await page.locator('[data-field="case_category"] .review-field__display:visible, [data-field="case_category"] .review-field__input:visible').boundingBox();
                const caseType = await page.locator('[data-field="case_type"] .review-field__display:visible, [data-field="case_type"] .review-field__input:visible').boundingBox();
                assert.ok(Math.abs(category.y - caseType.y) <= 1, 'case fields align');
                const plaintiff = await page.locator('[data-extracted-detail="plaintiff or petitioner names"]').boundingBox();
                const defendant = await page.locator('[data-extracted-detail="defendant or respondent names"]').boundingBox();
                assert.ok(Math.abs(plaintiff.y - defendant.y) <= 1, 'opposing parties share a row');
                for (const row of await page.locator('.review-party').all()) {
                    const name = await row.locator('[name="party_name"]').boundingBox();
                    const side = await row.locator('[name="party_side"]').boundingBox();
                    assert.ok(Math.abs(name.y - side.y) <= 1, 'name and side inputs align despite help text');
                }
            }
            const audit = await new AxeBuilder({
                page
            }).include("#extraction-review-form").analyze();
            assert.deepEqual(audit.violations, []);
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
            assert.deepEqual(errors, []);
            await page.goto(config.baseUrl + config.partiesUrl);
            const staffChoice = page.locator('#filer-not-a-party');
            await expect(staffChoice).toBeVisible();
            assert.equal(await staffChoice.evaluate(el => !!el.closest('details')), false);
            const moreRoles = page.locator('#your-role details');
            await expect(moreRoles).toHaveCount(1);
            assert.equal(await moreRoles.evaluate(el => el.open), false);
            assert.equal(await staffChoice.evaluate(el => !!(el.closest('label').compareDocumentPosition(document.querySelector('#your-role details')) & Node.DOCUMENT_POSITION_FOLLOWING)), true);
            await context.close();
        }
    } finally {
        await browser.close();
    }
})().catch(error => {
    process.stderr.write(`${error.stack}\n`);
    process.exitCode = 1;
});