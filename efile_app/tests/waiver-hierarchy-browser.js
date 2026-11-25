/* Real upload, PDF review and submission against the synthetic Django test proxy. */
const fs = require("node:fs");
const path = require("node:path");
const {
    chromium,
    expect
} = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

async function main() {
    const config = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
    const browser = await chromium.launch({
        executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined
    });
    try {
        const context = await browser.newContext({
            viewport: {
                width: 1280,
                height: 1000
            }
        });
        await context.addCookies([{
            name: config.cookieName,
            value: config.cookie,
            url: config.baseUrl
        }]);
        const page = await context.newPage();
        fs.mkdirSync(config.evidence, {
            recursive: true
        });
        const shot = name => page.screenshot({
            path: path.join(config.evidence, `${config.scenario}-${name}.png`),
            fullPage: true
        });
        const chooseWaiver = () => page.locator('input[name="paymentIntent"][value="waiver"]').check();
        await page.goto(config.baseUrl + config.paymentUrl);
        await chooseWaiver();
        await page.locator("#add-waiver-document").click();
        await expect(page.locator("#waiver-filing-type")).toHaveValue(config.scenario);
        expect(await page.locator("#waiver-filing-type option").evaluateAll(options => options.map(option => option.value))).toEqual(config.expectedCodes);
        await shot("court-choices");
        await page.locator("#waiver-document-type").selectOption("private");
        await page.locator("#waiver-file").setInputFiles(config.waiverFile);
        await page.locator("#upload-waiver-document").click();
        const check = page.locator("[data-document-check]");
        await expect(check).toHaveCount(1);
        await expect(page.locator("#submitButton")).toBeDisabled();
        await page.reload();
        await chooseWaiver();
        await expect(page.locator("#waiver-upload-required")).toBeHidden();
        await expect(check).toHaveCount(1);
        await expect(check.locator("[data-pdf-download]")).toBeVisible();
        await expect(check.locator("[data-pdf-preview]")).toHaveCount(0);
        await shot("review-upload");
        const accessibility = await new AxeBuilder({
            page
        }).include(".workflow-card").withTags(["wcag2a", "wcag2aa"]).analyze();
        expect(accessibility.violations).toEqual([]);
        await page.setViewportSize({
            width: 390,
            height: 844
        });
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
        await shot("mobile-upload");
        await check.locator("[data-document-confirm]").focus();
        await page.keyboard.press("Enter");
        await expect(check).toHaveCount(0);
        await page.setViewportSize({
            width: 1280,
            height: 1000
        });
        await page.locator("#submitButton").click();
        await expect(page.locator("#confirm-filing")).toBeVisible();
        await expect(page.locator(".workflow-card")).toContainText(config.expectedName);
        await shot("review");
        await page.locator("#confirm-filing").check();
        const submitted = page.waitForResponse(response => response.url().includes("/api/submit-final-filing/"));
        await page.locator("#submitButton").click();
        const response = await submitted;
        expect(response.status(), await response.text()).toBe(200);
        await expect(page).toHaveURL(/filing-confirmation/);
        await shot("confirmation");
        console.log(`${config.scenario}: upload → reload → PDF check → Review → synthetic submission passed`);
    } finally {
        await browser.close();
    }
}
main().catch(error => {
    console.error(error);
    process.exitCode = 1;
});