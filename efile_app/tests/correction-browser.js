const assert = require("node:assert/strict");
const fs = require("node:fs");
const {
    chromium
} = require("@playwright/test");

(async () => {
    const config = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
    const browser = await chromium.launch({
        headless: true
    });
    try {
        const context = await browser.newContext({
            viewport: {
                width: 390,
                height: 844
            }
        });
        await context.addCookies([{
            name: "sessionid",
            value: config.cookie,
            url: config.baseUrl
        }]);
        const page = await context.newPage();
        const capture = async name => {
            if (!config.screenshotDir) return;
            fs.mkdirSync(config.screenshotDir, {
                recursive: true
            });
            await page.screenshot({
                path: `${config.screenshotDir}/${config.scenario}-${name}.png`,
                fullPage: true
            });
        };
        page.setDefaultTimeout(12000);
        const errors = [];
        page.on("pageerror", error => errors.push(error.message));
        await page.route("**/api/dropdowns/filing-types/**", route => route.fulfill({
            json: {
                success: true,
                data: [{
                    value: "petition",
                    text: "Petition"
                }]
            }
        }));
        await page.route("**/api/dropdowns/document-types/**", route => route.fulfill({
            json: {
                success: true,
                data: [{
                    value: "public",
                    text: "Public"
                }]
            }
        }));
        await page.route("**/api/get-filing-components/**", route => route.fulfill({
            json: {
                success: true,
                data: [{
                    value: "lead",
                    text: "Lead document"
                }]
            }
        }));
        await page.route("**/api/dropdowns/optional-services/**", route => route.fulfill({
            json: {
                success: true,
                data: []
            }
        }));
        await page.route("**/api/filing-availability/**", route => route.fulfill({
            json: {
                success: true,
                available: true
            }
        }));
        await page.route("**/api/payment-accounts/**", route => route.fulfill({
            json: {
                success: true,
                data: config.accounts
            }
        }));
        await page.route("**/api/payment-account-types/**", route => route.fulfill({
            json: {
                success: true,
                data: []
            }
        }));
        await page.goto(config.baseUrl + config.url);
        await capture("01-original");
        await page.getByRole("button", {
            name: "Copy and review filing"
        }).focus();
        await page.keyboard.press("Enter");
        await page.getByRole("heading", {
            name: "Review your corrected filing"
        }).waitFor();
        assert.equal(await page.locator("#submitButton").isDisabled(), true);
        const reviewUrl = page.url();
        await capture("02-review-blockers");
        await page.getByRole("link", {
            name: "Edit documents",
            exact: true
        }).click();
        await page.locator(".filing-type").selectOption("petition");
        await page.locator(".document-name").fill("Corrected petition.pdf");
        await page.getByRole("button", {
            name: "Save and return to review"
        }).click();
        await page.getByRole("heading", {
            name: "Review your corrected filing"
        }).waitFor();
        assert.equal(page.url(), reviewUrl);
        await page.getByRole("link", {
            name: "Choose a payment method or fee waiver and check fees."
        }).first().click();
        await page.waitForLoadState("load");
        await page.locator('input[name="paymentIntent"][value="pay"]').check();
        await page.locator("#submitButton:not([disabled])").waitFor().catch(async error => {
            throw new Error(error.message + "\n" + errors.join("\n") + "\n" + await page.locator("body").innerText());
        });
        await page.locator("#submitButton").click();
        await page.getByRole("heading", {
            name: "Review your corrected filing"
        }).waitFor();
        await page.locator("#confirm-filing").check();
        await page.locator("#submitButton:not([disabled])").waitFor();
        await capture("03-ready-mobile");
        if (config.screenshotDir) {
            await page.setViewportSize({
                width: 1440,
                height: 1000
            });
            await capture("04-ready-desktop");
            await page.setViewportSize({
                width: 390,
                height: 844
            });
        }
        await page.locator("#submitButton").click();
        await page.getByRole("heading", {
            name: "Your filing was sent to the court"
        }).waitFor();
        assert.equal(await page.getByRole("link", {
            name: "View the original filing"
        }).count(), 1);
        assert.deepEqual(errors, []);
        await capture("05-confirmation");
        console.log("Correction browser flow passed");
    } finally {
        await browser.close();
    }
})().catch(error => {
    console.error(error);
    process.exit(1);
});