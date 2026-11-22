(function() {
    function mergeFiles(existingFiles, incomingFiles) {
        return [...existingFiles, ...incomingFiles];
    }

    function extractionPollDelay(attempt, waitSeconds, random = Math.random) {
        // Start promptly, then spread long waits across a bounded interval.
        // Never schedule past the server's manual-entry deadline.
        const delay = attempt === 0 ? 2500 : Math.min(10000, 2500 * 1.5 ** Math.min(attempt, 4)) * (0.8 + 0.2 * random());
        return waitSeconds > 0 ? Math.min(delay, waitSeconds * 1000) : delay;
    }

    if (typeof module !== "undefined" && module.exports) {
        module.exports = {
            mergeFiles,
            extractionPollDelay
        };
    }
    if (typeof document === "undefined") return;

    const form = document.getElementById("document-upload-form");
    if (!form) return;

    const input = document.getElementById("documents-input");
    const dropZone = document.getElementById("document-drop-zone");
    const uploadButton = document.getElementById("upload-button");
    const state = document.getElementById("upload-state");
    const stateTitle = document.getElementById("upload-state-title");
    const stateDetail = document.getElementById("upload-state-detail");
    const errorBox = document.getElementById("upload-error");
    const pendingSection = document.getElementById("pending-files");
    const pendingList = document.getElementById("pending-file-list");
    const pendingCount = document.getElementById("pending-file-count");
    const continueButton = document.getElementById("continue-to-analysis");
    const aiOptOut = document.getElementById("ai-opt-out");
    const aiNoteOn = document.getElementById("ai-note-on");
    const aiNoteOff = document.getElementById("ai-note-off");
    const aiExplainer = document.getElementById("ai-explainer");
    const aiExplainerContent = document.getElementById("ai-explainer-content");
    const aiRemember = document.getElementById("ai-remember");
    const aiRememberChoice = document.getElementById("ai-remember-choice");
    const selectedFiles = new Map();
    const mainDialog = document.getElementById("main-document-dialog");
    const mainChoices = document.getElementById("main-document-choices");
    const mainChoiceForm = document.getElementById("main-document-choice-form");
    const processingDialog = document.getElementById("processing-dialog");
    const processingClose = document.getElementById("processing-close");
    const processingContinue = document.getElementById("processing-continue");
    const PHASES = ["uploading", "preparing", "reading", "ready"];
    const STEP_STATES = {
        done: "Done",
        active: "In progress",
        failed: "Needs attention",
        waiting: "Not started"
    };
    // An upload ends in a page refresh, so remember for this tab that one was
    // started; the window can then say "ready" for an upload that never needs
    // reading (a supporting document) instead of silently disappearing.
    const progressKey = `litefile-upload-progress:${new URL(window.location.href).searchParams.get("draft") || "current"}`;

    function rememberUpload(reading) {
        try {
            window.sessionStorage?.setItem(progressKey, JSON.stringify({
                reading
            }));
        } catch {
            // Storage can be unavailable; the window then only follows this page.
        }
    }

    function recalledUpload() {
        try {
            return JSON.parse(window.sessionStorage?.getItem(progressKey) || "null");
        } catch {
            return null;
        }
    }

    function forgetUpload() {
        try {
            window.sessionStorage?.removeItem(progressKey);
        } catch {
            // Nothing to forget.
        }
    }

    function processingPhase() {
        return processingDialog?.dataset.phase || "";
    }

    // The window stays up while anything is still running. Only a finished,
    // failed or overdue job lets the filer dismiss it.
    function processingClosable() {
        const phase = processingPhase();
        return phase === "ready" || phase === "failed" || (phase === "reading" && waitOver);
    }

    function refreshProcessingActions() {
        if (!processingDialog) return;
        if (processingClose) processingClose.hidden = !processingClosable();
        if (processingContinue) {
            const show = processingPhase() !== "failed" && canContinue();
            processingContinue.hidden = !show;
            if (show) processingContinue.setAttribute("href", continueButton.dataset.continueUrl);
        }
        const actions = document.getElementById("processing-actions");
        if (actions) actions.hidden = Boolean(processingClose?.hidden && processingContinue?.hidden);
    }

    function setProcessingPhase(phase, {
        reading = true
    } = {}) {
        if (!processingDialog) return;
        processingDialog.dataset.phase = phase;
        const current = phase === "failed" ? PHASES.indexOf("preparing") : PHASES.indexOf(phase);
        (processingDialog.querySelectorAll?.(".processing-steps li") || []).forEach((step) => {
            const index = PHASES.indexOf(step.dataset.step);
            let stepState = "waiting";
            if (index < current) stepState = "done";
            else if (index === current) stepState = phase === "failed" ? "failed" : "active";
            step.dataset.state = stepState;
            if (step.dataset.step === "reading") step.hidden = !reading && phase !== "reading";
            const label = step.querySelector(".processing-steps__state");
            if (label) label.textContent = STEP_STATES[stepState];
            if (stepState === "active") step.setAttribute("aria-current", "step");
            else step.removeAttribute("aria-current");
        });
        // Ready is the end: nothing to reopen after another refresh.
        if (phase === "ready" && !uploading) forgetUpload();
        refreshProcessingActions();
        if (!processingDialog.open) processingDialog.showModal?.();
    }

    function closeProcessing() {
        if (processingDialog?.open) processingDialog.close();
    }

    processingDialog?.addEventListener("cancel", (event) => {
        if (!processingClosable()) event.preventDefault();
    });
    processingClose?.addEventListener("click", closeProcessing);
    let mainFileKey = null;
    let nextSelectionId = 0;
    let uploading = false;
    let uploadBlocked = false;
    const hasLead = form.dataset.hasLead === "true";
    let analysisReady = hasLead && form.dataset.extractionPending !== "true";
    // After the server's wait limit the filer may go on without the analysis.
    let waitOver = hasLead && form.dataset.waitSeconds === "0";
    // The analysis status the panel shows whenever no upload is in progress.
    let analysisTitle = stateTitle.textContent;
    let analysisDetail = stateDetail.textContent;
    const initialAnalysisDetail = analysisDetail;
    const manualEntryMessage = "This is taking longer than usual. You can continue and enter the case details yourself.";
    const uploadUnknown = "We could not confirm your upload. Reopen this filing from My drafts and check your files before uploading again.";

    function showError(message) {
        errorBox.textContent = message;
        errorBox.hidden = false;
    }

    function updateUploadButton() {
        uploadButton.disabled = uploading || uploadBlocked || selectedFiles.size === 0;
    }

    function canContinue() {
        return !uploading && (analysisReady || waitOver);
    }

    function showAnalysisStatus(title, detail) {
        analysisTitle = title;
        analysisDetail = detail;
        if (uploading) return;
        stateTitle.textContent = title;
        stateDetail.textContent = detail;
    }

    function endWait() {
        if (waitOver) return;
        waitOver = true;
        updateContinue();
        if (!analysisReady) {
            showAnalysisStatus(analysisTitle, manualEntryMessage);
        }
    }

    function updateContinue() {
        const disabled = !canContinue();
        continueButton.classList.toggle("disabled", disabled);
        if (disabled) {
            continueButton.setAttribute("aria-disabled", "true");
            continueButton.setAttribute("tabindex", "-1");
            continueButton.removeAttribute("href");
        } else {
            continueButton.removeAttribute("aria-disabled");
            continueButton.removeAttribute("tabindex");
            continueButton.setAttribute("href", continueButton.dataset.continueUrl);
        }
        refreshProcessingActions();
    }

    continueButton.addEventListener("click", (event) => {
        if (!canContinue()) event.preventDefault();
    });
    updateContinue();
    // The server decides when the wait is over; its status checks open
    // Continue. This timer only matters if those checks keep failing, so a
    // broken connection cannot hold the filer here.
    let waitTimerDone = false;
    if (hasLead && !waitOver) {
        window.setTimeout(() => {
            waitTimerDone = true;
        }, Number(form.dataset.waitSeconds) * 1000);
    }
    // Merely opening preferences never changes the account default. Include
    // it only after the filer changes an AI setting during this page load.
    let aiChoiceTouched = false;

    function aiIsOff() {
        return Boolean(aiOptOut && aiOptOut.checked);
    }

    if (aiExplainer && aiExplainerContent && window.bootstrap) {
        const popover = new window.bootstrap.Popover(aiExplainer, {
            title: aiExplainer.textContent.trim(),
            content: aiExplainerContent.innerHTML,
            html: true,
            trigger: "click",
            placement: "top",
            customClass: "ai-explainer-popover"
        });
        // Bootstrap's click trigger only closes on the trigger itself, which
        // leaves the panel stranded over the page once attention moves on.
        document.addEventListener("click", (event) => {
            const inside = aiExplainer.contains(event.target) || event.target.closest(".ai-explainer-popover");
            if (!inside) popover.hide();
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") popover.hide();
        });
    }


    async function saveAiPreference() {
        const optedOut = aiOptOut.checked;
        const body = new FormData();
        body.append("action", "ai_preference");
        body.append("ai_opt_out", optedOut ? "yes" : "");
        if (aiChoiceTouched && aiRememberChoice) {
            body.append("remember_ai_choice", aiRememberChoice.checked ? "yes" : "no");
        }
        body.append("csrfmiddlewaretoken", apiUtils.getCSRFToken());
        try {
            const result = await documentRequests.json(
                aiOptOut.dataset.preferenceUrl || window.location.href,
                "We could not save your AI choice. Try again.", {
                    method: "POST",
                    body
                }
            );
            // The document on file was already read the other way, so the page
            // is showing answers that no longer apply. Reload into the fresh
            // analysis instead of leaving them there.
            if (result.reanalyzing) window.location.reload();
        } catch (error) {
            showError(error.message);
        }
    }

    if (aiOptOut) {
        aiOptOut.addEventListener("change", () => {
            const optedOut = aiOptOut.checked;
            if (aiNoteOn) aiNoteOn.hidden = optedOut;
            if (aiNoteOff) aiNoteOff.hidden = !optedOut;
            document.querySelectorAll(".processing-steps__reading-ai").forEach((label) => {
                label.hidden = optedOut;
            });
            document.querySelectorAll(".processing-steps__reading-rules").forEach((label) => {
                label.hidden = !optedOut;
            });
            aiChoiceTouched = true;
            if (aiRemember) aiRemember.open = true;
            saveAiPreference();
        });
    }

    // Closing "Replace a document" goes back to adding: a Replace choice the
    // filer can no longer see must not overwrite one of their documents.
    const replaceDocument = document.getElementById("replace-document");
    const replaceDisclosure = document.getElementById("replace-document-disclosure");
    if (replaceDocument && replaceDisclosure) replaceDisclosure.addEventListener("toggle", () => {
        if (!replaceDisclosure.open) replaceDocument.value = "";
    });

    if (aiRememberChoice) aiRememberChoice.addEventListener("change", () => {
        aiChoiceTouched = true;
        saveAiPreference();
    });

    function syncFiles() {
        const transfer = new DataTransfer();
        selectedFiles.forEach((file) => transfer.items.add(file));
        input.files = transfer.files;
        updateUploadButton();
        pendingSection.hidden = selectedFiles.size === 0;
        pendingCount.textContent = selectedFiles.size;
        pendingList.replaceChildren();
        selectedFiles.forEach((file, key) => {
            const row = document.createElement("div");
            row.className = "pending-file-row";
            const name = document.createElement("span");
            name.textContent = file.name;
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "btn btn-link text-danger pending-file-remove";
            remove.dataset.fileKey = key;
            remove.textContent = "Remove";
            remove.setAttribute("aria-label", `Remove ${file.name}`);
            row.append(name, remove);
            pendingList.append(row);
        });
        const fileCountLabel = selectedFiles.size === 1 ? "file" : "files";
        dropZone.querySelector("strong").textContent = selectedFiles.size ?
            `${selectedFiles.size} ${fileCountLabel} selected` :
            "Choose PDFs or Word documents, or drag them here";
    }

    function askForMainDocument() {
        mainChoices.replaceChildren();
        let number = 0;
        selectedFiles.forEach((file, key) => {
            const label = document.createElement("label");
            const radio = document.createElement("input");
            radio.type = "radio";
            radio.name = "main_file";
            radio.value = key;
            radio.required = true;
            const name = document.createElement("span");
            name.textContent = `${++number}. ${file.name}`;
            label.append(radio, name);
            mainChoices.append(label);
        });
        mainDialog.showModal();
    }

    mainChoiceForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const key = mainChoiceForm.querySelector('input[name="main_file"]:checked')?.value;
        if (!selectedFiles.has(key)) return;
        const mainFile = selectedFiles.get(key);
        selectedFiles.delete(key);
        const ordered = [
            [key, mainFile], ...selectedFiles
        ];
        selectedFiles.clear();
        ordered.forEach(([fileKey, file]) => selectedFiles.set(fileKey, file));
        mainFileKey = key;
        syncFiles();
        mainDialog.close();
        form.requestSubmit();
    });
    document.getElementById("main-document-cancel").addEventListener("click", () => mainDialog.close());

    function addFiles(files) {
        if (files.length && form.dataset.workerPrewake === "true") {
            const body = new FormData();
            body.append("action", "wake_workers");
            body.append("csrfmiddlewaretoken", apiUtils.getCSRFToken());
            // The upload will independently confirm worker readiness.
            fetch(window.location.href, {
                method: "POST",
                body
            }).catch(() => {});
        }
        // Each selection is a separate filing document, even if it refers to
        // the same PDF. Removal uses its selection ID, never its filename.
        const merged = mergeFiles(selectedFiles.values(), files);
        selectedFiles.clear();
        mainFileKey = null;
        merged.forEach((file) => selectedFiles.set(String(++nextSelectionId), file));
        syncFiles();
    }

    input.addEventListener("change", () => addFiles(input.files));
    ["dragenter", "dragover"].forEach((eventName) => {
        dropZone.addEventListener(eventName, (event) => {
            event.preventDefault();
            dropZone.classList.add("drop-zone--active");
        });
    });
    ["dragleave", "drop"].forEach((eventName) => {
        dropZone.addEventListener(eventName, (event) => {
            event.preventDefault();
            dropZone.classList.remove("drop-zone--active");
        });
    });
    dropZone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
    pendingList.addEventListener("click", (event) => {
        const button = event.target.closest(".pending-file-remove");
        if (!button) return;
        selectedFiles.delete(button.dataset.fileKey);
        syncFiles();
    });

    async function checkUpload(body) {
        const statusUrl = new URL(form.dataset.extractionStatusUrl, window.location.href);
        statusUrl.searchParams.set("upload_request_token", body.get("upload_request_token"));
        stateDetail.textContent = "Checking whether your upload was received…";
        return documentRequests.status(statusUrl, uploadUnknown);
    }

    async function handleUploadError(error, body) {
        if (error.kind !== "uncertain") {
            uploadBlocked = error.kind === "reopen";
            return error.message;
        }
        // In async mode the same token can be retried safely even if the first
        // request reaches the queue just after we check for it.
        if (form.dataset.asyncPreparation === "true") {
            try {
                const result = await checkUpload(body);
                if (result.upload_recorded === true) {
                    window.location.reload();
                    return null;
                }
                if (result.upload_recorded === false) {
                    return "We could not confirm your upload. Check your selected files and try again.";
                }
            } catch (statusError) {
                if (statusError.kind === "reopen") {
                    uploadBlocked = true;
                    return statusError.message;
                }
            }
        }
        uploadBlocked = true;
        return uploadUnknown;
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (uploading || uploadBlocked || selectedFiles.size === 0) return;
        if (!hasLead && selectedFiles.size > 1 && !selectedFiles.has(mainFileKey)) {
            askForMainDocument();
            return;
        }
        uploading = true;
        updateContinue();
        errorBox.hidden = true;
        state.hidden = false;
        updateUploadButton();
        // Only the first upload's main document is read for suggestions.
        const reading = !hasLead;
        rememberUpload(reading);
        setProcessingPhase("uploading", {
            reading
        });
        stateTitle.textContent = "Uploading your files…";
        stateDetail.textContent = "Keep this page open while we make your PDFs.";

        const body = new FormData(form);
        try {
            const result = await documentRequests.json(window.location.href, uploadUnknown, {
                method: "POST",
                body,
                headers: {
                    "X-CSRFToken": apiUtils.getCSRFToken()
                },
            });
            const pending = result.preparation_pending || result.extraction_pending;
            let nextPhase = "ready";
            if (result.preparation_pending) nextPhase = "preparing";
            else if (result.extraction_pending) nextPhase = "reading";
            setProcessingPhase(nextPhase, {
                reading
            });
            stateTitle.textContent = pending ? "Processing your documents…" : "Your documents are ready";
            let pendingDetail = "Please wait while we read your document to suggest answers. You can continue when it is ready.";
            if (result.preparation_pending) pendingDetail = "Your files are uploaded. Please wait while we prepare the PDFs and check them for case information.";
            else if (aiIsOff()) pendingDetail = "AI is off. We are looking for form and case numbers.";
            stateDetail.textContent = pending ?
                pendingDetail :
                "Review what we found before you continue.";
            const replacement = form.querySelector('[name="replace_document_id"]');
            if (replacement) replacement.value = "";
            window.setTimeout(() => window.location.reload(), 300);
        } catch (error) {
            const message = await handleUploadError(error, body);
            if (message === null) return;
            uploading = false;
            forgetUpload();
            closeProcessing();
            updateContinue();
            // Go back to the first file's analysis status, including any
            // result that arrived while this upload was running.
            state.hidden = form.dataset.extractionPending !== "true";
            stateTitle.textContent = analysisTitle;
            stateDetail.textContent = analysisDetail;
            showError(message);
            updateUploadButton();
        }
    });

    document.querySelectorAll(".remove-document").forEach((button) => {
        button.addEventListener("click", async () => {
            if (!window.confirm("Remove this document from your filing?")) return;
            const body = new FormData();
            body.append("action", "remove");
            body.append("document_id", button.dataset.documentId);
            body.append("csrfmiddlewaretoken", apiUtils.getCSRFToken());
            try {
                await documentRequests.json(
                    window.location.href,
                    "We could not confirm the document was removed. Reload this page to check your documents.", {
                        method: "POST",
                        body
                    }
                );
                window.location.reload();
            } catch (error) {
                window.alert(error.message);
            }
        });
    });

    let pollAttempt = 0;
    let previousStatus = null;

    function showPendingAnalysis(result) {
        if (result.wait_seconds === 0) endWait();
        const detail = result.delay_message || (waitOver ? manualEntryMessage : initialAnalysisDetail);
        if (detail !== analysisDetail) showAnalysisStatus(analysisTitle, detail);
    }

    async function pollExtraction() {
        if (!form.dataset.extractionStatusUrl || form.dataset.extractionPending !== "true") return;
        try {
            const result = await documentRequests.status(
                form.dataset.extractionStatusUrl,
                "We could not check document analysis. We’ll try again shortly."
            );
            if (!result.ready) {
                showPendingAnalysis(result);
                if (result.status !== previousStatus) pollAttempt = 0;
                previousStatus = result.status;
                window.setTimeout(pollExtraction, extractionPollDelay(pollAttempt++, result.wait_seconds));
                return;
            }

            let readyTitle = "Document analysis is ready";
            if (result.ai_opted_out) readyTitle = "We finished checking your document";
            // Nothing is reviewed on this page: the details are on the next
            // one, so say where the checking actually happens.
            const nextPageNudge = "Review the information carefully on the next page.";
            analysisReady = true;
            showAnalysisStatus(
                result.status === "failed" ? "Your document is ready for manual review" : readyTitle,
                result.total_pages > result.pages_analyzed ?
                `We read the first ${result.pages_analyzed} of ${result.total_pages} pages. ${nextPageNudge}` :
                nextPageNudge
            );
            updateContinue();
            if (!uploading && processingPhase() === "reading") setProcessingPhase("ready");
            const analyzingPill = document.querySelector(".status-pill--analyzing");
            if (analyzingPill) {
                analyzingPill.classList.replace("status-pill--analyzing", "status-pill--ready");
                analyzingPill.innerHTML = '<i class="fa-solid fa-check" aria-hidden="true"></i> Ready';
            }
        } catch (error) {
            showAnalysisStatus(analysisTitle, error.message);
            if (error.kind !== "uncertain") return;
            if (waitTimerDone) endWait();
            window.setTimeout(pollExtraction, extractionPollDelay(pollAttempt++));
        }
    }

    window.UploadProgress = {
        forget: forgetUpload
    };
    // Reopen the window after the refresh that follows each step.
    function reopenProcessing() {
        const recalled = recalledUpload();
        const serverPhase = processingPhase();
        if (serverPhase) {
            setProcessingPhase(serverPhase, {
                reading: recalled?.reading ?? true
            });
        } else if (recalled) {
            stateTitle.textContent = "Your documents are ready";
            stateDetail.textContent = hasLead ?
                "Your files are uploaded. Review the information carefully on the next page." :
                "Your files are uploaded.";
            setProcessingPhase("ready", {
                reading: recalled.reading
            });
        }
    }
    reopenProcessing();

    pollExtraction();
})();