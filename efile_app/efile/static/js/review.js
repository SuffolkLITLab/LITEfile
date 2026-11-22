const reviewJSON = (id) => JSON.parse(document.getElementById(id).textContent);

const Messages = {
    hide() {
        document.getElementById("errorMessage").hidden = true;
        document.getElementById("successMessage").hidden = true;
    },
    showError(message, actions = []) {
        document.getElementById("errorText").textContent = message;
        window.FilingErrorActions?.render("filing-error-actions", actions);
        const box = document.getElementById("errorMessage");
        box.hidden = false;
        box.scrollIntoView({
            behavior: "smooth",
            block: "center"
        });
    },
    showSuccess(message) {
        document.getElementById("successText").textContent = message;
        document.getElementById("successMessage").hidden = false;
    }
};

const USABLE_FEE_STATES = ["current", "waived"];

const FilingHandler = {
    feeQuoteState: "missing",

    // A disabled checkbox means the court requirements could not be loaded.
    filingConfirmed() {
        const checkbox = document.getElementById("confirm-filing");
        return checkbox.checked && !checkbox.disabled;
    },

    // Set once a submit may have reached the court; Submit then stays off.
    submissionOutcomeUnknown: false,
    // True from the click until the outcome is known, so nothing else on the
    // page (the confirmation box, a fee refresh) can re-enable Submit meanwhile.
    submissionInFlight: false,
    outcomePollDelayMs: 5000,
    // The server waits up to 310 seconds for the court, so keep asking while
    // the claim is held for about as long (66 x 5 s), matching the submit timeout.
    outcomePollAttempts: 66,

    setSubmissionState(submitting) {
        this.submissionInFlight = submitting;
        document.getElementById("loadingSpinner").style.display = submitting ? "block" : "none";
        this.refreshSubmitButton();
    },

    refreshSubmitButton() {
        // Never against a total the filer has not seen for this filing: the
        // button waits until the quote on the page is the current one.
        document.getElementById("submitButton").disabled = this.submissionInFlight ||
            this.submissionOutcomeUnknown ||
            !this.filingConfirmed() ||
            !USABLE_FEE_STATES.includes(this.feeQuoteState);
    },

    // Ask this app's own records what became of the draft. A lost response
    // (proxy error, a machine stopped mid-request, a timeout) can follow a
    // filing that did reach the court, so the browser's error alone never
    // proves nothing was sent. "pending" means the claim is still held: the
    // request may be finishing, so ask again for a while before giving up.
    //
    // With `unanswered`, the submit got no reply from this app at all, so the
    // first request may still be checking the payment account before it claims
    // the draft. A draft must then stay unclaimed for a while (draftSettleChecks
    // answers) before the page says nothing was sent.
    draftSettleChecks: 6,

    async checkSubmissionOutcome({
        unanswered = false
    } = {}) {
        let failures = 0;
        let unclaimed = 0;
        for (let attempt = 0; attempt < this.outcomePollAttempts; attempt++) {
            if (attempt > 0) await new Promise((resolve) => setTimeout(resolve, this.outcomePollDelayMs));
            const status = await this.requestSubmissionStatus();
            if (status.refused) break;
            if (status.failed) {
                failures += 1;
                if (failures >= 3) break;
                continue;
            }
            const outcome = status.result?.outcome;
            if (outcome === "not_sent" && unanswered && ++unclaimed < this.draftSettleChecks) continue;
            if (outcome !== "pending") return status.result || {
                outcome: "unknown"
            };
        }
        return {
            outcome: "unknown"
        };
    },

    async requestSubmissionStatus() {
        try {
            return {
                result: await apiUtils.makeRequest("/api/submission-status/", {
                    method: "GET",
                    timeout: 15000
                })
            };
        } catch (error) {
            // A refusal (signed out, not this filer's draft) will not change on
            // retry; only a network or server failure is worth repeating.
            return error?.status >= 400 && error?.status < 500 ? {
                refused: true
            } : {
                failed: true
            };
        }
    },

    showUnknownOutcome() {
        this.submissionOutcomeUnknown = true;
        const jurisdiction = apiUtils.getCurrentJurisdiction();
        Messages.showError(
            gettext("We are not sure whether your filing reached the court. Do not submit it again. Check My filings in a few minutes. If it is not listed there, contact us for help."),
            jurisdiction ? [{
                url: `/jurisdiction/${jurisdiction}/filing_statuses/`,
                label: gettext("Open My filings")
            }] : []
        );
        this.setSubmissionState(false);
    },

    showSubmitError(error) {
        // A refused second submit no longer applies once the draft is released,
        // so give the general advice; other refusals keep their own reason.
        const message = error?.errorCode === "submission_claimed" ? null : error?.serverMessage;
        Messages.showError(message || gettext("We could not submit the filing. Please try again."), error?.errorActions);
        this.setSubmissionState(false);
    },

    async handleSubmissionFailure(error) {
        const spinnerText = document.getElementById("loadingSpinnerText");
        const sendingText = spinnerText?.textContent;
        if (spinnerText) spinnerText.textContent = gettext("Checking whether your filing reached the court. This can take a few minutes. Do not close this page.");
        // Our server answers every outcome it reaches with JSON; a bare proxy
        // error, a timeout or a dropped connection means the request may still
        // be running.
        const unanswered = !error?.serverMessage && !(error?.status >= 400 && error?.status < 500);
        const status = await this.checkSubmissionOutcome({
            unanswered
        });
        if (spinnerText) spinnerText.textContent = sendingText;
        if (status.outcome === "submitted" && status.redirect_url) {
            window.location.href = status.redirect_url;
            return;
        }
        if (status.outcome === "not_sent") {
            if (error?.status === 412 && !["payment_account_unavailable", "correction_not_ready"].includes(error?.errorCode)) {
                // Something changed in another tab since this page loaded.
                await this.refreshFeeQuote();
            }
            this.showSubmitError(error);
            return;
        }
        this.showUnknownOutcome();
    },

    setFeesState() {},

    buildCurrentFilingData() {
        const caseData = reviewJSON("case-data");
        return this.buildEFilingData(
            this.userDataFromCaseData(caseData),
            caseData,
            reviewJSON("upload-data"),
            reviewJSON("payment-account-id")
        );
    },

    initFreeFilingPopover() {
        const trigger = document.getElementById("free-filing-explainer");
        const content = document.getElementById("free-filing-explainer-content");
        if (!trigger || !content || !window.bootstrap) return;
        this.freeFilingPopover = new window.bootstrap.Popover(trigger, {
            title: trigger.textContent.trim(),
            content: content.innerHTML,
            html: true,
            trigger: "click",
            placement: "top",
            customClass: "info-explainer-popover"
        });
        document.addEventListener("click", (event) => {
            const inside = trigger.contains(event.target) || event.target.closest(".info-explainer-popover");
            if (!inside) this.freeFilingPopover.hide();
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") this.freeFilingPopover.hide();
        });
    },

    setFreeFilingHelp(visible) {
        const help = document.getElementById("free-filing-help");
        if (help) help.hidden = !visible;
        if (!visible) this.freeFilingPopover?.hide();
    },

    showFeeQuote(quote) {
        this.setFreeFilingHelp(quote.state === "current" && quote.is_zero === true);
        const amount = document.getElementById("fee-quote-amount");
        const list = document.getElementById("fee-quote-breakdown");
        amount.textContent = Number(quote.total).toLocaleString("en-US", {
            style: "currency",
            currency: "USD"
        });
        const fees = quote.is_zero ? [] : (quote.breakdown || []);
        list.replaceChildren(...fees.map((fee) => this.feeReceiptRow(fee.label, fee.amount)));
        document.getElementById("submit-button-label").textContent =
            quote.state === "current" && Number(quote.total) > 0 ? gettext("Submit and pay") : gettext("Submit filing");
        document.getElementById("fee-quote").dataset.state = quote.state;
        document.getElementById("fee-quote-total").hidden = false;
        document.getElementById("fee-quote-pending").hidden = true;
        document.getElementById("fee-quote-error").hidden = true;
        window.FilingErrorActions?.render("fee-error-actions", []);
    },

    showFeeError(message, actions = []) {
        this.setFreeFilingHelp(false);
        document.getElementById("submit-button-label").textContent = gettext("Submit filing");
        document.getElementById("fee-quote-breakdown").replaceChildren();
        document.getElementById("fee-quote-pending").hidden = true;
        document.getElementById("fee-quote-total").hidden = true;
        document.getElementById("fee-quote-error-text").textContent = message;
        window.FilingErrorActions?.render("fee-error-actions", actions);
        document.getElementById("fee-quote-error").hidden = false;
    },

    // The quote saved on Payment no longer matches this filing (or there is
    // none): ask the court again from here rather than sending the filer back
    // to Payment to find out what changed.
    async refreshFeeQuote() {
        this.feeQuoteState = "stale";
        this.setFreeFilingHelp(false);
        this.setSubmissionState(false);
        try {
            const result = await apiUtils.post("/api/payment-fees/", {
                efile_data: this.buildCurrentFilingData(),
                confirm_submission: true,
                payment_account_id: reviewJSON("payment-account-id"),
                fee_inputs_token: reviewJSON("fee-inputs-token")
            }, {}, {
                timeout: ApiUtils.FEE_TIMEOUT_MS
            });
            const quote = result?.quote;
            if (result?.success && result.quote_superseded) {
                // This page shows an older version of the filing than the
                // one saved now, so its fees would be for the wrong filing.
                throw new Error(gettext("This filing changed while we were calculating fees, perhaps in another window. Reload this page to see the current filing."));
            }
            if (!result?.success || !result.quote_recorded || !quote || !USABLE_FEE_STATES.includes(quote.state)) {
                throw new Error(gettext("The court did not return a fee total for this filing."));
            }
            this.feeQuoteState = quote.state;
            this.showFeeQuote(quote);
        } catch (error) {
            this.feeQuoteState = "missing";
            this.showFeeError(`${error?.serverMessage || error?.message || gettext("We could not calculate the court's fees.")} ${gettext("You cannot submit until the fees are known.")}`, error?.errorActions);
        }
        this.setSubmissionState(false);
    },

    async submitFiling() {
        if (this.submissionInFlight || this.submissionOutcomeUnknown) return;
        if (!this.filingConfirmed()) {
            Messages.showError(gettext("Confirm that you reviewed the filing before you submit."));
            return;
        }
        Messages.hide();
        this.setSubmissionState(true);
        let body;
        try {
            body = {
                efile_data: this.buildCurrentFilingData(),
                disclaimer_token: reviewJSON("disclaimer-token"),
                confirm_submission: true,
                payment_account_id: reviewJSON("payment-account-id")
            };
        } catch (error) {
            // Nothing has been sent yet, so there is no outcome to check.
            this.showSubmitError(error);
            return;
        }
        try {
            const result = await apiUtils.post("/api/submit-final-filing/", body, {}, {
                timeout: ApiUtils.SUBMISSION_TIMEOUT_MS
            });
            if (result?.success) {
                this.handleSubmissionResult(result);
            } else {
                await this.handleSubmissionFailure({
                    serverMessage: result?.error,
                    errorActions: result?.error_actions
                });
            }
        } catch (error) {
            await this.handleSubmissionFailure(error);
        }
    }
};

Object.assign(FilingHandler, FilingPayload);
document.addEventListener("DOMContentLoaded", () => {
    FilingHandler.initFreeFilingPopover();
    const confirmation = document.getElementById("confirm-filing");
    FilingHandler.feeQuoteState = reviewJSON("fee-quote-state");
    if (!document.getElementById("correction-issues-heading") && !USABLE_FEE_STATES.includes(FilingHandler.feeQuoteState)) FilingHandler.refreshFeeQuote();
    confirmation.addEventListener("change", () => FilingHandler.refreshSubmitButton());
    // An earlier submit of this draft may have reached the court (#445).
    if (reviewJSON("submission-outcome-unknown")) FilingHandler.showUnknownOutcome();
    document.getElementById("submitButton").addEventListener("click", () => FilingHandler.submitFiling());
});