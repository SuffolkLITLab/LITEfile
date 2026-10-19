(function() {
    function mergeFiles(existingFiles, incomingFiles) {
        return [...existingFiles, ...incomingFiles];
    }

    if (typeof module !== "undefined" && module.exports) {
        module.exports = {
            mergeFiles
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
    let nextSelectionId = 0;
    let uploading = false;
    const hasLead = form.dataset.hasLead === "true";
    let analysisReady = hasLead && form.dataset.extractionPending !== "true";
    // After the server's wait limit the filer may go on without the analysis.
    let waitOver = hasLead && form.dataset.waitSeconds === "0";
    // The analysis status the panel shows whenever no upload is in progress.
    let analysisTitle = stateTitle.textContent;
    let analysisDetail = stateDetail.textContent;

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
            showAnalysisStatus(analysisTitle, "This is taking longer than usual. You can continue and enter the case details yourself.");
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
            const response = await fetch(aiOptOut.dataset.preferenceUrl || window.location.href, {
                method: "POST",
                body
            });
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.error || "Could not save that choice.");
            // The document on file was already read the other way, so the page
            // is showing answers that no longer apply. Reload into the fresh
            // analysis instead of leaving them there.
            if (result.reanalyzing) window.location.reload();
        } catch (error) {
            errorBox.textContent = error.message;
            errorBox.hidden = false;
        }
    }

    if (aiOptOut) {
        aiOptOut.addEventListener("change", () => {
            const optedOut = aiOptOut.checked;
            if (aiNoteOn) aiNoteOn.hidden = optedOut;
            if (aiNoteOff) aiNoteOff.hidden = !optedOut;
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
        uploadButton.disabled = selectedFiles.size === 0;
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

    function addFiles(files) {
        // Each selection is a separate filing document, even if it refers to
        // the same PDF. Removal uses its selection ID, never its filename.
        const merged = mergeFiles(selectedFiles.values(), files);
        selectedFiles.clear();
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

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (uploading) return;
        uploading = true;
        updateContinue();
        errorBox.hidden = true;
        state.hidden = false;
        uploadButton.disabled = true;
        stateTitle.textContent = "Uploading your files…";
        stateDetail.textContent = "Keep this page open while we make your PDFs.";

        try {
            const response = await fetch(window.location.href, {
                method: "POST",
                body: new FormData(form),
                headers: {
                    "X-CSRFToken": apiUtils.getCSRFToken()
                },
            });
            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.error || "Upload failed.");
            stateTitle.textContent = result.extraction_pending ? "Your documents are uploaded" : "Your documents are ready";
            let pendingDetail = "Please wait while we read your first file. You can continue when it is ready.";
            if (aiIsOff()) pendingDetail = "AI is off. We are looking for form and case numbers.";
            stateDetail.textContent = result.extraction_pending ?
                pendingDetail :
                "Review what we found before you continue.";
            const replacement = form.querySelector('[name="replace_document_id"]');
            if (replacement) replacement.value = "";
            window.setTimeout(() => window.location.reload(), 300);
        } catch (error) {
            uploading = false;
            updateContinue();
            // Go back to the first file's analysis status, including any
            // result that arrived while this upload was running.
            state.hidden = form.dataset.extractionPending !== "true";
            stateTitle.textContent = analysisTitle;
            stateDetail.textContent = analysisDetail;
            errorBox.textContent = error.message;
            errorBox.hidden = false;
            uploadButton.disabled = false;
        }
    });

    document.querySelectorAll(".remove-document").forEach((button) => {
        button.addEventListener("click", async () => {
            if (!window.confirm("Remove this document from your filing?")) return;
            const body = new FormData();
            body.append("action", "remove");
            body.append("document_id", button.dataset.documentId);
            body.append("csrfmiddlewaretoken", apiUtils.getCSRFToken());
            const response = await fetch(window.location.href, {
                method: "POST",
                body
            });
            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }
            const result = await response.json();
            if (response.ok && result.success) window.location.reload();
            else window.alert(result.error || "Could not remove the document.");
        });
    });

    async function pollExtraction() {
        if (!form.dataset.extractionStatusUrl || state.hidden) return;
        try {
            const response = await fetch(form.dataset.extractionStatusUrl, {
                headers: {
                    "X-CSRFToken": apiUtils.getCSRFToken()
                },
            });
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.error || "Could not check document analysis.");
            if (!result.ready) {
                if (result.wait_seconds === 0) endWait();
                window.setTimeout(pollExtraction, 2500);
                return;
            }

            state.querySelector(".spinner-border")?.remove();
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
            const analyzingPill = document.querySelector(".status-pill--analyzing");
            if (analyzingPill) {
                analyzingPill.classList.replace("status-pill--analyzing", "status-pill--ready");
                analyzingPill.innerHTML = '<i class="fa-solid fa-check" aria-hidden="true"></i> Ready';
            }
        } catch (error) {
            showAnalysisStatus(analysisTitle, error.message);
            if (waitTimerDone) endWait();
            window.setTimeout(pollExtraction, 5000);
        }
    }

    pollExtraction();
})();