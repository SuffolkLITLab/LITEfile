/* Browser checks for the Fees screen, invoked by the opt-in Django integration test. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
    chromium
} = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

const WAIVER_ACCOUNT = {
    paymentAccountID: "wv-1",
    accountName: "Fee waiver",
    paymentAccountTypeCode: "WV",
    active: {
        value: true
    }
};
const CARD_ACCOUNT = {
    paymentAccountID: "cc-1",
    accountName: "Visa",
    paymentAccountTypeCode: "CC",
    cardLast4: "4242",
    active: {
        value: true
    }
};

async function main() {
    const config = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
    const browser = await chromium.launch({
        executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined
    });
    const context = await browser.newContext({
        viewport: {
            width: 1280,
            height: 1000
        }
    });
    await context.addCookies([{
        name: "sessionid",
        value: config.cookie,
        url: config.baseUrl
    }]);
    const feeRequests = [];
    // Court-side APIs are synthetic; the app's own endpoints are real.
    await context.route("**/api/payment-account-types/**", (route) => route.fulfill({
        json: {
            success: true,
            data: [{
                code: "CC",
                description: "Credit card"
            }]
        }
    }));
    await context.route("**/api/payment-accounts/**", (route) => route.fulfill({
        json: {
            success: true,
            data: [WAIVER_ACCOUNT, CARD_ACCOUNT]
        }
    }));
    await context.route("**/api/waiver-account/**", (route) => route.fulfill({
        json: {
            success: true,
            data: WAIVER_ACCOUNT
        }
    }));
    await context.route("**/api/payment-fees/**", (route) => {
        feeRequests.push(route.request().postDataJSON());
        return route.fulfill({
            json: {
                success: true,
                quote_recorded: true,
                api_response: {
                    feesCalculationAmount: {
                        value: "120.00"
                    },
                    allowanceCharge: []
                }
            }
        });
    });
    await context.route("**/api/dropdowns/**", route => {
        const endpoint = new URL(route.request().url()).pathname;
        const choices = {
            "/api/dropdowns/courts/": [{
                value: "cook:law1",
                text: "Circuit Court of Cook County"
            }],
            "/api/dropdowns/case-categories/": [{
                value: "civil",
                text: "Civil"
            }],
            "/api/dropdowns/case-types/": [{
                value: "contract",
                text: "Contract"
            }],
            "/api/dropdowns/filing-types/": [{
                value: "petition",
                text: "Petition"
            }, {
                value: "waiver",
                text: "Application to waive court fees"
            }],
        };
        if (endpoint === "/api/dropdowns/court-selector/") return route.fulfill({
            json: {
                success: true,
                data: {
                    available: false
                }
            }
        });
        if (choices[endpoint]) return route.fulfill({
            json: {
                success: true,
                data: choices[endpoint]
            }
        });
        return route.continue();
    });
    const page = await context.newPage();
    const errors = [];
    const visited = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
        if (message.type() === "error") console.log("console:", message.text());
    });
    page.on("framenavigated", (frame) => {
        if (frame === page.mainFrame()) visited.push(new URL(frame.url()).pathname);
    });
    page.on("dialog", (dialog) => dialog.accept());
    page.on("response", (response) => {
        if (response.status() >= 400) console.log("HTTP", response.status(), response.url());
    });
    fs.mkdirSync(config.evidence, {
        recursive: true
    });
    const screenshot = (name) => page.screenshot({
        path: path.join(config.evidence, name),
        fullPage: true
    });
    const payment = config.baseUrl + config.paymentUrl;
    const submit = page.locator("#submitButton");
    const checks = page.locator("[data-document-check]");

    const chooseWaiver = async () => {
        await page.locator('input[name="paymentIntent"][value="waiver"]').check();
        await page.locator("#successMessage:not([hidden])").waitFor();
    };
    const uploadWaiver = async (file) => {
        if (await page.locator("#waiver-upload-panel").isHidden()) await page.locator("#add-waiver-document").click();
        await page.locator("#upload-waiver-document:not([disabled])").waitFor();
        await page.locator("#waiver-document-type").selectOption("private");
        await page.locator("#waiver-file").setInputFiles(file);
        await page.locator("#upload-waiver-document").click();
    };
    const waitRendered = (locator) => locator.locator("[data-pdf-download]").waitFor();

    try {
        // 1. The reported flow: upload on Fees, check it there, go to Review.
        await page.goto(payment);
        await chooseWaiver();
        visited.length = 0;
        await uploadWaiver(config.waiverFile);
        await checks.first().waitFor();
        assert.equal(await checks.count(), 1);
        assert.ok(page.url().includes("/payment/"), `left Fees for ${page.url()}`);
        assert.ok(!visited.some((url) => url.includes("preview-documents")), `visited ${visited}`);
        assert.equal(await page.locator('input[name="paymentIntent"][value="waiver"]').isChecked(), true);
        assert.equal(await page.locator("#waiver-upload-required").isHidden(), true);
        await waitRendered(checks.first());
        assert.equal(await submit.isDisabled(), true, "Continue must wait for the copy to be checked");
        await screenshot("01-fees-check-waiver.png");
        const axe = await new AxeBuilder({
            page
        }).include(".workflow-card").analyze();
        assert.deepEqual(axe.violations.map((item) => item.id), []);
        await page.setViewportSize({
            width: 390,
            height: 844
        });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
        await screenshot("02-fees-check-mobile.png");
        await page.setViewportSize({
            width: 1280,
            height: 1000
        });
        await checks.first().locator("[data-document-confirm]").click();
        await checks.first().waitFor({
            state: "detached"
        });
        await page.locator("#submitButton:not([disabled])").waitFor();
        assert.match(await page.locator("#waiver-upload-confirmation").innerText(), /checked/);
        await Promise.all([page.waitForURL(/\/review\//), submit.click()]);
        assert.ok(!visited.some((url) => url.includes("preview-documents")), `visited ${visited}`);
        assert.match(await page.locator("body").innerText(), /waiver\.pdf/);
        await screenshot("03-review-after-waiver.png");

        // 2. Coming back from Review: the waiver is part of the filing already.
        await page.goto(payment);
        assert.equal(await checks.count(), 0);
        await chooseWaiver();
        assert.equal(await page.locator("#waiver-upload-required").isHidden(), true);
        await page.locator("#submitButton:not([disabled])").waitFor();

        // 3. Paying instead: the fee request includes the waiver document.
        await page.locator('input[name="paymentIntent"][value="pay"]').check();
        await page.locator("#paymentSection:not([hidden])").waitFor();
        const priced = JSON.stringify(feeRequests.at(-1));
        assert.match(priced, /waiver\.pdf/, "fee request must price the added document");
        await page.locator("#submitButton:not([disabled])").waitFor();
        console.log("Scenarios 1-3 passed");
    } catch (error) {
        await screenshot("browser-failure.png");
        throw error;
    }

    // A second filing that has no waiver yet.
    const secondPayment = config.baseUrl + config.secondPaymentUrl;
    try {
        // 4. Remove a wrong file and upload another, still on Fees.
        await page.goto(secondPayment);
        await chooseWaiver();
        await uploadWaiver(config.wrongFile);
        await checks.first().waitFor();
        await checks.first().locator("[data-document-remove]").click();
        await checks.first().waitFor({
            state: "detached"
        });
        await page.locator("#waiver-upload-required:not([hidden])").waitFor();
        assert.equal(await page.locator("#waiver-upload-confirmation").isHidden(), true);
        assert.equal(await page.locator('input[name="paymentIntent"][value="waiver"]').isChecked(), true);
        await uploadWaiver(config.waiverFile);
        await checks.first().waitFor();
        assert.equal(await checks.count(), 1);
        assert.match(await checks.first().innerText(), /waiver\.pdf/);

        // 5. Reload before checking: the check is still on Fees, not elsewhere.
        await page.reload();
        assert.ok(page.url().includes("/payment/"));
        assert.equal(await checks.count(), 1);
        await waitRendered(checks.first());
        await chooseWaiver();
        assert.equal(await submit.isDisabled(), true);

        // 6. A copy removed in another tab cannot be confirmed in this one.
        const other = await context.newPage();
        await other.goto(secondPayment);
        await other.locator("[data-document-check] [data-document-remove]").click();
        await other.locator("[data-document-check]").waitFor({
            state: "detached"
        });
        await other.close();
        // Removing a document also changes the fee inputs. The stale page is
        // rejected by that guard before the document's existence is checked.
        const staleCheck = page.waitForResponse((response) =>
            response.url().includes("/document-checks/") && response.request().method() === "POST"
        );
        await checks.first().locator("[data-document-confirm]").click();
        assert.equal((await staleCheck).status(), 409);
        await checks.first().locator("[data-document-check-status]").filter({
            hasText: /This filing changed\. Reload this page/
        }).waitFor();
        assert.equal(await submit.isDisabled(), true);
        await screenshot("04-stale-tab.png");

        // 7. A file that cannot be prepared explains itself and adds nothing.
        await page.goto(secondPayment);
        await chooseWaiver();
        await uploadWaiver(config.invalidFile);
        await page.locator("#waiver-upload-status").filter({
            hasText: /could not be read/
        }).waitFor();
        assert.equal(await checks.count(), 0);
        assert.equal(await page.locator("#upload-waiver-document").isEnabled(), true);
        await uploadWaiver(config.waiverFile);
        await checks.first().locator("[data-document-confirm]").click();
        await checks.first().waitFor({
            state: "detached"
        });
        await page.locator("#submitButton:not([disabled])").waitFor();
        await Promise.all([page.waitForURL(/\/review\//), submit.click()]);
        console.log("Scenarios 4-7 passed");
    } catch (error) {
        await screenshot("browser-failure-2.png");
        throw error;
    }
    try {
        // 8. Changing files from a Review detour returns to Review, stopping
        // at Organize only when a file needs a filing type.
        const review = /\/review\//;
        await page.goto(config.baseUrl + config.previewUrl + "&return_to=review");
        await page.getByRole("link", {
            name: "Change files"
        }).click();
        await page.waitForURL(/upload-documents\/\?.*return_to=review/);
        await page.locator("#documents-input").setInputFiles(config.wrongFile);
        await Promise.all([page.waitForEvent("load"), page.locator("#upload-button").click()]);
        await page.locator(".document-row").nth(2).waitFor();
        assert.ok(page.url().includes("return_to=review"), `upload reload lost its origin: ${page.url()}`);
        await page.locator("#continue-to-analysis").click();
        await page.waitForURL(/extraction-review\/\?.*return_to=review/);
        await Promise.all([page.waitForURL(/organize-documents\/\?.*return_to=review/), page.getByRole("button", {
            name: "Continue"
        }).click()]);
        await screenshot("05-new-file-stops-at-organize.png");
        // Taking the new file back out leaves nothing to organize.
        await page.goto(config.baseUrl + config.uploadUrl + "&return_to=review");
        await Promise.all([page.waitForEvent("load"), page.locator(".remove-document").nth(2).click()]);
        await page.locator(".document-row").nth(1).waitFor();
        assert.equal(await page.locator(".document-row").count(), 2);
        await page.getByRole("link", {
            name: "Back"
        }).click();
        await page.waitForURL(/extraction-review\/\?.*return_to=review/);
        await Promise.all([page.waitForURL(review), page.getByRole("button", {
            name: "Continue"
        }).click()]);
        assert.ok(!visited.slice(-3).some((url) => url.includes("preview-documents")), `visited ${visited}`);
        await screenshot("06-back-to-review.png");
        console.log("Scenario 8 passed");
    } catch (error) {
        await screenshot("browser-failure-3.png");
        throw error;
    }
    try {
        // 9. A missing document added on the checklist is checked there.
        const checklist = config.baseUrl + config.checklistUrl;
        const addMissing = async (file) => {
            await page.goto(checklist);
            await page.locator(".add-missing-toggle").click();
            await page.locator("#checklist-documents").setInputFiles(file);
            await Promise.all([page.waitForEvent("load"), page.locator("#checklist-upload-form button[type=submit]").click()]);
            await checks.first().waitFor();
        };
        const proceed = page.locator("#checklist-confirm-form button[type=submit]");
        visited.length = 0;
        await addMissing(config.wrongFile);
        assert.ok(page.url().endsWith("#document-checks"), page.url());
        assert.equal(await proceed.isDisabled(), true, "Continue must wait for the new file's check");
        await waitRendered(checks.first());
        await screenshot("07-checklist-check.png");
        await Promise.all([page.waitForEvent("load"), checks.first().locator("[data-document-remove]").click()]);
        assert.equal(await checks.count(), 0);
        assert.equal(await proceed.isEnabled(), true);
        await addMissing(config.waiverFile);
        await Promise.all([page.waitForEvent("load"), checks.first().locator("[data-document-confirm]").click()]);
        assert.equal(await checks.count(), 0);
        assert.match(await page.locator(".checklist-files").innerText(), /waiver\.pdf/);
        await Promise.all([page.waitForURL(/organize-documents/), proceed.click()]);
        assert.ok(!visited.some((url) => url.includes("preview-documents")), `visited ${visited}`);
        console.log("Scenario 9 passed");
    } catch (error) {
        await screenshot("browser-failure-4.png");
        throw error;
    }
    assert.deepEqual(errors, []);
    console.log("Fees browser validation passed.");
    await browser.close();
}

main().catch((error) => {
    console.error(error);
    // An open browser would keep this process alive past a failure.
    process.exit(1);
});