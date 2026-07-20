/*
 * Socket.IO contract used by this page:
 *
 * client -> server: run_sequence { run_id, code }, abort_sequence { run_id },
 *                   subscribe_runs { run_ids }
 * server -> client: sequence_snapshot { runs: [...] },
 *                   sequence_update { run_id, status_messages?, message?,
 *                   replace_index?, environment_output?, predicted_steps?,
 *                   guidance?, state?, ended? }, sequence_error { run_id, message }
 *
 * A run_id is generated in the browser so reconnects can ask the server for the
 * active sequence snapshot without creating a second sequence.
 */

const STORAGE_KEY = "impactode.web.state.v1.0.0";
const THEME_KEY = "impactode.web.theme";
const HOME_TAB_ID = "home";
const LOGS_TAB_ID = "logs";
const MAX_LOG_ENTRIES = 250;
const RUNNING_STATES = new Set(["queued", "running", "aborting"]);
const FINISHED_STATES = new Set(["completed", "failed", "aborted", "error"]);
const isPhone = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);

const elements = {
    tabList: document.getElementById("tab-list"),
    workspace: document.getElementById("workspace"),
    connectionStatus: document.getElementById("connection-status"),
    connectionLabel: document.querySelector(".connection-label"),
    debugLogsToggle: document.getElementById("debug-logs-toggle"),
    themeToggle: document.getElementById("theme-toggle"),
};

let socket = null;
let renderQueued = false;
let pendingEnvironmentScrollFor = null;
let pendingStatusScrollFor = null;
let draftSaveTimer = null;
let persistenceTimer = null;
let pendingLogScroll = false;
let lastRenderedActiveTab = null;
let editingRunTitleId = null;
let forceWorkspaceRender = false;
const tabDrag = { pointerId: null, startX: 0, startY: 0, scrollLeft: 0, axis: null, moved: false, suppressClick: false };
let state = loadState();

function getCookie(name) {
    return localStorage.getItem(name) || null;
}

function setCookie(name, value) {
    localStorage.setItem(name, value);
}

function createDefaultState() {
    return {
        activeTab: HOME_TAB_ID,
        draftCode: "",
        tabs: [],
        logsTabOpen: false,
        logs: [],
    };
}

function loadState() {
    const fallback = createDefaultState();
    const saved = getCookie(STORAGE_KEY);

    if (!saved) {
        return fallback;
    }

    try {
        const parsed = JSON.parse(saved);
        const tabs = Array.isArray(parsed.tabs) ? parsed.tabs.map(normalizeSavedRun).filter(Boolean) : [];
        const logsTabOpen = parsed.logsTabOpen === true;
        const activeTab = parsed.activeTab === HOME_TAB_ID
            || (logsTabOpen && parsed.activeTab === LOGS_TAB_ID)
            || tabs.some((tab) => tab.id === parsed.activeTab)
            ? parsed.activeTab
            : HOME_TAB_ID;

        return {
            activeTab,
            draftCode: typeof parsed.draftCode === "string" ? parsed.draftCode : "",
            tabs,
            logsTabOpen,
            logs: normalizeSavedLogs(parsed.logs),
        };
    } catch (error) {
        console.warn("Could not restore saved web tabs.", error);
        return fallback;
    }
}

function normalizeSavedRun(raw) {
    if (!raw || typeof raw !== "object" || typeof raw.id !== "string" || !raw.id) {
        return null;
    }

    const normalizedState = normalizeRunState(raw.state, raw.ended);
    return {
        id: raw.id,
        title: nonEmptyString(raw.title) || "Code run",
        code: typeof raw.code === "string" ? raw.code : "",
        statusMessages: normalizeMessages(raw.statusMessages || raw.status_messages),
        environmentOutput: typeof raw.environmentOutput === "string"
            ? raw.environmentOutput
            : typeof raw.environment_output === "string" ? raw.environment_output : "",
        guidance: typeof raw.guidance === "string" ? raw.guidance : "",
        error: typeof raw.error === "string" ? raw.error : "",
        predictedSteps: positiveInteger(raw.predictedSteps || raw.predicted_steps),
        state: normalizedState,
        startedAt: finiteNumber(raw.startedAt) || Date.now(),
        finishedAt: finiteNumber(raw.finishedAt) || (FINISHED_STATES.has(normalizedState) ? Date.now() : null),
        pendingSubmission: raw.pendingSubmission === true,
    };
}

function normalizeSavedLogs(values) {
    if (!Array.isArray(values)) {
        return [];
    }

    return values
        .map((entry) => {
            if (!entry || typeof entry !== "object" || !nonEmptyString(entry.message)) {
                return null;
            }

            return {
                id: typeof entry.id === "string" ? entry.id : createRunId(),
                timestamp: finiteNumber(entry.timestamp) || Date.now(),
                level: normalizeLogLevel(entry.level),
                message: String(entry.message),
                coalesceKey: typeof entry.coalesceKey === "string" ? entry.coalesceKey : "",
                count: positiveInteger(entry.count) || 1,
            };
        })
        .filter(Boolean)
        .slice(-MAX_LOG_ENTRIES);
}

function normalizeLogLevel(value) {
    return ["info", "warn", "error", "debug"].includes(value) ? value : "debug";
}

function persistState() {
    try {
        setCookie(STORAGE_KEY, JSON.stringify(state));
    } catch (error) {
        // The page still works if a browser storage quota is reached; the active DOM remains authoritative.
        console.warn("Could not save web tabs.", error);
    }
}

function schedulePersist() {
    if (persistenceTimer !== null) {
        return;
    }

    // localStorage is synchronous; coalescing rapid terminal chunks keeps live output responsive.
    persistenceTimer = window.setTimeout(() => {
        persistenceTimer = null;
        persistState();
    }, 180);
}

function flushScheduledPersistence() {
    if (persistenceTimer !== null) {
        window.clearTimeout(persistenceTimer);
        persistenceTimer = null;
        persistState();
    }
}

function addInterfaceLog(level, message, { coalesceKey = "" } = {}) {
    const timestamp = Date.now();
    const normalizedLevel = normalizeLogLevel(level);
    const normalizedMessage = String(message || "Interface event");
    const lastEntry = state.logs[state.logs.length - 1];

    if (coalesceKey && lastEntry && lastEntry.coalesceKey === coalesceKey && timestamp - lastEntry.timestamp < 500) {
        lastEntry.timestamp = timestamp;
        lastEntry.count += 1;
        lastEntry.message = `${normalizedMessage} (${lastEntry.count} updates)`;
    } else {
        state.logs.push({
            id: createRunId(),
            timestamp,
            level: normalizedLevel,
            message: normalizedMessage,
            coalesceKey,
            count: 1,
        });
    }

    if (state.logs.length > MAX_LOG_ENTRIES) {
        state.logs.splice(0, state.logs.length - MAX_LOG_ENTRIES);
    }

    schedulePersist();
    if (state.activeTab === LOGS_TAB_ID) {
        pendingLogScroll = true;
        queueRender();
    }
}

function nonEmptyString(value) {
    return typeof value === "string" && value.trim() ? value : "";
}

function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

function positiveInteger(value) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? Math.floor(number) : null;
}

function normalizeMessages(values) {
    if (!Array.isArray(values)) {
        return [];
    }

    return values
        .map(messageFromValue)
        .filter((message) => message.length > 0);
}

function messageFromValue(value) {
    if (typeof value === "string") {
        return value;
    }

    if (value && typeof value === "object") {
        return nonEmptyString(value.message) || nonEmptyString(value.text) || nonEmptyString(value.status);
    }

    return "";
}

function normalizeRunState(value, ended = false) {
    const raw = typeof value === "string" ? value.trim().toLowerCase() : "";
    const aliases = {
        complete: "completed",
        success: "completed",
        succeeded: "completed",
        done: "completed",
        cancelled: "aborted",
        canceled: "aborted",
        cancelled_by_owner: "aborted",
        abort: "aborted",
        failure: "failed",
        errored: "error",
        pending: "queued",
        processing: "running",
        active: "running",
    };
    const normalized = aliases[raw] || raw;

    if (["queued", "running", "aborting", "completed", "failed", "aborted", "error"].includes(normalized)) {
        return normalized;
    }

    return ended ? "completed" : "running";
}

function runStateLabel(run) {
    const labels = {
        queued: "Queued",
        running: "Running",
        aborting: "Aborting",
        completed: "Completed",
        failed: "Failed",
        aborted: "Aborted",
        error: "Error",
    };
    return labels[run.state] || "Running";
}

function isRunActive(run) {
    return RUNNING_STATES.has(run.state);
}

function isRunFinished(run) {
    return FINISHED_STATES.has(run.state);
}

function createRunId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
    }

    return `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 11)}`;
}

function makeRunTitle(index) {
    return `Run ${index}`;
}

function getRun(runId) {
    return state.tabs.find((tab) => tab.id === runId) || null;
}

function createRun({ id = createRunId(), code = "", title = "" } = {}) {
    const run = {
        id,
        title: title || makeRunTitle(state.tabs.length + 1),
        code,
        statusMessages: [],
        environmentOutput: "",
        guidance: "",
        error: "",
        predictedSteps: null,
        state: "queued",
        startedAt: Date.now(),
        finishedAt: null,
        pendingSubmission: false,
    };
    state.tabs.push(run);
    return run;
}

function queueRender({ scrollEnvironmentFor = null, scrollStatusFor = null, forceWorkspace = false } = {}) {
    if (scrollEnvironmentFor) {
        pendingEnvironmentScrollFor = scrollEnvironmentFor;
    }
    if (scrollStatusFor) {
        pendingStatusScrollFor = scrollStatusFor;
    }
    if (forceWorkspace) {
        forceWorkspaceRender = true;
    }

    if (renderQueued) {
        return;
    }

    renderQueued = true;
    window.requestAnimationFrame(render);
}

function render() {
    renderQueued = false;

    const isActiveLogsTab = state.activeTab === LOGS_TAB_ID && state.logsTabOpen;
    if (state.activeTab !== HOME_TAB_ID && !isActiveLogsTab && !getRun(state.activeTab)) {
        state.activeTab = HOME_TAB_ID;
        persistState();
    }

    const activeTabChanged = lastRenderedActiveTab !== state.activeTab;
    const keepTitleEditor = editingRunTitleId === state.activeTab && !forceWorkspaceRender && !activeTabChanged;
    renderTabs();
    if (!keepTitleEditor) {
        renderWorkspace(activeTabChanged);
    }
    forceWorkspaceRender = false;

    if (activeTabChanged) {
        const activeTab = elements.tabList.querySelector(`[data-tab-id="${cssEscape(state.activeTab)}"]`);
        if (activeTab) {
            activeTab.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
        }
        lastRenderedActiveTab = state.activeTab;
    }

    if (pendingEnvironmentScrollFor === state.activeTab) {
        pendingEnvironmentScrollFor = null;
        window.requestAnimationFrame(scrollLatestEnvironmentOutput);
    }

    if (pendingStatusScrollFor === state.activeTab) {
        pendingStatusScrollFor = null;
        window.requestAnimationFrame(scrollLatestStatus);
    }

    if (state.activeTab === LOGS_TAB_ID && pendingLogScroll) {
        pendingLogScroll = false;
        window.requestAnimationFrame(scrollLatestLogs);
    }
}

function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") {
        return window.CSS.escape(value);
    }

    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

function initialiseTabDragScroll() {
    const list = elements.tabList;

    list.addEventListener("pointerdown", (event) => {
        if ((event.pointerType === "mouse" && event.button !== 0) || event.target.closest("button")) {
            return;
        }

        tabDrag.pointerId = event.pointerId;
        tabDrag.startX = event.clientX;
        tabDrag.startY = event.clientY;
        tabDrag.scrollLeft = list.scrollLeft;
        tabDrag.axis = null;
        tabDrag.moved = false;
        tabDrag.suppressClick = false;
        list.setPointerCapture(event.pointerId);
    });

    list.addEventListener("pointermove", (event) => {
        if (event.pointerId !== tabDrag.pointerId) {
            return;
        }

        const distanceX = event.clientX - tabDrag.startX;
        const distanceY = event.clientY - tabDrag.startY;
        if (!tabDrag.axis && Math.max(Math.abs(distanceX), Math.abs(distanceY)) > 10) {
            tabDrag.axis = Math.abs(distanceX) >= Math.abs(distanceY) ? "horizontal" : "vertical";
        }
        if (tabDrag.axis !== "horizontal") {
            return;
        }

        tabDrag.moved = true;
        list.classList.add("is-dragging");
        list.scrollLeft = tabDrag.scrollLeft - distanceX;
        event.preventDefault();
    });

    const finishDrag = (event) => {
        if (event.pointerId !== tabDrag.pointerId) {
            return;
        }
        if (list.hasPointerCapture(event.pointerId)) {
            list.releasePointerCapture(event.pointerId);
        }
        list.classList.remove("is-dragging");
        tabDrag.suppressClick = event.type !== "pointercancel" && tabDrag.moved;
        tabDrag.pointerId = null;
    };

    list.addEventListener("pointerup", finishDrag);
    list.addEventListener("pointercancel", finishDrag);
    list.addEventListener("click", (event) => {
        if (!tabDrag.suppressClick) {
            return;
        }
        event.preventDefault();
        event.stopPropagation();
        tabDrag.suppressClick = false;
    }, true);
}

function renderTabs() {
    const fragment = document.createDocumentFragment();
    fragment.append(createHomeTab());

    if (state.logsTabOpen) {
        fragment.append(createLogsTab());
    }

    state.tabs.forEach((run) => fragment.append(createRunTab(run)));
    elements.tabList.replaceChildren(fragment);
}

function createHomeTab() {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tab-button";
    button.dataset.tabId = HOME_TAB_ID;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(state.activeTab === HOME_TAB_ID));
    button.setAttribute("aria-controls", "workspace");
    button.innerHTML = "<span class=\"tab-dot\"></span><span class=\"tab-label\">ᯤ New Run</span>";
    button.addEventListener("click", () => activateTab(HOME_TAB_ID));
    return button;
}

function createLogsTab() {
    const item = document.createElement("div");
    item.className = `tab-item${state.activeTab === LOGS_TAB_ID ? " is-active" : ""}`;
    item.setAttribute("role", "presentation");

    const button = document.createElement("button");
    button.type = "button";
    button.className = "tab-button";
    button.dataset.tabId = LOGS_TAB_ID;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(state.activeTab === LOGS_TAB_ID));
    button.setAttribute("aria-controls", "workspace");
    button.innerHTML = "<span class=\"tab-dot logs-dot\"></span><span class=\"tab-label\">Logs</span>";
    button.addEventListener("click", () => activateTab(LOGS_TAB_ID));
    item.append(button);

    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "tab-close";
    closeButton.setAttribute("aria-label", "Close logs");
    closeButton.title = "Close logs";
    closeButton.textContent = "×";
    closeButton.addEventListener("click", (event) => {
        event.stopPropagation();
        closeLogsTab();
    });
    item.append(closeButton);

    return item;
}

function createRunTab(run) {
    const item = document.createElement("div");
    item.className = `tab-item${state.activeTab === run.id ? " is-active" : ""}`;
    item.setAttribute("role", "presentation");

    const button = document.createElement("button");
    button.type = "button";
    button.className = "tab-button";
    button.dataset.tabId = run.id;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(state.activeTab === run.id));
    button.setAttribute("aria-controls", "workspace");

    const dot = document.createElement("span");
    dot.className = `tab-dot state-${run.state}`;
    dot.setAttribute("aria-hidden", "true");

    const label = document.createElement("span");
    label.className = "tab-label";
    label.textContent = run.title;

    button.append(dot, label);
    button.addEventListener("click", () => activateTab(run.id));
    item.append(button);

    if (isRunFinished(run)) {
        const closeButton = document.createElement("button");
        closeButton.type = "button";
        closeButton.className = "tab-close";
        closeButton.setAttribute("aria-label", `Close ${run.title}`);
        closeButton.title = "Close saved run";
        closeButton.textContent = "×";
        closeButton.addEventListener("click", (event) => {
            event.stopPropagation();
            closeRun(run.id);
        });
        item.append(closeButton);
    }

    return item;
}

function activateTab(tabId) {
    if (tabId !== HOME_TAB_ID && !(tabId === LOGS_TAB_ID && state.logsTabOpen) && !getRun(tabId)) {
        return;
    }

    editingRunTitleId = null;
    state.activeTab = tabId;
    persistState();
    const runTab = tabId === HOME_TAB_ID || tabId === LOGS_TAB_ID ? null : tabId;
    queueRender({ scrollEnvironmentFor: runTab, scrollStatusFor: runTab });
}

function openLogsTab() {
    const wasOpen = state.logsTabOpen;
    state.logsTabOpen = true;
    state.activeTab = LOGS_TAB_ID;
    if (!wasOpen) {
        addInterfaceLog("info", "Interface debug logs opened.");
    }
    persistState();
    pendingLogScroll = true;
    queueRender();
}

function closeRun(runId) {
    const run = getRun(runId);
    if (!run || !isRunFinished(run)) {
        return;
    }

    const index = state.tabs.findIndex((tab) => tab.id === runId);
    state.tabs.splice(index, 1);
    if (state.activeTab === runId) {
        state.activeTab = HOME_TAB_ID;
    }
    persistState();
    queueRender();
}

function closeLogsTab() {
    state.logsTabOpen = false;
    if (state.activeTab === LOGS_TAB_ID) {
        state.activeTab = HOME_TAB_ID;
    }
    persistState();
    queueRender();
}

function startRunTitleEdit(runId) {
    const run = getRun(runId);
    const currentTitle = elements.workspace.querySelector(".run-title");
    if (!run || !currentTitle || state.activeTab !== runId) {
        return;
    }

    editingRunTitleId = runId;
    const input = createRunTitleInput(run);
    currentTitle.replaceWith(input);
    window.requestAnimationFrame(() => {
        input.focus();
        input.select();
    });
}

function finishRunTitleEdit(runId, input, originalTitle, cancel = false) {
    const run = getRun(runId);
    if (!run || editingRunTitleId !== runId) {
        return;
    }

    run.title = cancel ? originalTitle : (input.value.trim() || "Untitled run");
    editingRunTitleId = null;
    persistState();
    updateRunTitleLabel(run.id, run.title);

    if (input.isConnected && state.activeTab === runId) {
        input.replaceWith(createRunTitleHeading(run));
    }
}

function updateRunTitleLabel(runId, title) {
    const label = elements.tabList.querySelector(`[data-tab-id="${cssEscape(runId)}"] .tab-label`);
    if (label) {
        label.textContent = title;
    }
}

function renderWorkspace(animate) {
    let page;
    if (state.activeTab === HOME_TAB_ID) {
        page = createHomePage();
    } else if (state.activeTab === LOGS_TAB_ID && state.logsTabOpen) {
        page = createLogsPage();
    } else {
        const run = getRun(state.activeTab);
        page = run ? createRunPage(run) : createHomePage();
    }

    if (animate) {
        page.classList.add("page-enter");
    }
    elements.workspace.replaceChildren(page);
}

function createLogsPage() {
    const page = document.createElement("section");
    page.className = "page logs-page";
    page.setAttribute("role", "tabpanel");
    page.setAttribute("aria-label", "Interface debug logs");

    const toolbar = document.createElement("header");
    toolbar.className = "logs-toolbar";
    const copy = document.createElement("div");
    const title = document.createElement("h1");
    title.textContent = "Debug Log";
    const description = document.createElement("p");
    description.textContent = "Sequence diagnostics for debugging purposes.";
    copy.append(title, description);

    const clearButton = document.createElement("button");
    clearButton.type = "button";
    clearButton.className = "secondary-button";
    clearButton.textContent = "Clear";
    clearButton.disabled = state.logs.length === 0;
    clearButton.addEventListener("click", () => {
        state.logs = [];
        persistState();
        queueRender();
    });
    toolbar.append(copy, clearButton);

    const panel = document.createElement("section");
    panel.className = "logs-panel";
    const panelHeader = document.createElement("div");
    panelHeader.className = "logs-panel-header";
    panelHeader.textContent = `${state.logs.length} saved event${state.logs.length === 1 ? "" : "s"}`;

    const list = document.createElement("ol");
    list.className = "logs-list";
    list.setAttribute("aria-live", "polite");
    if (!state.logs.length) {
        const empty = document.createElement("li");
        empty.className = "empty-logs";
        empty.textContent = "Nothing yet...";
        list.append(empty);
    } else {
        state.logs.forEach((entry) => {
            const item = document.createElement("li");
            item.className = `log-entry level-${entry.level}`;
            const time = document.createElement("time");
            time.className = "log-time";
            time.dateTime = new Date(entry.timestamp).toISOString();
            time.textContent = formatLogTime(entry.timestamp);
            const level = document.createElement("span");
            level.className = "log-level";
            level.textContent = entry.level;
            const message = document.createElement("span");
            message.className = "log-message";
            message.textContent = entry.message;
            item.append(time, level, message);
            list.append(item);
        });
    }
    panel.append(panelHeader, list);
    page.append(toolbar, panel);
    return page;
}

function formatLogTime(timestamp) {
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) {
        return "--:--:--";
    }

    return new Intl.DateTimeFormat("en-US-u-nu-latn", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
    }).format(date);
}

function createHomePage() {
    const page = document.createElement("section");
    page.className = "page home-page";
    page.setAttribute("role", "tabpanel");
    page.setAttribute("aria-label", "Start a new run");

    const intro = document.createElement("div");
    intro.className = "intro-copy";
    intro.innerHTML = `
        <h1><em>ᯤ ImpaCtODE</em><br><br>Web Interface</h1>
    `;

    const composer = document.createElement("form");
    composer.className = "code-composer";
    composer.noValidate = true;
    composer.innerHTML = `
        <div class="liquidAss-effect" aria-hidden="true"></div>
        <div class="liquidAss-shine" aria-hidden="true"></div>
        <div class="composer-content">
            <div class="composer-header">
                <label for="code-input">Code and instructions</label>
                <span class="shortcut-hint">Ctrl / ⌘ + Enter</span>
            </div>
            <textarea id="code-input" class="code-input" name="code" spellcheck="false" autocomplete="off" placeholder="# Paste code, special requirements, and any instructions here.\n\nprint(&quot;Hello Impactode!&quot;)"></textarea>
            <div class="composer-footer">
                <button class="primary-button" type="submit"><span aria-hidden="true">▶</span> Start run</button>
            </div>
        </div>
    `;

    const codeInput = composer.querySelector("#code-input");
    codeInput.value = state.draftCode;
    codeInput.addEventListener("input", () => {
        state.draftCode = codeInput.value;
        window.clearTimeout(draftSaveTimer);
        draftSaveTimer = window.setTimeout(persistState, 120);
    });
    codeInput.addEventListener("keydown", (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
            event.preventDefault();
            composer.requestSubmit();
        }
    });
    composer.addEventListener("submit", (event) => {
        event.preventDefault();
        startRunFromComposer(codeInput.value, codeInput);
    });

    page.append(intro, composer);
    return page;
}

function createRunTitleHeading(run) {
    const title = document.createElement("h1");
    title.className = "run-title";
    title.tabIndex = 0;
    title.setAttribute("role", "button");
    title.setAttribute("aria-label", "Rename run");
    title.title = "Click to rename";
    title.textContent = run.title;
    title.addEventListener("click", () => startRunTitleEdit(run.id));
    title.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            startRunTitleEdit(run.id);
        }
    });
    return title;
}

function createRunTitleInput(run) {
    const originalTitle = run.title;
    const title = document.createElement("input");
    title.type = "text";
    title.className = "run-title-input";
    title.value = run.title;
    title.maxLength = 80;
    title.setAttribute("aria-label", "Run name");
    title.addEventListener("input", () => {
        run.title = title.value.trim() || "Untitled run";
        updateRunTitleLabel(run.id, run.title);
        persistState();
    });
    title.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            finishRunTitleEdit(run.id, title, originalTitle);
        } else if (event.key === "Escape") {
            event.preventDefault();
            finishRunTitleEdit(run.id, title, originalTitle, true);
        }
    });
    title.addEventListener("blur", () => finishRunTitleEdit(run.id, title, originalTitle));
    return title;
}

function createRunPage(run) {
    const page = document.createElement("section");
    page.className = "page run-page";
    page.setAttribute("role", "tabpanel");
    page.setAttribute("aria-label", `${run.title} execution details`);

    const toolbar = document.createElement("header");
    toolbar.className = "run-toolbar";
    const titleGroup = document.createElement("div");
    const title = editingRunTitleId === run.id ? createRunTitleInput(run) : createRunTitleHeading(run);
    const subTitle = document.createElement("p");
    subTitle.textContent = `Started ${formatRunTime(run.startedAt)} · ${runStateLabel(run)}`;
    titleGroup.append(title, subTitle);

    const toolbarActions = document.createElement("div");
    toolbarActions.className = "run-toolbar-actions";
    const copyButton = document.createElement("button");
    copyButton.type = "button";
    copyButton.className = "secondary-button";
    copyButton.textContent = "Copy code";
    copyButton.addEventListener("click", () => copyRunCode(run, copyButton));
    toolbarActions.append(copyButton);

    if (isRunActive(run)) {
        const abortButton = document.createElement("button");
        abortButton.type = "button";
        abortButton.className = "danger-button";
        abortButton.textContent = run.state === "aborting" ? "Aborting…" : "Abort run";
        abortButton.disabled = run.state === "aborting";
        abortButton.addEventListener("click", () => abortRun(run.id));
        toolbarActions.append(abortButton);
    }

    toolbar.append(titleGroup, toolbarActions);

    const layout = document.createElement("div");
    layout.className = "execution-layout";
    layout.append(createCodePanel(run), createOutputColumn(run));
    page.append(toolbar, layout);
    return page;
}

function createCodePanel(run) {
    const panel = document.createElement("section");
    panel.className = "panel code-panel";
    const heading = document.createElement("div");
    heading.className = "panel-heading";
    heading.innerHTML = "<h2>Input code</h2>";
    const viewer = document.createElement("textarea");
    viewer.className = "code-viewer";
    viewer.readOnly = true;
    viewer.spellcheck = false;
    viewer.value = run.code;
    panel.append(heading, viewer);
    return panel;
}

function createOutputColumn(run) {
    const column = document.createElement("div");
    column.className = "run-output-column";

    if (run.error) {
        const notice = document.createElement("p");
        notice.className = "notice";
        notice.textContent = run.error;
        column.append(notice);
    }

    column.append(createEnvironmentPanel(run), createStatusPanel(run));

    if (run.guidance) {
        const guidance = document.createElement("section");
        guidance.className = "panel guidance-panel";
        const heading = document.createElement("div");
        heading.className = "panel-heading";
        heading.innerHTML = "<h2>Possible fixes</h2>";
        const content = document.createElement("pre");
        content.className = "guidance-content";
        content.textContent = run.guidance;
        guidance.append(heading, content);
        column.append(guidance);
    }

    return column;
}

function createStatusPanel(run) {
    const panel = document.createElement("section");
    panel.className = "panel status-panel";

    const heading = document.createElement("div");
    heading.className = "panel-heading";
    const statusTitle = document.createElement("h2");
    statusTitle.textContent = "Run status";
    const summary = document.createElement("div");
    summary.className = "status-summary";
    const badge = document.createElement("span");
    badge.className = `state-badge state-${run.state}`;
    badge.textContent = runStateLabel(run);
    const count = document.createElement("span");
    count.className = "status-count";
    count.textContent = run.predictedSteps
        ? `${run.statusMessages.length} / ${run.predictedSteps}`
        : `${run.statusMessages.length} update${run.statusMessages.length === 1 ? "" : "s"}`;
    summary.append(badge, count);
    heading.append(statusTitle, summary);

    const progressTrack = document.createElement("div");
    progressTrack.className = "progress-track";
    const progressBar = document.createElement("div");
    progressBar.className = "progress-bar";
    const percentage = run.predictedSteps
        ? Math.min(100, (run.statusMessages.length / run.predictedSteps) * 100)
        : isRunFinished(run) ? 100 : Math.min(88, run.statusMessages.length * 9);
    progressBar.style.width = `${percentage}%`;
    progressTrack.append(progressBar);

    const statusList = document.createElement("div");
    statusList.className = "status-list";
    statusList.setAttribute("aria-live", "polite");
    if (!run.statusMessages.length) {
        const empty = document.createElement("p");
        empty.className = "empty-status";
        empty.textContent = run.pendingSubmission
            ? "Waiting for a connection to submit this run…"
            : "The sequence will post progress here.";
        statusList.append(empty);
    } else {
        run.statusMessages.forEach((message, index) => {
            const item = document.createElement("div");
            item.className = `status-item${index === run.statusMessages.length - 1 ? " is-latest" : ""}`;
            renderStatusMessage(item, message);
            statusList.append(item);
        });
    }

    panel.append(heading, progressTrack, statusList);
    return panel;
}

function createEnvironmentPanel(run) {
    const panel = document.createElement("section");
    panel.className = "panel environment-panel";
    const heading = document.createElement("div");
    heading.className = "environment-heading";
    heading.innerHTML = "<h2>Environment output</h2>";
    const output = document.createElement("pre");
    output.className = "environment-output";
    output.dataset.runId = run.id;
    output.tabIndex = 0;
    output.setAttribute("aria-label", "Environment output. Scroll vertically or horizontally to inspect it.");
    output.textContent = run.environmentOutput || "Environment is not initialized yet…";
    panel.append(heading, output);
    return panel;
}

function renderStatusMessage(item, message) {
    let text = String(message || "");
    const checkbox = text.match(/^\[([xX ])\]\s*/);

    if (checkbox) {
        const completed = checkbox[1].toLowerCase() === "x";
        const icon = document.createElement("span");
        icon.className = `status-checkbox ${completed ? "is-complete" : "is-pending"}`;
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = completed ? "✓" : "";
        item.classList.add("has-checkbox");
        item.append(icon);
        text = text.slice(checkbox[0].length);
    }

    appendInlineMarkdown(item, text);
}

function appendInlineMarkdown(container, text) {
    const tokenPattern = /(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|==[^=]+==|~~[^~]+~~|\*[^*]+\*|_[^_]+_|\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g;
    let cursor = 0;
    let match;

    while ((match = tokenPattern.exec(text)) !== null) {
        if (match.index > cursor) {
            container.append(document.createTextNode(text.slice(cursor, match.index)));
        }
        appendMarkdownToken(container, match[0]);
        cursor = match.index + match[0].length;
    }

    if (cursor < text.length) {
        container.append(document.createTextNode(text.slice(cursor)));
    }
}

function appendMarkdownToken(container, token) {
    const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/);
    if (link) {
        const anchor = document.createElement("a");
        anchor.href = link[2];
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";
        anchor.textContent = link[1];
        container.append(anchor);
        return;
    }

    const styles = [
        ["`", "`", "code"],
        ["**", "**", "strong"],
        ["__", "__", "strong"],
        ["==", "==", "strong"],
        ["~~", "~~", "del"],
        ["*", "*", "em"],
        ["_", "_", "em"],
    ];

    const style = styles.find(([start, end]) => token.startsWith(start) && token.endsWith(end));
    if (!style) {
        container.append(document.createTextNode(token));
        return;
    }

    const element = document.createElement(style[2]);
    element.textContent = token.slice(style[0].length, -style[1].length);
    container.append(element);
}

function formatRunTime(timestamp) {
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) {
        return "just now";
    }

    return new Intl.DateTimeFormat("en-US-u-nu-latn", {
        hour: "2-digit",
        minute: "2-digit",
    }).format(date);
}

function startRunFromComposer(code, input) {
    if (!code.trim()) {
        input.focus();
        input.setAttribute("aria-invalid", "true");
        return;
    }

    input.removeAttribute("aria-invalid");
    const run = createRun({ code });
    run.pendingSubmission = true;
    run.statusMessages.push("Preparing this code run.");
    state.draftCode = "";
    state.activeTab = run.id;
    addInterfaceLog("info", `${run.title}: run created in the browser.`);
    persistState();
    queueRender({ scrollEnvironmentFor: run.id, scrollStatusFor: run.id });
    dispatchQueuedRun(run);
}

function dispatchQueuedRun(run) {
    if (!run || !run.pendingSubmission) {
        return;
    }

    if (!socket || !socket.connected) {
        if (!window.io) {
            run.pendingSubmission = false;
            setRunError(run, "Socket.IO is unavailable, so this run could not be submitted.");
        } else {
            setRunStatus(run, "Waiting for the web server connection.");
            addInterfaceLog("warn", `${run.title}: waiting for a socket connection before submission.`);
            persistState();
            queueRender();
        }
        return;
    }

    run.pendingSubmission = false;
    run.state = "running";
    setRunStatus(run, "Sending code to the sequence runner.");
    addInterfaceLog("info", `${run.title}: submitting code to the sequence runner.`);
    persistState();
    queueRender();

    socket.emit("run_sequence", { run_id: run.id, code: run.code }, (response) => {
        if (!response || typeof response !== "object") {
            return;
        }

        if (response.error) {
            setRunError(run, messageFromValue(response.error) || String(response.error));
            persistState();
            queueRender();
            return;
        }

        applySequenceUpdate(response);
    });
}

function setRunStatus(run, message, replaceIndex = null) {
    const normalized = messageFromValue(message);
    if (!normalized) {
        return;
    }

    if (Number.isInteger(replaceIndex) && replaceIndex >= 0 && replaceIndex < run.statusMessages.length) {
        run.statusMessages[replaceIndex] = normalized;
        return;
    }

    if (run.statusMessages[run.statusMessages.length - 1] !== normalized) {
        run.statusMessages.push(normalized);
    }
}

function setRunError(run, message) {
    run.error = message || "The run could not be completed.";
    run.state = "error";
    run.finishedAt = Date.now();
    setRunStatus(run, `Error: ${run.error}`);
    addInterfaceLog("error", `${run.title}: ${run.error}`);
}

function abortRun(runId) {
    const run = getRun(runId);
    if (!run || !isRunActive(run) || run.state === "aborting") {
        return;
    }

    if (!socket || !socket.connected) {
        run.error = "Cannot abort while the connection to the web server is unavailable.";
        addInterfaceLog("warn", `${run.title}: abort could not be sent because the socket is offline.`);
        persistState();
        queueRender();
        return;
    }

    run.state = "aborting";
    setRunStatus(run, "Abort requested. Destroying the disposable environment.");
    addInterfaceLog("warn", `${run.title}: abort requested by the user.`);
    persistState();
    queueRender();
    socket.emit("abort_sequence", { run_id: run.id }, (response) => {
        if (response && typeof response === "object" && response.error) {
            run.state = "running";
            run.error = messageFromValue(response.error) || String(response.error);
            persistState();
            queueRender();
        }
    });
}

async function copyRunCode(run, button) {
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(run.code);
        } else {
            const temporary = document.createElement("textarea");
            temporary.value = run.code;
            temporary.style.position = "fixed";
            temporary.style.opacity = "0";
            document.body.append(temporary);
            temporary.select();
            document.execCommand("copy");
            temporary.remove();
        }

        const original = button.textContent;
        button.textContent = "Copied";
        button.classList.add("copy-feedback");
        window.setTimeout(() => {
            button.textContent = original;
            button.classList.remove("copy-feedback");
        }, 1100);
    } catch (error) {
        console.warn("Could not copy input code.", error);
        addInterfaceLog("error", `${run.title}: copying input code failed.`);
    }
}

function scrollLatestEnvironmentOutput() {
    const output = elements.workspace.querySelector(".environment-output");
    if (!output) {
        return;
    }

    output.classList.remove("is-updating");
    // Restart the short update animation without a long-running layout effect.
    void output.offsetWidth;
    output.classList.add("is-updating");

    const startTop = output.scrollTop;
    const targetTop = Math.max(0, output.scrollHeight - output.clientHeight);
    const duration = isPhone ? 95 : 135;
    const startedAt = performance.now();

    const move = (now) => {
        const progress = Math.min(1, (now - startedAt) / duration);
        const eased = 1 - Math.pow(1 - progress, 3);
        output.scrollTop = startTop + ((targetTop - startTop) * eased);
        if (progress < 1) {
            window.requestAnimationFrame(move);
        }
    };
    window.requestAnimationFrame(move);
}

function scrollLatestStatus() {
    const status = elements.workspace.querySelector(".status-list");
    if (status) {
        status.scrollTop = status.scrollHeight;
    }
}

function scrollLatestLogs() {
    const list = elements.workspace.querySelector(".logs-list");
    if (list) {
        list.scrollTop = list.scrollHeight;
    }
}

function connectSocket() {
    if (typeof window.io !== "function") {
        setConnectionState("is-offline", "Socket.IO unavailable");
        addInterfaceLog("error", "Socket.IO client script is unavailable.");
        return;
    }

    try {
        socket = io({
            transports: ["websocket"],
            reconnection: true,
            reconnectionDelay: 1000,
            reconnectionDelayMax: 1500,
            reconnectionAttempts: Infinity,
            timeout: 10000,
        });
        addInterfaceLog("info", "Socket.IO client initialized with WebSocket transport only.");
    } catch (error) {
        console.warn("Could not start Socket.IO.", error);
        setConnectionState("is-error", "Connection error");
        addInterfaceLog("error", `Socket.IO initialization failed${error && error.message ? `: ${error.message}` : "."}`);
        return;
    }

    socket.on("connect", () => {
        setConnectionState("is-connected", "Live");
        addInterfaceLog("info", "Socket connected.");
        subscribeToRuns();
        state.tabs.filter((run) => run.pendingSubmission).forEach(dispatchQueuedRun);
    });
    socket.on("disconnect", (reason) => {
        setConnectionState("is-offline", "Reconnecting");
        addInterfaceLog("warn", `Socket disconnected${reason ? `: ${reason}` : "."}`);
    });
    socket.on("connect_error", (error) => {
        setConnectionState("is-error", "Connection error");
        addInterfaceLog("error", `Socket connection error${error && error.message ? `: ${error.message}` : "."}`);
    });
    socket.on("error", (error) => {
        addInterfaceLog("error", `Socket error${error && error.message ? `: ${error.message}` : "."}`);
    });

    socket.on("sequence_snapshot", applySequenceSnapshot);
    socket.on("sequence_update", applySequenceUpdate);
    socket.on("sequence_started", applySequenceUpdate);
    socket.on("sequence_error", applySequenceError);

    // These aliases make the UI resilient during server-side migration without changing its primary contract.
    socket.on("run_snapshot", applySequenceSnapshot);
    socket.on("run_update", applySequenceUpdate);
    socket.on("run_error", applySequenceError);
}

function setConnectionState(className, label) {
    elements.connectionStatus.className = `connection-status ${className}`;
    elements.connectionLabel.textContent = label;
}

function subscribeToRuns() {
    if (!socket || !socket.connected) {
        return;
    }

    const runIds = state.tabs.filter(isRunActive).map((run) => run.id);
    if (runIds.length) {
        socket.emit("subscribe_runs", { run_ids: runIds });
    }
}

function applySequenceSnapshot(payload) {
    const source = payload && typeof payload === "object" ? payload : {};
    const rawRuns = Array.isArray(payload)
        ? payload
        : source.runs || source.sequences || source.active_runs || source.activeSequences || [];

    const count = Array.isArray(rawRuns) ? rawRuns.length : rawRuns && typeof rawRuns === "object" ? Object.keys(rawRuns).length : 0;
    addInterfaceLog("debug", `Sequence snapshot received (${count} run${count === 1 ? "" : "s"}).`);

    if (Array.isArray(rawRuns)) {
        rawRuns.forEach((run) => applySequenceUpdate(run, { deferRender: true }));
    } else if (rawRuns && typeof rawRuns === "object") {
        Object.entries(rawRuns).forEach(([runId, run]) => {
            applySequenceUpdate({ ...(run || {}), run_id: run && (run.run_id || run.id) ? (run.run_id || run.id) : runId }, { deferRender: true });
        });
    }

    persistState();
    queueRender();
}

function applySequenceError(payload) {
    const data = unwrapUpdate(payload);
    const runId = extractRunId(data);
    const message = messageFromValue(data.message) || messageFromValue(data.error) || "The server could not run this sequence.";
    const run = runId ? getRun(runId) : null;
    if (!run) {
        addInterfaceLog("error", `Sequence error: ${message}`);
        return;
    }

    setRunError(run, message);
    persistState();
    queueRender();
}

function unwrapUpdate(payload) {
    if (!payload || typeof payload !== "object") {
        return {};
    }

    if (payload.run && typeof payload.run === "object") {
        return { ...payload, ...payload.run };
    }

    if (payload.sequence && typeof payload.sequence === "object") {
        return { ...payload, ...payload.sequence };
    }

    return payload;
}

function extractRunId(data) {
    const candidate = data.run_id || data.runId || data.sequence_id || data.sequenceId || data.id;
    return typeof candidate === "string" || typeof candidate === "number" ? String(candidate) : "";
}

function applySequenceUpdate(payload, options = {}) {
    const data = unwrapUpdate(payload);
    const runId = extractRunId(data);
    if (!runId) {
        return;
    }

    let run = getRun(runId);
    if (!run) {
        run = createRun({
            id: runId,
            code: typeof data.code === "string" ? data.code : "",
            title: nonEmptyString(data.title) || nonEmptyString(data.name),
        });
    }

    mergeRunUpdate(run, data);
    addInterfaceLog("debug", `${run.title}: sequence update received.`, { coalesceKey: `sequence-update:${run.id}` });
    if (isRunFinished(run)) {
        flushScheduledPersistence();
        persistState();
    } else {
        schedulePersist();
    }

    if (!options.deferRender) {
        const receivedOutput = hasOwn(data, "environment_output") || hasOwn(data, "environmentOutput") || hasOwn(data, "environment") || hasOwn(data, "output");
        const receivedStatus = hasOwn(data, "status_messages") || hasOwn(data, "statusMessages") || hasOwn(data, "statuses") || hasOwn(data, "message") || hasOwn(data, "status_message") || hasOwn(data, "update");
        queueRender({
            scrollEnvironmentFor: receivedOutput ? run.id : null,
            scrollStatusFor: receivedStatus ? run.id : null,
        });
    }
}

function mergeRunUpdate(run, data) {
    run.pendingSubmission = false;

    if (typeof data.code === "string") {
        run.code = data.code;
    }
    if (nonEmptyString(data.title) || nonEmptyString(data.name)) {
        run.title = nonEmptyString(data.title) || nonEmptyString(data.name);
    }
    if (positiveInteger(data.predicted_steps || data.predictedSteps)) {
        run.predictedSteps = positiveInteger(data.predicted_steps || data.predictedSteps);
    }

    const fullStatuses = data.status_messages || data.statusMessages || data.statuses;
    if (Array.isArray(fullStatuses)) {
        run.statusMessages = normalizeMessages(fullStatuses);
    } else {
        const message = messageFromValue(data.message) || messageFromValue(data.status_message) || messageFromValue(data.update);
        if (message) {
            const replaceIndex = numberOrNull(data.replace_index ?? data.replaceIndex ?? data.status_index ?? data.statusIndex);
            setRunStatus(run, message, replaceIndex);
        }
    }

    const environmentOutput = firstStringProperty(data, ["environment_output", "environmentOutput", "environment", "output"]);
    if (environmentOutput !== null) {
        run.environmentOutput = environmentOutput;
    }

    const guidance = firstStringProperty(data, ["guidance", "possible_fixes", "possibleFixes"]);
    if (guidance !== null) {
        run.guidance = guidance;
    }

    const error = firstStringProperty(data, ["error", "error_message", "errorMessage"]);
    if (error) {
        run.error = error;
    }

    const explicitState = data.state || data.status || data.run_status || data.runStatus;
    const ended = data.ended === true || data.end_sequence === true || data.finished === true || data.complete === true;
    if (typeof explicitState === "string") {
        run.state = normalizeRunState(explicitState, ended);
    } else if (error) {
        run.state = "error";
    } else if (ended) {
        run.state = "completed";
    } else if (run.state === "queued") {
        run.state = "running";
    }

    if (isRunFinished(run)) {
        run.finishedAt = run.finishedAt || Date.now();
    }
}

function hasOwn(object, property) {
    return Object.prototype.hasOwnProperty.call(object, property);
}

function numberOrNull(value) {
    const number = Number(value);
    return Number.isInteger(number) && number >= 0 ? number : null;
}

function firstStringProperty(object, properties) {
    for (const property of properties) {
        if (hasOwn(object, property) && typeof object[property] === "string") {
            return object[property];
        }
    }
    return null;
}

function initialiseTheme() {
    const savedTheme = getCookie(THEME_KEY);
    const shouldUseLight = savedTheme === "light" || (!savedTheme && window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches);
    document.body.classList.toggle("light-mode", shouldUseLight);
    updateThemeButton(shouldUseLight);

    elements.themeToggle.addEventListener("click", () => {
        const useLight = !document.body.classList.contains("light-mode");
        document.body.classList.toggle("light-mode", useLight);
        setCookie(THEME_KEY, useLight ? "light" : "dark");
        updateThemeButton(useLight);
    });
}

function updateThemeButton(useLight) {
    elements.themeToggle.setAttribute("aria-label", useLight ? "Switch to dark mode" : "Switch to light mode");
    elements.themeToggle.title = useLight ? "Switch to dark mode" : "Switch to light mode";
}

document.body.classList.toggle("is-phone", isPhone);
initialiseTheme();
elements.debugLogsToggle.addEventListener("click", openLogsTab);
window.addEventListener("pagehide", flushScheduledPersistence);
if (state.activeTab === LOGS_TAB_ID && state.logsTabOpen) {
    pendingLogScroll = true;
} else if (state.activeTab !== HOME_TAB_ID) {
    pendingEnvironmentScrollFor = state.activeTab;
    pendingStatusScrollFor = state.activeTab;
}
initialiseTabDragScroll();
queueRender();
connectSocket();
