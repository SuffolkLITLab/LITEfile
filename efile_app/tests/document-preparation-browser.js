/* Browser validation invoked by the opt-in Django integration test. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
    chromium
} = require("@playwright/test");
const AxeBuilder = require("@axe-core/playwright").default;

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
    const page = await context.newPage();
    // Court choices and payment accounts are synthetic; no court API calls.
    await page.route(/\/api\/(?:dropdowns\/|payment-(?:accounts|account-types)\/)/, (route) =>
        route.fulfill({
            json: {
                success: true,
                data: []
            }
        })
    );
    await page.route("**/case-filing-types/**", route => route.fulfill({
        json: {
            success: true,
            data: []
        }
    }));
    const errors = [];
    page.on("console", (message) => {
        if (["warning", "error"].includes(message.type())) console.log(message.text());
    });
    page.on("pageerror", (error) => errors.push(error.message));
    fs.mkdirSync(config.evidence, {
        recursive: true
    });
    const screenshot = (name) =>
        page.screenshot({
            path: path.join(config.evidence, name),
            fullPage: true
        });
    try {
        await page.goto(config.baseUrl + config.uploadUrl);
        await page.locator("#documents-input").setInputFiles(config.files);
        await screenshot("01-upload-selection.png");
        await page.locator("#upload-button").click();
        await page.waitForResponse(
            (response) => response.url().includes("upload-documents") && response.request().method() === "POST"
        );
        await page.locator("#continue-to-analysis:not(.disabled)").waitFor();
        await page.waitForFunction(() => document.querySelectorAll(".document-row").length === 2);
        await page.locator("#continue-to-analysis").click();
        await page.waitForURL(/extraction-review/);
        const previews = page.locator("[data-pdf-preview]");
        assert.equal(await previews.count(), 2);
        await previews.first().locator("summary").click();
        await previews.first().locator("canvas").first().waitFor();
        await page.waitForFunction(() => document.querySelector("[data-pdf-preview]").dataset.rendered === "true");
        assert.match(await previews.first().innerText(), /of 2/);
        await screenshot("02-multiline-preview.png");
        await previews.first().locator("[data-pdf-next]").click();
        assert.equal(await previews.first().locator("[data-pdf-page]").inputValue(), "2");
        await previews.first().locator("[data-pdf-zoom-in]").click();
        await screenshot("03-second-page.png");
        await previews.nth(1).locator("summary").click();
        await page.waitForFunction(
            () => document.querySelectorAll("[data-pdf-preview]")[1].dataset.rendered === "true"
        );
        await screenshot("04-word-preview.png");
        const accessibility = await new AxeBuilder({
                page
            })
            .include(".workflow-card")
            .analyze();
        const serious = accessibility.violations.filter((item) => ["serious", "critical"].includes(item.impact));
        fs.writeFileSync(
            path.join(config.evidence, "accessibility.json"),
            JSON.stringify({
                    violations: accessibility.violations,
                    passes: accessibility.passes.map((item) => item.id)
                },
                null,
                2
            )
        );
        assert.equal(
            accessibility.violations.length,
            0,
            JSON.stringify(
                serious.map((item) => ({
                    id: item.id,
                    nodes: item.nodes.map((node) => node.target)
                }))
            )
        );
        assert.equal(await page.locator('input[type="checkbox"]').count(), 0);
        await page
            .getByRole("button", {
                name: "Continue",
                exact: true
            })
            .click();
        await page.waitForURL(/case-(lookup|confirmation)/);
        await page.goto(config.baseUrl + config.organizeUrl);
        assert.match(page.url(), /organize-documents/);
        assert.equal(await page.locator("h1").innerText(), "Organize your documents");
        await page.locator("[data-pdf-preview]").first().locator("summary").click();
        await page.waitForFunction(() => document.querySelector("[data-pdf-preview]").dataset.rendered === "true");
        await screenshot("05-organize-preview.png");
        await page.goto(config.baseUrl + config.paymentUrl);
        assert.match(page.url(), /\/payment\//);
        assert.equal(await page.locator("h1").innerText(), "Choose how to pay court fees");
        await page.locator("[data-pdf-preview]").first().locator("summary").click();
        await page.waitForFunction(() => document.querySelector("[data-pdf-preview]").dataset.rendered === "true");
        await screenshot("14-fees-preview.png");
        await page.setViewportSize({
            width: 390,
            height: 844
        });
        await page.goto(config.baseUrl + config.previewUrl);
        await page.locator("[data-pdf-preview]").first().locator("summary").click();
        await page.waitForFunction(() => document.querySelector("[data-pdf-preview]").dataset.rendered === "true");
        await screenshot("06-mobile-preview.png");
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
        await page.setViewportSize({
            width: 1280,
            height: 1000
        });
        await page.goto(config.baseUrl + config.uploadUrl);
        await page.locator("#documents-input").setInputFiles({
            name: "invalid.pdf",
            mimeType: "application/pdf",
            buffer: Buffer.from("not a PDF")
        });
        await page.locator("#upload-button").click();
        if (config.asyncPreparation) {
            await page.locator('#preparation-state[data-status="failed"]').waitFor();
            assert.match(await page.locator("#preparation-detail").innerText(), /could not be read/);
            assert.equal(await page.locator("#continue-to-analysis").getAttribute("aria-disabled"), "true");
            await screenshot("07-recoverable-error.png");
            await page.locator('[data-action="cancel_preparation"]').click();
            await page.locator("#continue-to-analysis:not(.disabled)").waitFor();
        } else {
            await page.locator("#upload-error:not([hidden])").waitFor();
            await screenshot("07-recoverable-error.png");
            assert.match(await page.locator("#upload-error").innerText(), /could not be read/);
        }
        // A storage failure must leave a usable download/retry explanation.
        await page.route("**/documents/*/content/**", (route) =>
            route.fulfill({
                status: 503,
                body: "Unavailable"
            })
        );
        await page.goto(config.baseUrl + config.previewUrl);
        await page.locator("[data-pdf-preview]").first().locator("summary").click();
        await page
            .getByText("The PDF did not load.", {
                exact: false
            })
            .waitFor();
        await screenshot("08-preview-failure.png");
        await page.unroute("**/documents/*/content/**");
        await page.locator("[data-pdf-preview]").first().locator("summary").click();
        await page.locator("[data-pdf-preview]").first().locator("summary").click();
        await page.waitForFunction(() => document.querySelector("[data-pdf-preview]").dataset.rendered === "true");
        assert.deepEqual(errors, []);
        console.log(
            "Browser validation passed: upload, real PDF.js pages, navigation, zoom, Word preview, Continue without checkboxes, organize, fees, mobile, errors/retry, and Axe."
        );
    } catch (error) {
        await screenshot("browser-failure.png");
        throw error;
    } finally {
        await browser.close();
    }
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});