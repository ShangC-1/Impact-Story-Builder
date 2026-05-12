const appRoot = document.querySelector("#app");

const state = {
  schema: null,
  backendHealth: null,
  currentUser: null,
  myInterviews: [],
  sharedInterviews: [],
  myInterviewSort: "updated_desc",
  dashboardEditingTitles: {},
  pendingInterviewId: "",
  activeInterviewId: "",
  currentInterviewProjectName: "",
  currentInterviewVisibility: "private",
  currentInterviewDraftStatus: "draft",
  currentInterviewOwnerEmail: "",
  currentInterviewCanEdit: true,
  currentInterviewCopiedFromId: "",
  currentStepIndex: 0,
  answers: {},
  aiInferences: {},
  generatedStory: "",
  conciseVersion: "",
  reviewNotes: [],
  validationErrors: {},
  loading: true,
  loadError: "",
  statusMessage: "",
  statusTone: "info",
  dashboardStatusMessage: "",
  dashboardStatusTone: "info",
  pendingAction: "",
  reviewReturnStepIndex: null,
  providerSettings: {
    provider: "mock",
    apiKey: "",
    baseUrl: "",
    model: "",
  },
  providerStatusMessage: "",
  providerStatusTone: "info",
  authForm: {
    email: "",
    password: "",
  },
  authStatusMessage: "",
  authStatusTone: "info",
  settingsExpanded: true,
  voiceInputSupported: false,
  activeDictationFieldKey: "",
  cleaningFieldKey: "",
};

const REQUIRED_STATUS_LABELS = {
  required: "Required",
  optional: "Optional",
  optional_but_strongly_encouraged: "Strongly encouraged",
};

const INPUT_TYPE_LABELS = {
  text: "text",
  textarea: "text",
  single_select_cards: "single_select_cards",
  multi_select_cards: "multi_select_cards",
};

const LINKEDIN_REWRITER_URL = "https://translate.kagi.com/?from=en&to=linkedin";
const CURRENT_FLOW_VERSION = "merged_step_v2";
const STORY_TONE_OPTIONS = [
  {
    key: "professional",
    label: "Professional",
    description:
      "Clear, polished, evidence-based, and suitable for internal reports, program communications, and general policy or funder audiences.",
  },
  {
    key: "conversational",
    label: "Conversational",
    description:
      "Accessible, plain-language, and easier to read while still sounding credible and evidence-based.",
  },
  {
    key: "funder_facing",
    label: "Funder-facing",
    description:
      "Outcome-oriented and evidence-forward, with emphasis on contribution, uptake, durability, scale, and future potential.",
  },
];
const STORY_LENGTH_LIMITS = {
  min: 100,
  max: 750,
  window: 50,
  startMax: 700,
  defaultMin: 300,
  defaultMax: 350,
};
const VOICE_INPUT_PRIVACY_NOTE =
  "Voice input creates a draft transcript. Please review and edit before saving or generating.";
const VOICE_INPUT_UNSUPPORTED_MESSAGE =
  "Voice input is not supported in this browser. Please use Chrome or Edge, or type manually.";
let activeSpeechRecognition = null;

bootstrap();

async function bootstrap() {
  render();
  const initialInterviewId = getInterviewIdFromUrl();
  state.pendingInterviewId = initialInterviewId;

  try {
    const [schema, backendHealth] = await Promise.all([
      fetchJson("./schema/impactStorySchema.json"),
      fetchJson("/api/health"),
    ]);
    const currentUser = await fetchCurrentUserOrNull();
    initializeState(schema, backendHealth, currentUser);
    if (currentUser) {
      await refreshInterviewDashboard();
    }
    if (currentUser && initialInterviewId) {
      try {
        await loadInterviewDraft(initialInterviewId, { statusMessage: "Saved interview draft loaded." });
      } catch (error) {
        state.statusMessage = getErrorMessage(error);
        state.statusTone = "warning";
      }
    }
  } catch (error) {
    state.loadError = error instanceof Error ? error.message : "Unknown loading error";
  } finally {
    state.loading = false;
    render();
  }
}

function initializeState(schema, backendHealth, currentUser) {
  state.schema = schema;
  state.backendHealth = backendHealth;
  state.currentUser = currentUser;
  state.myInterviews = [];
  state.sharedInterviews = [];
  state.myInterviewSort = "updated_desc";
  state.dashboardEditingTitles = {};
  state.statusMessage = "";
  state.statusTone = "info";
  state.dashboardStatusMessage = "";
  state.dashboardStatusTone = "info";
  state.pendingAction = "";
  state.providerSettings = createInitialProviderSettings(backendHealth);
  state.providerStatusMessage = "";
  state.providerStatusTone = "info";
  state.authForm = {
    email: currentUser?.email || "",
    password: "",
  };
  state.authStatusMessage = "";
  state.authStatusTone = "info";
  state.voiceInputSupported = Boolean(getSpeechRecognitionConstructor());
  state.activeDictationFieldKey = "";
  state.cleaningFieldKey = "";
  resetInterviewWorkspace();
}

function resetInterviewWorkspace() {
  stopActiveDictation({ preserveStatus: true, renderAfter: false });
  state.activeInterviewId = "";
  state.currentInterviewProjectName = "";
  state.currentInterviewVisibility = "private";
  state.currentInterviewDraftStatus = "draft";
  state.currentInterviewOwnerEmail = state.currentUser?.email || "";
  state.currentInterviewCanEdit = true;
  state.currentInterviewCopiedFromId = "";
  state.currentStepIndex = 0;
  state.answers = {};
  state.aiInferences = {};
  state.generatedStory = "";
  state.conciseVersion = "";
  state.reviewNotes = [];
  state.validationErrors = {};
  state.reviewReturnStepIndex = null;
  state.cleaningFieldKey = "";
  setInterviewIdInUrl("");

  for (const field of getAllFields()) {
    state.answers[field.fieldKey] = field.inputType === "multi_select_cards" ? [] : "";
  }
  state.answers.story_tone = STORY_TONE_OPTIONS[0].key;
  state.answers.story_length_min = STORY_LENGTH_LIMITS.defaultMin;
  state.answers.story_length_max = STORY_LENGTH_LIMITS.defaultMax;
}

function createInitialProviderSettings(backendHealth) {
  const provider = backendHealth?.defaults?.defaultProvider || "mock";
  if (provider === "claude") {
    return {
      provider,
      apiKey: "",
      baseUrl: backendHealth?.defaults?.claude?.baseUrl || "https://api.anthropic.com",
      model: backendHealth?.defaults?.claude?.model || "claude-sonnet-4-6",
    };
  }

  if (provider === "openai_compatible") {
    return {
      provider,
      apiKey: "",
      baseUrl: backendHealth?.defaults?.openaiCompatible?.baseUrl || "https://api.openai.com",
      model: "",
    };
  }

  return {
    provider: "mock",
    apiKey: "",
    baseUrl: "",
    model: "",
  };
}

function getSteps() {
  return state.schema?.steps ?? [];
}

function getCurrentStep() {
  return getSteps()[state.currentStepIndex];
}

function getAllFields(schema = state.schema) {
  if (!schema) {
    return [];
  }

  return schema.steps.flatMap((step) => step.fields ?? []);
}

function getFieldByKey(fieldKey) {
  return getAllFields().find((field) => field.fieldKey === fieldKey) ?? null;
}

function getSpeechRecognitionConstructor() {
  if (typeof window === "undefined") {
    return null;
  }
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function supportsVoiceInput() {
  return Boolean(state.voiceInputSupported && getSpeechRecognitionConstructor());
}

function isVoiceCapableField(field) {
  return field?.inputType === "textarea";
}

function getVoiceCapableFields(step = getCurrentStep()) {
  return (step?.fields ?? []).filter((field) => isVoiceCapableField(field));
}

function getOutcomeOptionLabel(optionKey) {
  const option = state.schema?.outcomeTypeOptions?.find((item) => item.key === optionKey);
  return option?.label ?? optionKey;
}

function getOutcomeSelection(value = state.answers.primary_outcome_type) {
  return normalizeOutcomeSelection(value);
}

function getStoryTone() {
  return normalizeStoryTone(state.answers.story_tone);
}

function getStoryLengthMin() {
  return normalizeStoryLengthStartValue(
    state.answers.story_length_min,
    STORY_LENGTH_LIMITS.defaultMin,
    state.answers.story_length_max
  );
}

function getStoryLengthMax(lengthMin = getStoryLengthMin()) {
  return deriveStoryLengthMax(lengthMin);
}

function getStoryStyleValidationMessage() {
  const lengthMin = getStoryLengthMin();
  const lengthMax = getStoryLengthMax();
  if (lengthMin < STORY_LENGTH_LIMITS.min || lengthMin > STORY_LENGTH_LIMITS.startMax) {
    return `Target length must stay between ${STORY_LENGTH_LIMITS.min} and ${STORY_LENGTH_LIMITS.max} words.`;
  }
  if (lengthMax > STORY_LENGTH_LIMITS.max) {
    return `Target length must stay between ${STORY_LENGTH_LIMITS.min} and ${STORY_LENGTH_LIMITS.max} words.`;
  }
  return "";
}

function getCurrentModeLabel() {
  const settings = state.providerSettings;
  if (settings.provider === "mock") {
    return "Running in Mock Mode";
  }

  if (settings.provider === "claude") {
    if (!normalizeAnswer(settings.apiKey)) {
      return "Claude selected, API key missing";
    }
    return "Using Claude API";
  }

  if (!normalizeAnswer(settings.apiKey) || !normalizeAnswer(settings.baseUrl)) {
    return "OpenAI-compatible selected, settings incomplete";
  }

  return "Using OpenAI-compatible API";
}

function getCurrentModeClass() {
  const settings = state.providerSettings;
  if (settings.provider === "mock") {
    return "meta-pill-warning";
  }
  if (settings.provider === "claude" && normalizeAnswer(settings.apiKey)) {
    return "meta-pill-success";
  }
  if (
    settings.provider === "openai_compatible" &&
    normalizeAnswer(settings.apiKey) &&
    normalizeAnswer(settings.baseUrl)
  ) {
    return "meta-pill-success";
  }
  return "meta-pill-neutral";
}

function getEffectiveModelLabel() {
  const settings = state.providerSettings;
  if (settings.provider === "mock") {
    return "No model needed";
  }
  if (settings.provider === "claude") {
    return normalizeAnswer(settings.model) || state.backendHealth?.defaults?.claude?.model || "claude-sonnet-4-6";
  }
  return normalizeAnswer(settings.model) || state.backendHealth?.defaults?.openaiCompatible?.model || "Backend default model";
}

function getProviderPayload() {
  return {
    provider: state.providerSettings.provider,
    apiKey: state.providerSettings.apiKey,
    baseUrl: normalizeAnswer(state.providerSettings.baseUrl),
    model: normalizeAnswer(state.providerSettings.model),
  };
}

function getInterviewIdFromUrl() {
  const interviewId = new URL(window.location.href).searchParams.get("interview");
  return interviewId ? interviewId.trim() : "";
}

function setInterviewIdInUrl(interviewId) {
  const url = new URL(window.location.href);
  if (interviewId) {
    url.searchParams.set("interview", interviewId);
  } else {
    url.searchParams.delete("interview");
  }
  window.history.replaceState({}, "", url);
}

function buildInterviewDraftPayload() {
  return {
    projectName: normalizeAnswer(state.currentInterviewProjectName) || normalizeAnswer(state.answers.project_name_location),
    visibility: state.currentInterviewVisibility,
    draftStatus: state.currentInterviewDraftStatus,
    currentStepIndex: state.currentStepIndex,
    reviewReturnStepIndex: state.reviewReturnStepIndex,
    answers: buildPersistedAnswers(),
    aiInferences: state.aiInferences,
    generatedStory: state.generatedStory,
    conciseVersion: state.conciseVersion,
    reviewNotes: state.reviewNotes,
  };
}

function applyInterviewDraft(interview) {
  stopActiveDictation({ preserveStatus: true, renderAfter: false });
  const answers = normalizeLoadedAnswers(interview.answers ?? {});

  state.activeInterviewId = interview.id || "";
  state.currentInterviewProjectName = interview.projectName || "";
  state.currentInterviewVisibility = interview.visibility || "private";
  state.currentInterviewDraftStatus = interview.draftStatus || "draft";
  state.currentInterviewOwnerEmail = interview.ownerEmail || state.currentUser?.email || "";
  state.currentInterviewCanEdit = Boolean(interview.canEdit);
  state.currentInterviewCopiedFromId = interview.copiedFromInterviewId || "";
  state.answers = answers;
  state.aiInferences = interview.aiInferences ?? {};
  state.generatedStory = interview.generatedStory ?? "";
  state.conciseVersion = interview.conciseVersion ?? "";
  state.reviewNotes = interview.reviewNotes ?? [];
  state.validationErrors = {};
  state.cleaningFieldKey = "";
  state.reviewReturnStepIndex =
    interview.reviewReturnStepIndex == null ? null : normalizeSavedStepIndex(interview.reviewReturnStepIndex, answers);
  state.currentStepIndex = normalizeSavedStepIndex(interview.currentStepIndex, answers);

  setInterviewIdInUrl(state.activeInterviewId);
}

function normalizeLoadedAnswers(rawAnswers = {}) {
  const nextAnswers = {};

  for (const field of getAllFields()) {
    const legacyOutcomeValue =
      field.fieldKey === "primary_outcome_type" && rawAnswers[field.fieldKey] == null
        ? rawAnswers.primary_outcome_types
        : rawAnswers[field.fieldKey];

    if (field.inputType === "multi_select_cards") {
      nextAnswers[field.fieldKey] = normalizeOutcomeSelection(legacyOutcomeValue);
    } else {
      nextAnswers[field.fieldKey] = typeof legacyOutcomeValue === "string" ? legacyOutcomeValue : "";
    }
  }

  nextAnswers.story_tone = normalizeStoryTone(rawAnswers.story_tone);
  nextAnswers.story_length_min = normalizeStoryLengthStartValue(
    rawAnswers.story_length_min,
    STORY_LENGTH_LIMITS.defaultMin,
    rawAnswers.story_length_max
  );
  nextAnswers.story_length_max = getStoryLengthMax(nextAnswers.story_length_min);
  nextAnswers.__flowVersion = normalizeAnswer(rawAnswers.__flowVersion) || CURRENT_FLOW_VERSION;
  return nextAnswers;
}

function buildPersistedAnswers() {
  const persistedAnswers = {
    ...state.answers,
    primary_outcome_type: getOutcomeSelection(),
    story_tone: getStoryTone(),
    story_length_min: getStoryLengthMin(),
    story_length_max: getStoryLengthMax(),
    __flowVersion: CURRENT_FLOW_VERSION,
  };
  delete persistedAnswers.primary_outcome_types;
  return persistedAnswers;
}

function normalizeSavedStepIndex(rawIndex, answers = state.answers) {
  const stepCount = getSteps().length;
  const normalized = Number(rawIndex);
  if (!Number.isFinite(normalized) || normalized < 0) {
    return 0;
  }

  const isLegacyFlow = normalizeAnswer(answers.__flowVersion) !== CURRENT_FLOW_VERSION;
  if (isLegacyFlow) {
    return Math.max(0, Math.min(stepCount - 1, normalized === 0 ? 0 : normalized - 1));
  }

  if (normalized >= stepCount) {
    return Math.max(0, stepCount - 1);
  }

  return normalized;
}

async function loadInterviewDraft(interviewId, options = {}) {
  const interview = await fetchJson(`/api/interviews/${encodeURIComponent(interviewId)}`);
  applyInterviewDraft(interview);
  if (options.statusMessage) {
    state.statusMessage = options.statusMessage;
    state.statusTone = "success";
  }
}

async function refreshInterviewDashboard() {
  const [mineResponse, sharedResponse] = await Promise.all([
    fetchJson("/api/interviews?scope=mine"),
    fetchJson("/api/interviews?scope=shared"),
  ]);
  state.myInterviews = mineResponse.interviews ?? [];
  state.sharedInterviews = sharedResponse.interviews ?? [];
  state.dashboardEditingTitles = {};
}

function isFormReadOnly() {
  return !state.currentInterviewCanEdit;
}

function getSortedMyInterviews() {
  const interviews = [...state.myInterviews];
  switch (state.myInterviewSort) {
    case "updated_asc":
      return interviews.sort((left, right) => compareDateValues(left.updatedAt, right.updatedAt));
    case "title_asc":
      return interviews.sort((left, right) => compareTitleValues(left.projectName, right.projectName));
    case "title_desc":
      return interviews.sort((left, right) => compareTitleValues(right.projectName, left.projectName));
    case "updated_desc":
    default:
      return interviews.sort((left, right) => compareDateValues(right.updatedAt, left.updatedAt));
  }
}

function compareDateValues(leftValue, rightValue) {
  const left = new Date(leftValue || 0).getTime();
  const right = new Date(rightValue || 0).getTime();
  const safeLeft = Number.isFinite(left) ? left : 0;
  const safeRight = Number.isFinite(right) ? right : 0;
  return safeLeft - safeRight;
}

function compareTitleValues(leftValue, rightValue) {
  const left = normalizeAnswer(leftValue) || "Untitled interview";
  const right = normalizeAnswer(rightValue) || "Untitled interview";
  return left.localeCompare(right, undefined, { sensitivity: "base" });
}

function formatDraftStatusLabel(status) {
  return String(status || "draft")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (match) => match.toUpperCase());
}

function getDraftTitleInputValue(interview) {
  const draftValue = state.dashboardEditingTitles[interview.id];
  if (typeof draftValue === "string") {
    return draftValue;
  }
  return interview.projectName || "";
}

function formatDateTime(value) {
  if (!value) {
    return "Unknown";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function render() {
  if (state.loading) {
    appRoot.innerHTML = `
      <section class="loading-state">
        <p class="eyebrow">Impact Story Builder</p>
        <h1>Loading provider-aware prototype</h1>
        <p>The interface is loading the interview schema and local backend defaults.</p>
      </section>
    `;
    return;
  }

  if (state.loadError) {
    appRoot.innerHTML = `
      <section class="error-state">
        <p class="eyebrow">Impact Story Builder</p>
        <h1>Unable to load the app</h1>
        <p>${escapeHtml(state.loadError)}</p>
        <p>Start the local Phase 2 server with <code>scripts/start-demo.ps1</code>.</p>
      </section>
    `;
    return;
  }

  if (state.backendHealth?.authMode === "manual_invite" && !state.currentUser) {
    appRoot.innerHTML = renderManualInviteLogin();
    attachListeners();
    return;
  }

  const steps = getSteps();
  const currentStep = getCurrentStep();
  const isReviewStep = currentStep.stepKey === "review_and_generate";
  const isBusy = Boolean(state.pendingAction);
  const nextLabel = state.pendingAction === "analyzing" ? "Analyzing..." : "Next";
  const generateLabel = state.pendingAction === "generating" ? "Generating..." : "Generate my impact story";
  const conciseLabel =
    state.pendingAction === "generating_concise" ? "Creating concise version..." : "Create concise LinkedIn-style version";
  const testLabel = state.pendingAction === "testing_provider" ? "Testing..." : "Test connection";
  const saveLabel = state.pendingAction === "saving_draft" ? "Saving..." : "Save Draft";
  const isReadOnly = isFormReadOnly();
  const showReturnToReview = state.reviewReturnStepIndex === steps.length - 1 && state.currentStepIndex < steps.length - 1;

  appRoot.innerHTML = `
    ${renderSettingsPanel(testLabel, isBusy)}

    <section class="session-card">
      <div>
        <p class="section-kicker">Session</p>
        <h2>Signed in as ${escapeHtml(state.currentUser?.email || "unknown user")}.</h2>
      </div>
      <div class="session-meta">
        <span class="meta-pill">${escapeHtml(state.currentUser?.role || "editor")}</span>
        ${
          state.activeInterviewId
            ? `<span class="meta-pill meta-pill-neutral">Draft linked</span>`
            : `<span class="meta-pill meta-pill-neutral">No draft saved yet</span>`
        }
        ${
          state.backendHealth?.authMode === "manual_invite"
            ? `<button type="button" class="secondary-button compact-button" data-action="logout" ${isBusy ? "disabled" : ""}>Logout</button>`
            : ""
        }
      </div>
    </section>

    ${renderDashboard(isBusy)}

    <section class="hero-card">
      <div class="hero-copy">
        <p class="eyebrow">Phase 2 Prototype</p>
        <h1>Impact Story Builder</h1>
        <p class="hero-text">
          A schema-driven interview flow with a local AI provider panel. The browser never stores the API key, and all external AI calls remain server-side.
        </p>
      </div>
      <div class="hero-meta">
        <span class="meta-pill ${escapeHtml(getCurrentModeClass())}">${escapeHtml(getCurrentModeLabel())}</span>
        <span class="meta-pill">${escapeHtml(getEffectiveModelLabel())}</span>
        <span class="meta-pill">Server-side AI</span>
      </div>
    </section>

    <section class="progress-card">
      <div class="progress-summary">
        <div>
          <p class="section-kicker">Progress</p>
          <h2>Step ${state.currentStepIndex + 1} of ${steps.length}</h2>
        </div>
        <p class="progress-step-name">${escapeHtml(currentStep.title)}</p>
      </div>
      <div class="progress-bar" aria-hidden="true">
        <span style="width: ${((state.currentStepIndex + 1) / steps.length) * 100}%"></span>
      </div>
      <ol class="progress-steps">
        ${steps
          .map((step, index) => {
            const status =
              index === state.currentStepIndex ? "current" : index < state.currentStepIndex ? "complete" : "upcoming";
            return `
              <li class="progress-step progress-step-${status}">
                <span class="progress-step-number">${index + 1}</span>
                <div>
                  <p>${escapeHtml(step.title)}</p>
                  <small>${escapeHtml(step.stepKey.replaceAll("_", " "))}</small>
                </div>
              </li>
            `;
          })
          .join("")}
      </ol>
    </section>

    <section class="step-card" ${isBusy ? 'aria-busy="true"' : ""}>
      <div class="step-header">
        <div>
          <p class="section-kicker">${escapeHtml(currentStep.stepKey.replaceAll("_", " "))}</p>
          <h2>${escapeHtml(currentStep.title)}</h2>
        </div>
        <span class="step-badge">Step ${currentStep.stepNumber}</span>
      </div>
      <p class="step-purpose">${escapeHtml(currentStep.purpose)}</p>

      ${
        isReadOnly && state.activeInterviewId
          ? `<div class="status-banner status-warning">
              This shared interview is read-only. Copy to My Drafts to edit your own version.
              <button type="button" class="secondary-button inline-banner-button" data-action="copy-interview" data-interview-id="${escapeHtml(state.activeInterviewId)}" ${isBusy ? "disabled" : ""}>Copy to My Drafts</button>
            </div>`
          : ""
      }

      ${
        state.statusMessage
          ? `<div class="status-banner status-${escapeHtml(state.statusTone)}">${escapeHtml(state.statusMessage)}</div>`
          : ""
      }

      ${isReviewStep ? renderReviewStep() : renderQuestionStep(currentStep)}

      <div class="step-actions">
        <div class="step-actions-left">
          <button
            type="button"
            class="secondary-button"
            data-action="back"
            ${state.currentStepIndex === 0 || isBusy ? "disabled" : ""}
          >
            Back
          </button>
          <button type="button" class="secondary-button" data-action="save-draft" ${isBusy || isReadOnly ? "disabled" : ""}>
            ${escapeHtml(saveLabel)}
          </button>
          ${
            showReturnToReview
              ? `<button type="button" class="secondary-button" data-action="return-to-review" ${isBusy ? "disabled" : ""}>Return to Review</button>`
              : ""
          }
        </div>
        ${
          isReviewStep
            ? `<button type="button" class="primary-button" data-action="generate" ${isBusy || isReadOnly ? "disabled" : ""}>${escapeHtml(generateLabel)}</button>`
            : `<button type="button" class="primary-button" data-action="next" ${isBusy ? "disabled" : ""}>${escapeHtml(nextLabel)}</button>`
        }
      </div>
    </section>
  `;

  attachListeners();

  if (state.activeDictationFieldKey && !document.getElementById(state.activeDictationFieldKey)) {
    stopActiveDictation({ preserveStatus: true });
  }
}

function renderManualInviteLogin() {
  const isBusy = state.pendingAction === "logging_in";
  const healthNote =
    state.backendHealth?.manualInviteConfigured === false
      ? `<div class="status-banner status-warning">Manual Invite Pilot mode is not configured on the server yet. Set DEMO_ALLOWED_EMAILS and DEMO_SHARED_PASSWORD, then reload.</div>`
      : "";

  return `
    <section class="hero-card">
      <div class="hero-copy">
        <p class="eyebrow">Manual Invite Pilot</p>
        <h1>Impact Story Builder</h1>
        <p class="hero-text">Sign in with your invited email and the shared team password to access the internal demo workspace.</p>
      </div>
    </section>

    <section class="auth-card">
      <div>
        <p class="section-kicker">Sign In</p>
        <h2>Enter your invite details</h2>
        <p class="field-hint">This demo mode is for short-term internal testing only. Your email is used as the draft owner identity for shared interviews.</p>
      </div>
      ${healthNote}
      ${
        state.authStatusMessage
          ? `<div class="status-banner status-${escapeHtml(state.authStatusTone)}">${escapeHtml(state.authStatusMessage)}</div>`
          : ""
      }
      <div class="auth-form">
        <label class="settings-field">
          <span>Email</span>
          <input
            class="text-input"
            data-auth-field="email"
            type="email"
            value="${escapeHtml(state.authForm.email)}"
            placeholder="name@example.org"
            autocomplete="username"
            ${isBusy ? "disabled" : ""}
          />
        </label>
        <label class="settings-field">
          <span>Shared team password</span>
          <input
            class="text-input"
            data-auth-field="password"
            type="password"
            value="${escapeHtml(state.authForm.password)}"
            placeholder="Enter shared password"
            autocomplete="current-password"
            ${isBusy ? "disabled" : ""}
          />
        </label>
      </div>
      <div class="auth-actions">
        <button type="button" class="primary-button" data-action="login" ${isBusy ? "disabled" : ""}>
          ${isBusy ? "Signing in..." : "Sign In"}
        </button>
      </div>
    </section>
  `;
}

function renderDashboard(isBusy) {
  return `
    <section class="dashboard-card">
      <div class="dashboard-header">
        <div>
          <p class="section-kicker">Interview Dashboard</p>
          <h2>Workspace</h2>
        </div>
        <div class="dashboard-actions">
          <button type="button" class="primary-button" data-action="new-interview" ${isBusy ? "disabled" : ""}>New Interview</button>
        </div>
      </div>
      <div class="dashboard-banner-slot">
        ${
          state.dashboardStatusMessage
            ? `<div class="status-banner status-${escapeHtml(state.dashboardStatusTone)}">${escapeHtml(state.dashboardStatusMessage)}</div>`
            : `<div class="dashboard-banner-spacer" aria-hidden="true"></div>`
        }
      </div>
      <div class="dashboard-grid">
        ${renderDashboardSection("My Interviews", getSortedMyInterviews(), {
          emptyMessage: "No saved interviews yet. Start a new interview, then save it here.",
          variant: "mine",
          headerControls: renderMyInterviewSortControl(isBusy),
          isBusy,
        })}
        ${renderDashboardSection("Shared Interviews", state.sharedInterviews, {
          emptyMessage: "No shared interviews are available yet.",
          variant: "shared",
          isBusy,
        })}
      </div>
    </section>
  `;
}

function renderDashboardSection(title, interviews, options) {
  const items = interviews.length
    ? interviews.map((interview) => renderInterviewListItem(interview, options)).join("")
    : `<p class="dashboard-empty">${escapeHtml(options.emptyMessage)}</p>`;

  return `
    <section class="dashboard-section">
      <div class="dashboard-section-header">
        <div class="dashboard-section-title">
          <h3>${escapeHtml(title)}</h3>
          <span>${interviews.length}</span>
        </div>
        <div class="dashboard-section-controls">
          ${options.headerControls || `<span class="dashboard-section-controls-placeholder" aria-hidden="true"></span>`}
        </div>
      </div>
      <div class="dashboard-list">${items}</div>
    </section>
  `;
}

function renderMyInterviewSortControl(isBusy) {
  return `
    <label class="dashboard-sort-control">
      <span>Sort by</span>
      <select class="dashboard-sort-select" data-dashboard-sort="my-interviews" ${isBusy ? "disabled" : ""}>
        <option value="updated_desc" ${state.myInterviewSort === "updated_desc" ? "selected" : ""}>Recently updated first</option>
        <option value="updated_asc" ${state.myInterviewSort === "updated_asc" ? "selected" : ""}>Oldest updated first</option>
        <option value="title_asc" ${state.myInterviewSort === "title_asc" ? "selected" : ""}>Title A-Z</option>
        <option value="title_desc" ${state.myInterviewSort === "title_desc" ? "selected" : ""}>Title Z-A</option>
      </select>
    </label>
  `;
}

function renderInterviewListItem(interview, options) {
  const ownerEmail = normalizeAnswer(interview.ownerEmail);
  const isOwner = Boolean(interview.isOwner);
  const isSharedSection = options.variant === "shared";
  const titleValue = getDraftTitleInputValue(interview);
  const showCopyButton = isSharedSection && !isOwner;
  return `
    <article class="interview-row">
      <div class="interview-row-primary">
        <label class="sr-only" for="dashboard-title-${escapeHtml(interview.id)}">Interview title</label>
        ${
          !isSharedSection && isOwner
            ? `<input
                id="dashboard-title-${escapeHtml(interview.id)}"
                class="dashboard-title-input"
                data-dashboard-field="title"
                data-interview-id="${escapeHtml(interview.id)}"
                type="text"
                value="${escapeHtml(titleValue)}"
                placeholder="Untitled interview"
                ${options.isBusy ? "disabled" : ""}
              />`
            : `<h4>${escapeHtml(interview.projectName || "Untitled interview")}</h4>`
        }
        <div class="interview-row-actions">
          <button type="button" class="secondary-button compact-button" data-action="open-interview" data-interview-id="${escapeHtml(interview.id)}" ${options.isBusy ? "disabled" : ""}>Open</button>
        </div>
      </div>
      <div class="interview-row-secondary">
        <span>${escapeHtml(ownerEmail || "Unknown owner")}</span>
        <span>${escapeHtml(formatDateTime(interview.updatedAt))}</span>
      </div>
      <div class="interview-row-tertiary">
        ${
          isSharedSection
            ? `
              <span class="meta-pill meta-pill-neutral compact-pill">Shared</span>
              ${
                showCopyButton
                  ? `<button type="button" class="secondary-button compact-button" data-action="copy-interview" data-interview-id="${escapeHtml(interview.id)}" ${options.isBusy ? "disabled" : ""}>Copy</button>`
                  : ""
              }
            `
            : `
              <label class="dashboard-visibility-field">
                <span class="sr-only">Visibility</span>
                <select
                  class="dashboard-visibility-select"
                  data-dashboard-field="visibility"
                  data-interview-id="${escapeHtml(interview.id)}"
                  ${options.isBusy ? "disabled" : ""}
                >
                  <option value="private" ${interview.visibility === "private" ? "selected" : ""}>Private</option>
                  <option value="shared" ${interview.visibility === "shared" ? "selected" : ""}>Shared</option>
                </select>
              </label>
              <button
                type="button"
                class="secondary-button compact-button destructive-button"
                data-action="delete-interview"
                data-interview-id="${escapeHtml(interview.id)}"
                ${options.isBusy ? "disabled" : ""}
              >Delete</button>
            `
        }
      </div>
    </article>
  `;
}

function renderSettingsPanel(testLabel, isBusy) {
  const settings = state.providerSettings;
  const showFields = settings.provider !== "mock";
  const openLabel = state.settingsExpanded ? "Hide AI Settings" : "Show AI Settings";
  const panelBody = state.settingsExpanded
    ? `
      <div class="settings-body">
        <div class="settings-grid">
          <label class="settings-field">
            <span>Provider</span>
            <select class="text-input settings-select" data-settings-field="provider" ${isBusy ? "disabled" : ""}>
              <option value="mock" ${settings.provider === "mock" ? "selected" : ""}>Mock AI mode</option>
              <option value="claude" ${settings.provider === "claude" ? "selected" : ""}>Claude API</option>
              <option value="openai_compatible" ${settings.provider === "openai_compatible" ? "selected" : ""}>OpenAI-compatible custom endpoint</option>
            </select>
          </label>

          ${
            showFields
              ? `
                <label class="settings-field">
                  <span>API key</span>
                  <input
                    class="text-input"
                    data-settings-field="apiKey"
                    type="password"
                    value="${escapeHtml(settings.apiKey)}"
                    placeholder="Enter API key for this session"
                    autocomplete="off"
                    spellcheck="false"
                    ${isBusy ? "disabled" : ""}
                  />
                </label>

                <label class="settings-field settings-field-wide">
                  <span>Base URL</span>
                  <input
                    class="text-input"
                    data-settings-field="baseUrl"
                    type="text"
                    value="${escapeHtml(settings.baseUrl)}"
                    placeholder="${escapeHtml(getBaseUrlPlaceholder())}"
                    autocomplete="off"
                    spellcheck="false"
                    ${isBusy ? "disabled" : ""}
                  />
                </label>

                <label class="settings-field">
                  <span>Model</span>
                  <input
                    class="text-input"
                    data-settings-field="model"
                    type="text"
                    value="${escapeHtml(settings.model)}"
                    placeholder="${escapeHtml(getModelPlaceholder())}"
                    autocomplete="off"
                    spellcheck="false"
                    ${isBusy ? "disabled" : ""}
                  />
                </label>
              `
              : `
                <div class="settings-hint-card">
                  <strong>Mock mode is active.</strong>
                  <p>No API key, base URL, or model is required for local demo use.</p>
                </div>
              `
          }
        </div>

        <div class="settings-footer">
          <div class="settings-copy">
            <p class="settings-mode-label">${escapeHtml(getCurrentModeLabel())}</p>
            <p class="settings-note">Settings stay only in memory for this browser session. API keys are never written to localStorage.</p>
          </div>
          <button type="button" class="secondary-button" data-action="test-provider" ${isBusy ? "disabled" : ""}>${escapeHtml(testLabel)}</button>
        </div>

        ${
          state.providerStatusMessage
            ? `<div class="status-banner status-${escapeHtml(state.providerStatusTone)}">${escapeHtml(state.providerStatusMessage)}</div>`
            : ""
        }
      </div>
    `
    : "";

  return `
    <section class="settings-card">
      <div class="settings-header">
        <div>
          <p class="section-kicker">AI Settings</p>
          <h2>Local demo provider configuration</h2>
        </div>
        <button type="button" class="secondary-button settings-toggle" data-action="toggle-settings">${escapeHtml(openLabel)}</button>
      </div>
      <p class="step-purpose">Choose the provider for Step 1 analysis and Step 5 generation. The frontend sends the settings to the local backend for each request, but does not persist them.</p>
      ${panelBody}
    </section>
  `;
}

function getBaseUrlPlaceholder() {
  if (state.providerSettings.provider === "claude") {
    return state.backendHealth?.defaults?.claude?.baseUrl || "https://api.anthropic.com";
  }
  return state.backendHealth?.defaults?.openaiCompatible?.baseUrl || "https://api.openai.com";
}

function getModelPlaceholder() {
  if (state.providerSettings.provider === "claude") {
    return state.backendHealth?.defaults?.claude?.model || "claude-sonnet-4-6";
  }
  return state.backendHealth?.defaults?.openaiCompatible?.model || "Backend default model";
}

function renderQuestionStep(step) {
  const fields = step.fields ?? [];
  const voiceCapableFields = getVoiceCapableFields(step);
  const voiceNoteMarkup = voiceCapableFields.length
    ? `
      <div class="voice-note-banner ${supportsVoiceInput() ? "" : "voice-note-banner-warning"}">
        ${escapeHtml(supportsVoiceInput() ? VOICE_INPUT_PRIVACY_NOTE : VOICE_INPUT_UNSUPPORTED_MESSAGE)}
      </div>
    `
    : "";
  return `
    ${voiceNoteMarkup}
    <div class="field-list">
      ${fields.map((field) => renderField(field)).join("")}
    </div>
  `;
}

function renderField(field) {
  const error = state.validationErrors[field.fieldKey];
  const inference = state.aiInferences[field.fieldKey];
  const inferenceMarkup = inference
    ? `
      <div class="inference-note">
        AI prefilled this answer from the Step 1 project context using ${escapeHtml(inference.providerLabel || "the selected provider")}. Confidence: ${Math.round(inference.confidence * 100)}%. Review and edit as needed.
      </div>
    `
    : "";

  return `
    <article class="field-card">
      <div class="field-topline">
        <label class="field-label" for="${escapeHtml(field.fieldKey)}">${escapeHtml(field.label)}</label>
        <div class="field-pills">
          <span class="field-pill">${escapeHtml(INPUT_TYPE_LABELS[field.inputType] ?? field.inputType)}</span>
          <span class="field-pill field-pill-soft">${escapeHtml(
            REQUIRED_STATUS_LABELS[field.requiredStatus] ?? field.requiredStatus,
          )}</span>
        </div>
      </div>
      <p class="field-hint">${escapeHtml(field.hint)}</p>
      <div class="field-example">
        <span>Example</span>
        <p>${escapeHtml(field.example)}</p>
      </div>
      ${renderInputControl(field)}
      ${renderFieldAssistantControls(field)}
      ${inferenceMarkup}
      ${error ? `<p class="field-error">${escapeHtml(error)}</p>` : ""}
    </article>
  `;
}

function renderFieldAssistantControls(field) {
  if (!isVoiceCapableField(field)) {
    return "";
  }

  const isReadOnly = isFormReadOnly();
  const isListening = state.activeDictationFieldKey === field.fieldKey;
  const isCleaning = state.cleaningFieldKey === field.fieldKey;
  const hasActiveOtherField =
    Boolean(state.activeDictationFieldKey) && state.activeDictationFieldKey !== field.fieldKey;

  return `
    <div class="field-assistant-row">
      <div class="field-assistant-actions">
        ${
          supportsVoiceInput()
            ? `
              <button
                type="button"
                class="secondary-button compact-button"
                data-action="toggle-dictation"
                data-field-key="${escapeHtml(field.fieldKey)}"
                ${isReadOnly || isCleaning || hasActiveOtherField ? "disabled" : ""}
              >
                ${escapeHtml(isListening ? "Stop" : "Dictate")}
              </button>
            `
            : ""
        }
        <button
          type="button"
          class="secondary-button compact-button"
          data-action="clean-field-notes"
          data-field-key="${escapeHtml(field.fieldKey)}"
          ${isReadOnly || isCleaning || isListening || Boolean(state.pendingAction) ? "disabled" : ""}
        >
          ${escapeHtml(isCleaning ? "Cleaning..." : "Clean up notes")}
        </button>
      </div>
      ${
        isListening
          ? `<span class="field-assistant-state">Listening for this field...</span>`
          : ""
      }
    </div>
  `;
}

function renderInputControl(field) {
  const value = state.answers[field.fieldKey] ?? "";
  const disabledAttr = isFormReadOnly() ? "disabled" : "";

  if (field.inputType === "textarea") {
    return `
      <textarea
        id="${escapeHtml(field.fieldKey)}"
        class="text-input textarea-input"
        data-field-key="${escapeHtml(field.fieldKey)}"
        rows="${Number(field.uiRows) > 0 ? Number(field.uiRows) : 6}"
        placeholder="Enter your response here"
        ${disabledAttr}
      >${escapeHtml(value)}</textarea>
    `;
  }

  if (field.inputType === "text") {
    return `
      <input
        id="${escapeHtml(field.fieldKey)}"
        class="text-input"
        data-field-key="${escapeHtml(field.fieldKey)}"
        type="text"
        value="${escapeHtml(value)}"
        placeholder="Enter your response here"
        ${disabledAttr}
      />
    `;
  }

  if (field.inputType === "single_select_cards" || field.inputType === "multi_select_cards") {
    const selectedValues = field.inputType === "multi_select_cards" ? getOutcomeSelection(value) : [value];
    return `
      <div class="option-grid" role="list" aria-label="${escapeHtml(field.label)}">
        ${(state.schema?.outcomeTypeOptions ?? [])
          .map((option) => {
            const isSelected = selectedValues.includes(option.key);
            return `
              <button
                type="button"
                class="option-card ${isSelected ? "option-card-selected" : ""}"
                data-action="select-option"
                data-field-key="${escapeHtml(field.fieldKey)}"
                data-option-key="${escapeHtml(option.key)}"
                data-selection-mode="${escapeHtml(field.inputType === "multi_select_cards" ? "multiple" : "single")}"
                aria-pressed="${isSelected ? "true" : "false"}"
                ${disabledAttr}
              >
                <strong>${escapeHtml(option.label)}</strong>
                <span>${escapeHtml(option.description)}</span>
              </button>
            `;
          })
          .join("")}
      </div>
    `;
  }

  return `<p class="field-error">Unsupported input type: ${escapeHtml(field.inputType)}</p>`;
}

function renderReviewStep() {
  const isReadOnly = isFormReadOnly();
  const groupedReview = getSteps()
    .filter((item) => item.stepKey !== "review_and_generate")
    .map((reviewStep, stepIndex) => {
      const rows = (reviewStep.fields ?? [])
        .map((field) => {
          const value = formatReviewValue(field, state.answers[field.fieldKey]);
          return `
            <div class="review-row">
              <dt>${escapeHtml(field.label)}</dt>
              <dd class="${value.isEmpty ? "review-empty" : ""}">${value.html}</dd>
            </div>
          `;
        })
        .join("");

      return `
        <section class="review-section">
          <div class="review-section-header">
            <div class="review-section-heading">
              <h3>${escapeHtml(reviewStep.title)}</h3>
              <span>Step ${reviewStep.stepNumber}</span>
            </div>
            <button type="button" class="secondary-button review-edit-button" data-action="edit-review-section" data-step-index="${stepIndex}">
              Edit this section
            </button>
          </div>
          <dl class="review-grid">${rows}</dl>
        </section>
      `;
    })
    .join("");

  const notesMarkup = state.reviewNotes.length
    ? `
      <div class="note-stack">
        <h3>Review notes</h3>
        <ul>
          ${state.reviewNotes.map((note) => `<li>${escapeHtml(note)}</li>`).join("")}
        </ul>
      </div>
    `
    : "";

  const storyMarkup = state.generatedStory
    ? `
      <section class="story-output">
        <div class="story-output-header">
          <div>
            <p class="section-kicker">Generated draft</p>
            <h3>Editable impact story draft</h3>
          </div>
          <button type="button" class="secondary-button" data-action="copy-story">Copy to Clipboard</button>
        </div>
        <textarea
          class="text-input textarea-input story-textarea"
          data-field-key="generated_story_draft"
          rows="14"
          ${isReadOnly ? "disabled" : ""}
        >${escapeHtml(state.generatedStory)}</textarea>
        ${notesMarkup}
      </section>
      ${renderConciseSection(conciseLabel())}
    `
    : "";

  return `
    <div class="review-intro">
      <p>Review every response before generating the draft. Empty fields are shown as <strong>Not provided.</strong></p>
    </div>
    <div class="review-stack">${groupedReview}</div>
    ${renderStoryStyleSettings()}
    ${storyMarkup}
  `;
}

function renderStoryStyleSettings() {
  const selectedTone = getStoryTone();
  const selectedToneOption =
    STORY_TONE_OPTIONS.find((toneOption) => toneOption.key === selectedTone) ?? STORY_TONE_OPTIONS[0];
  const selectedLengthMin = getStoryLengthMin();
  const selectedLengthMax = getStoryLengthMax();
  const isReadOnly = isFormReadOnly();

  return `
    <section class="story-output story-style-card">
      <div class="story-output-header story-style-header">
        <div>
          <p class="section-kicker">Before generation</p>
          <h3>Story style settings</h3>
        </div>
        <span class="field-pill">Target length: ${selectedLengthMin}-${selectedLengthMax} words.</span>
      </div>
      <div class="story-style-stack story-style-compact-grid">
        <div class="story-style-block story-style-field">
          <label class="story-style-label" for="story_tone">Tone</label>
          <select
            id="story_tone"
            class="text-input story-style-select"
            data-style-field="story_tone"
            ${isReadOnly ? "disabled" : ""}
          >
            ${STORY_TONE_OPTIONS.map(
              (toneOption) => `
                <option value="${escapeHtml(toneOption.key)}" ${toneOption.key === selectedTone ? "selected" : ""}>
                  ${escapeHtml(toneOption.label)}
                </option>
              `
            ).join("")}
          </select>
          <p class="story-helper-text">${escapeHtml(selectedToneOption.description)}</p>
        </div>
        <div class="story-style-block story-style-field">
          <div class="story-style-range-header">
            <p class="story-style-label">Length</p>
            <p class="story-helper-text">Target length: ${selectedLengthMin}-${selectedLengthMax} words.</p>
          </div>
          <input
            type="range"
            class="story-length-slider"
            min="${STORY_LENGTH_LIMITS.min}"
            max="${STORY_LENGTH_LIMITS.startMax}"
            value="${selectedLengthMin}"
            data-style-field="story_length_min"
            ${isReadOnly ? "disabled" : ""}
          />
          <div class="story-style-scale">
            <span>${STORY_LENGTH_LIMITS.min}</span>
            <strong>${selectedLengthMin}-${selectedLengthMax} words</strong>
            <span>${STORY_LENGTH_LIMITS.max}</span>
          </div>
        </div>
      </div>
    </section>
  `;
}

function conciseLabel() {
  return state.pendingAction === "generating_concise"
    ? "Creating concise version..."
    : "Create concise LinkedIn-style version";
}

function renderConciseSection(buttonLabel) {
  const isReadOnly = isFormReadOnly();
  const conciseMarkup = state.conciseVersion
    ? `
      <textarea
        class="text-input textarea-input concise-textarea"
        data-field-key="concise_version_draft"
        rows="8"
        ${isReadOnly ? "disabled" : ""}
      >${escapeHtml(state.conciseVersion)}</textarea>
      <div class="story-link-row">
        <button type="button" class="secondary-button" data-action="copy-concise">Copy concise version</button>
        <a class="secondary-button secondary-link-button" href="${LINKEDIN_REWRITER_URL}" target="_blank" rel="noreferrer noopener">
          Open LinkedIn-style rewriter
        </a>
      </div>
    `
    : `
      <div class="story-link-row">
        <button type="button" class="secondary-button" data-action="generate-concise" ${state.pendingAction || isReadOnly ? "disabled" : ""}>${escapeHtml(buttonLabel)}</button>
        <a class="secondary-button secondary-link-button" href="${LINKEDIN_REWRITER_URL}" target="_blank" rel="noreferrer noopener">
          Open LinkedIn-style rewriter
        </a>
      </div>
    `;

  const actionRow = state.conciseVersion
    ? `<div class="story-link-row story-link-row-top">
        <button type="button" class="secondary-button" data-action="generate-concise" ${state.pendingAction || isReadOnly ? "disabled" : ""}>${escapeHtml(buttonLabel)}</button>
      </div>`
    : "";

  return `
    <section class="story-output concise-output">
      <div class="story-output-header">
        <div>
          <p class="section-kicker">Optional LinkedIn-style version</p>
          <h3>Shorter public-facing draft</h3>
        </div>
      </div>
      <p class="story-helper-text">
        Optional: create a shorter public-facing version, then copy it into the external LinkedIn-style rewriter if useful.
      </p>
      ${actionRow}
      ${conciseMarkup}
    </section>
  `;
}

function formatReviewValue(field, rawValue) {
  const value =
    field.inputType === "multi_select_cards" ? normalizeOutcomeSelection(rawValue) : typeof rawValue === "string" ? rawValue.trim() : rawValue;

  if (!value || (Array.isArray(value) && value.length === 0)) {
    return {
      isEmpty: true,
      html: "Not provided.",
    };
  }

  if (field.inputType === "single_select_cards" || field.inputType === "multi_select_cards") {
    const selectedValues = Array.isArray(value) ? value : [value];
    return {
      isEmpty: false,
      html: `<div class="field-pills">${selectedValues
        .map((optionKey) => `<span class="field-pill">${escapeHtml(getOutcomeOptionLabel(optionKey))}</span>`)
        .join("")}</div>`,
    };
  }

  return {
    isEmpty: false,
    html: escapeHtml(value).replaceAll("\n", "<br />"),
  };
}

function attachListeners() {
  document.querySelectorAll("[data-auth-field]").forEach((element) => {
    element.addEventListener("input", handleAuthFieldInput);
    element.addEventListener("keydown", handleAuthFieldKeydown);
  });

  document.querySelectorAll("[data-field-key]").forEach((element) => {
    if (
      element.matches("input, textarea") &&
      element.dataset.fieldKey !== "generated_story_draft" &&
      element.dataset.fieldKey !== "concise_version_draft"
    ) {
      element.addEventListener("input", handleFieldInput);
    }
  });

  document.querySelectorAll("[data-settings-field]").forEach((element) => {
    if (element.dataset.settingsField === "provider") {
      element.addEventListener("change", handleProviderSelection);
    } else {
      element.addEventListener("input", handleProviderFieldInput);
    }
  });

  document.querySelectorAll('[data-dashboard-field="title"]').forEach((element) => {
    element.addEventListener("input", handleDashboardTitleInput);
    element.addEventListener("keydown", handleDashboardTitleKeydown);
    element.addEventListener("blur", handleDashboardTitleCommit);
  });

  document.querySelectorAll('[data-dashboard-field="visibility"]').forEach((element) => {
    element.addEventListener("change", handleDashboardVisibilityChange);
  });

  document.querySelectorAll('[data-dashboard-sort="my-interviews"]').forEach((element) => {
    element.addEventListener("change", handleMyInterviewSortChange);
  });

  document.querySelectorAll('[data-action="select-option"]').forEach((button) => {
    button.addEventListener("click", handleOptionSelect);
  });

  document.querySelectorAll('[data-action="select-tone"]').forEach((button) => {
    button.addEventListener("click", handleToneSelect);
  });

  document.querySelectorAll("[data-style-field]").forEach((element) => {
    element.addEventListener("input", handleStoryStyleInput);
    element.addEventListener("change", handleStoryStyleInput);
  });

  document.querySelectorAll('[data-action="toggle-dictation"]').forEach((button) => {
    button.addEventListener("click", handleToggleDictation);
  });

  document.querySelectorAll('[data-action="clean-field-notes"]').forEach((button) => {
    button.addEventListener("click", handleCleanFieldNotes);
  });

  document.querySelectorAll('[data-action="next"]').forEach((button) => {
    button.addEventListener("click", handleNext);
  });

  document.querySelectorAll('[data-action="back"]').forEach((button) => {
    button.addEventListener("click", handleBack);
  });

  document.querySelectorAll('[data-action="save-draft"]').forEach((button) => {
    button.addEventListener("click", handleSaveDraft);
  });

  document.querySelectorAll('[data-action="new-interview"]').forEach((button) => {
    button.addEventListener("click", handleNewInterview);
  });

  document.querySelectorAll('[data-action="login"]').forEach((button) => {
    button.addEventListener("click", handleLogin);
  });

  document.querySelectorAll('[data-action="logout"]').forEach((button) => {
    button.addEventListener("click", handleLogout);
  });

  document.querySelectorAll('[data-action="open-interview"]').forEach((button) => {
    button.addEventListener("click", handleOpenInterview);
  });

  document.querySelectorAll('[data-action="copy-interview"]').forEach((button) => {
    button.addEventListener("click", handleCopyInterview);
  });

  document.querySelectorAll('[data-action="delete-interview"]').forEach((button) => {
    button.addEventListener("click", handleDeleteInterview);
  });

  document.querySelectorAll('[data-action="seed-shared"]').forEach((button) => {
    button.addEventListener("click", handleSeedSharedInterview);
  });

  document.querySelectorAll('[data-action="generate"]').forEach((button) => {
    button.addEventListener("click", handleGenerateStory);
  });

  document.querySelectorAll('[data-action="copy-story"]').forEach((button) => {
    button.addEventListener("click", copyStoryToClipboard);
  });

  document.querySelectorAll('[data-action="edit-review-section"]').forEach((button) => {
    button.addEventListener("click", handleEditReviewSection);
  });

  document.querySelectorAll('[data-action="return-to-review"]').forEach((button) => {
    button.addEventListener("click", handleReturnToReview);
  });

  document.querySelectorAll('[data-action="generate-concise"]').forEach((button) => {
    button.addEventListener("click", handleGenerateConciseVersion);
  });

  document.querySelectorAll('[data-action="copy-concise"]').forEach((button) => {
    button.addEventListener("click", copyConciseToClipboard);
  });

  document.querySelectorAll('[data-action="test-provider"]').forEach((button) => {
    button.addEventListener("click", handleTestProvider);
  });

  document.querySelectorAll('[data-action="toggle-settings"]').forEach((button) => {
    button.addEventListener("click", () => {
      state.settingsExpanded = !state.settingsExpanded;
      render();
    });
  });

  const generatedStoryField = document.querySelector('[data-field-key="generated_story_draft"]');
  if (generatedStoryField) {
    generatedStoryField.addEventListener("input", (event) => {
      state.generatedStory = event.target.value;
      state.conciseVersion = "";
      state.statusMessage = "";
    });
  }

  const conciseVersionField = document.querySelector('[data-field-key="concise_version_draft"]');
  if (conciseVersionField) {
    conciseVersionField.addEventListener("input", (event) => {
      state.conciseVersion = event.target.value;
      state.statusMessage = "";
    });
  }
}

function handleProviderSelection(event) {
  const nextProvider = event.target.value;
  const current = state.providerSettings;

  if (nextProvider === "mock") {
    state.providerSettings = {
      provider: "mock",
      apiKey: "",
      baseUrl: "",
      model: "",
    };
  } else if (nextProvider === "claude") {
    state.providerSettings = {
      provider: "claude",
      apiKey: current.provider === "claude" ? current.apiKey : current.apiKey,
      baseUrl: state.backendHealth?.defaults?.claude?.baseUrl || "https://api.anthropic.com",
      model:
        current.provider === "claude" && normalizeAnswer(current.model)
          ? current.model
          : state.backendHealth?.defaults?.claude?.model || "claude-sonnet-4-6",
    };
  } else {
    state.providerSettings = {
      provider: "openai_compatible",
      apiKey: current.provider === "openai_compatible" ? current.apiKey : current.apiKey,
      baseUrl: state.backendHealth?.defaults?.openaiCompatible?.baseUrl || "https://api.openai.com",
      model: current.provider === "openai_compatible" ? current.model : "",
    };
  }

  state.providerStatusMessage = "";
  state.providerStatusTone = "info";
  render();
}

function handleProviderFieldInput(event) {
  const field = event.target.dataset.settingsField;
  state.providerSettings[field] = event.target.value;
  state.providerStatusMessage = "";
  state.providerStatusTone = "info";
}

function findDashboardInterview(interviewId) {
  return [...state.myInterviews, ...state.sharedInterviews].find((item) => item.id === interviewId) || null;
}

async function saveInterviewMetadata(interviewId, changes, successMessage) {
  state.pendingAction = "saving_dashboard_metadata";
  state.dashboardStatusMessage = "Saving interview metadata...";
  state.dashboardStatusTone = "info";
  render();

  try {
    const interview = await fetchJson(`/api/interviews/${encodeURIComponent(interviewId)}`);
    const payload = {
      projectName: changes.projectName ?? interview.projectName ?? interview.title ?? "",
      visibility: changes.visibility ?? interview.visibility ?? "private",
      draftStatus: interview.draftStatus ?? "draft",
      currentStepIndex: interview.currentStepIndex ?? 0,
      reviewReturnStepIndex: interview.reviewReturnStepIndex,
      answers: interview.answers ?? {},
      aiInferences: interview.aiInferences ?? {},
      generatedStory: interview.generatedStory ?? "",
      conciseVersion: interview.conciseVersion ?? "",
      reviewNotes: interview.reviewNotes ?? [],
    };
    const updated = await patchJson(`/api/interviews/${encodeURIComponent(interviewId)}`, payload);

    if (state.activeInterviewId === interviewId) {
      state.currentInterviewProjectName = updated.projectName || payload.projectName;
      state.currentInterviewVisibility = updated.visibility || payload.visibility;
      state.currentInterviewDraftStatus = updated.draftStatus || payload.draftStatus;
      state.currentInterviewOwnerEmail = updated.ownerEmail || state.currentInterviewOwnerEmail;
    }

    await refreshInterviewDashboard();
    state.dashboardStatusMessage = successMessage;
    state.dashboardStatusTone = "success";
  } catch (error) {
    state.dashboardStatusMessage = getErrorMessage(error);
    state.dashboardStatusTone = "warning";
  } finally {
    state.pendingAction = "";
    render();
  }
}

function handleDashboardTitleInput(event) {
  const interviewId = event.target.dataset.interviewId;
  if (!interviewId) {
    return;
  }
  state.dashboardEditingTitles[interviewId] = event.target.value;
}

function handleDashboardTitleKeydown(event) {
  if (event.key !== "Enter") {
    return;
  }
  event.preventDefault();
  event.currentTarget.blur();
}

async function handleDashboardTitleCommit(event) {
  const interviewId = event.target.dataset.interviewId;
  if (!interviewId) {
    return;
  }

  const interview = findDashboardInterview(interviewId);
  if (!interview || !interview.isOwner) {
    return;
  }

  const nextTitle = normalizeAnswer(state.dashboardEditingTitles[interviewId] ?? interview.projectName);
  const currentTitle = normalizeAnswer(interview.projectName);
  delete state.dashboardEditingTitles[interviewId];

  if (!nextTitle || nextTitle === currentTitle) {
    render();
    return;
  }

  await saveInterviewMetadata(interviewId, { projectName: nextTitle }, "Title updated.");
}

async function handleDashboardVisibilityChange(event) {
  const interviewId = event.target.dataset.interviewId;
  const nextVisibility = event.target.value;
  if (!interviewId) {
    return;
  }

  const interview = findDashboardInterview(interviewId);
  if (!interview || !interview.isOwner) {
    return;
  }

  if (nextVisibility === interview.visibility) {
    return;
  }

  await saveInterviewMetadata(interviewId, { visibility: nextVisibility }, "Visibility updated.");
}

function handleMyInterviewSortChange(event) {
  state.myInterviewSort = event.target.value || "updated_desc";
  render();
}

function handleAuthFieldInput(event) {
  const field = event.target.dataset.authField;
  if (!field) {
    return;
  }
  state.authForm[field] = event.target.value;
}

function handleAuthFieldKeydown(event) {
  if (event.key !== "Enter") {
    return;
  }
  event.preventDefault();
  handleLogin();
}

async function handleLogin() {
  if (state.pendingAction) {
    return;
  }

  state.pendingAction = "logging_in";
  state.authStatusMessage = "";
  state.authStatusTone = "info";
  render();

  try {
    const result = await postJson("/api/auth/login", {
      email: state.authForm.email,
      password: state.authForm.password,
    });
    state.currentUser = result.user;
    state.authForm.password = "";
    state.authStatusMessage = "";
    state.authStatusTone = "info";

    const pendingInterviewId = state.pendingInterviewId;
    resetInterviewWorkspace();
    state.pendingInterviewId = pendingInterviewId;
    await refreshInterviewDashboard();
    if (pendingInterviewId) {
      try {
        await loadInterviewDraft(pendingInterviewId, { statusMessage: "Saved interview draft loaded." });
      } catch (error) {
        state.statusMessage = getErrorMessage(error);
        state.statusTone = "warning";
      }
    }
  } catch (error) {
    state.authStatusMessage = getErrorMessage(error);
    state.authStatusTone = "warning";
  } finally {
    state.pendingAction = "";
    render();
  }
}

async function handleLogout() {
  if (state.pendingAction) {
    return;
  }

  state.pendingAction = "logging_out";
  render();

  try {
    await postJson("/api/auth/logout", {});
  } catch (error) {
    // Best effort for demo logout; continue clearing local session state.
  } finally {
    state.currentUser = null;
    state.myInterviews = [];
    state.sharedInterviews = [];
    state.dashboardEditingTitles = {};
    state.authForm = {
      email: "",
      password: "",
    };
    state.authStatusMessage = "Signed out.";
    state.authStatusTone = "success";
    state.dashboardStatusMessage = "";
    state.providerStatusMessage = "";
    state.statusMessage = "";
    state.pendingAction = "";
    resetInterviewWorkspace();
    render();
  }
}

function handleNewInterview() {
  resetInterviewWorkspace();
  state.dashboardStatusMessage = "Started a new interview draft.";
  state.dashboardStatusTone = "success";
  state.statusMessage = "";
  render();
}

async function handleOpenInterview(event) {
  const interviewId = event.currentTarget.dataset.interviewId;
  if (!interviewId) {
    return;
  }

  state.pendingAction = "loading_interview";
  state.dashboardStatusMessage = "Opening interview...";
  state.dashboardStatusTone = "info";
  render();

  try {
    await loadInterviewDraft(interviewId);
    state.dashboardStatusMessage = "Interview opened.";
    state.dashboardStatusTone = "success";
    state.statusMessage = state.currentInterviewCanEdit
      ? "Interview loaded. Continue editing below."
      : "Shared interview opened in read-only mode. Copy it to My Drafts before editing.";
    state.statusTone = state.currentInterviewCanEdit ? "success" : "warning";
  } catch (error) {
    state.dashboardStatusMessage = getErrorMessage(error);
    state.dashboardStatusTone = "warning";
  } finally {
    state.pendingAction = "";
    render();
  }
}

async function handleCopyInterview(event) {
  const interviewId = event.currentTarget.dataset.interviewId;
  if (!interviewId) {
    return;
  }

  state.pendingAction = "copying_interview";
  state.dashboardStatusMessage = "Copying interview into your drafts...";
  state.dashboardStatusTone = "info";
  render();

  try {
    const interview = await postJson(`/api/interviews/${encodeURIComponent(interviewId)}/copy`, {});
    applyInterviewDraft(interview);
    await refreshInterviewDashboard();
    state.dashboardStatusMessage = "Shared interview copied to My Drafts.";
    state.dashboardStatusTone = "success";
    state.statusMessage = "You are now editing your own copied draft.";
    state.statusTone = "success";
  } catch (error) {
    state.dashboardStatusMessage = getErrorMessage(error);
    state.dashboardStatusTone = "warning";
  } finally {
    state.pendingAction = "";
    render();
  }
}

async function handleDeleteInterview(event) {
  const interviewId = event.currentTarget.dataset.interviewId;
  if (!interviewId) {
    return;
  }

  const interview = findDashboardInterview(interviewId);
  if (!interview || !interview.isOwner) {
    return;
  }

  const confirmed = window.confirm("Delete this interview? This cannot be undone.");
  if (!confirmed) {
    return;
  }

  state.pendingAction = "deleting_interview";
  state.dashboardStatusMessage = "Deleting interview...";
  state.dashboardStatusTone = "info";
  render();

  try {
    await deleteJson(`/api/interviews/${encodeURIComponent(interviewId)}`);
    const deletedActiveInterview = state.activeInterviewId === interviewId;
    if (deletedActiveInterview) {
      resetInterviewWorkspace();
      state.statusMessage = "Interview deleted. Start a new interview or open another saved draft.";
      state.statusTone = "success";
    }

    await refreshInterviewDashboard();
    state.dashboardStatusMessage = "Interview deleted.";
    state.dashboardStatusTone = "success";
  } catch (error) {
    state.dashboardStatusMessage = getErrorMessage(error);
    state.dashboardStatusTone = "warning";
  } finally {
    state.pendingAction = "";
    render();
  }
}

async function handleSeedSharedInterview() {
  state.pendingAction = "seeding_shared";
  state.dashboardStatusMessage = "Creating a sample shared interview...";
  state.dashboardStatusTone = "info";
  render();

  try {
    await postJson("/api/demo/seed-shared-interview", {});
    await refreshInterviewDashboard();
    state.dashboardStatusMessage = "Sample shared interview is ready in Shared Interviews.";
    state.dashboardStatusTone = "success";
  } catch (error) {
    state.dashboardStatusMessage = getErrorMessage(error);
    state.dashboardStatusTone = "warning";
  } finally {
    state.pendingAction = "";
    render();
  }
}

async function handleSaveDraft() {
  if (isFormReadOnly()) {
    state.statusMessage = "This shared interview is read-only. Copy it to My Drafts before saving changes.";
    state.statusTone = "warning";
    render();
    return;
  }

  state.pendingAction = "saving_draft";
  state.statusMessage = "Saving the current interview draft...";
  state.statusTone = "info";
  render();

  try {
    const payload = buildInterviewDraftPayload();
    const interview = state.activeInterviewId
      ? await patchJson(`/api/interviews/${encodeURIComponent(state.activeInterviewId)}`, payload)
      : await postJson("/api/interviews", payload);
    applyInterviewDraft(interview);
    await refreshInterviewDashboard();
    state.statusMessage = "Draft saved. Reopen this URL later to continue editing.";
    state.statusTone = "success";
    state.dashboardStatusMessage = "Interview dashboard updated.";
    state.dashboardStatusTone = "success";
  } catch (error) {
    state.statusMessage = getErrorMessage(error);
    state.statusTone = "warning";
  } finally {
    state.pendingAction = "";
    render();
  }
}

async function handleTestProvider() {
  state.pendingAction = "testing_provider";
  state.providerStatusMessage = "Testing the selected provider...";
  state.providerStatusTone = "info";
  render();

  try {
    const result = await postJson("/api/test-provider", {
      providerSettings: getProviderPayload(),
    });
    state.providerStatusMessage = result.message || `${result.providerLabel || "Provider"} connection succeeded.`;
    state.providerStatusTone = "success";
  } catch (error) {
    state.providerStatusMessage = getErrorMessage(error);
    state.providerStatusTone = "warning";
  } finally {
    state.pendingAction = "";
    render();
  }
}

function handleFieldInput(event) {
  if (isFormReadOnly()) {
    return;
  }
  const fieldKey = event.target.dataset.fieldKey;
  state.answers[fieldKey] = event.target.value;
  delete state.validationErrors[fieldKey];
  delete state.aiInferences[fieldKey];
  state.statusMessage = "";
}

function handleToggleDictation(event) {
  const fieldKey = event.currentTarget.dataset.fieldKey;
  if (!fieldKey || isFormReadOnly()) {
    return;
  }

  if (state.activeDictationFieldKey === fieldKey) {
    stopActiveDictation();
    return;
  }

  if (!supportsVoiceInput()) {
    state.statusMessage = VOICE_INPUT_UNSUPPORTED_MESSAGE;
    state.statusTone = "warning";
    render();
    return;
  }

  if (state.activeDictationFieldKey) {
    return;
  }

  startDictation(fieldKey);
}

function startDictation(fieldKey) {
  const RecognitionConstructor = getSpeechRecognitionConstructor();
  if (!RecognitionConstructor) {
    state.statusMessage = VOICE_INPUT_UNSUPPORTED_MESSAGE;
    state.statusTone = "warning";
    render();
    return;
  }

  const recognition = new RecognitionConstructor();
  recognition.lang = "en-US";
  recognition.continuous = true;
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;

  recognition.onresult = (resultEvent) => {
    const transcriptParts = [];
    for (let index = resultEvent.resultIndex; index < resultEvent.results.length; index += 1) {
      const result = resultEvent.results[index];
      if (result.isFinal && result[0]?.transcript) {
        transcriptParts.push(result[0].transcript.trim());
      }
    }

    const transcript = transcriptParts.join(" ").trim();
    if (!transcript) {
      return;
    }

    appendTranscriptToField(fieldKey, transcript);
    state.statusMessage = "Voice transcript appended. Review and edit before saving or generating.";
    state.statusTone = "info";
    render();
  };

  recognition.onerror = (recognitionError) => {
    stopActiveDictation({ preserveStatus: true });
    state.statusMessage = formatSpeechRecognitionError(recognitionError?.error);
    state.statusTone = "warning";
    render();
  };

  recognition.onend = () => {
    if (activeSpeechRecognition !== recognition) {
      return;
    }
    activeSpeechRecognition = null;
    state.activeDictationFieldKey = "";
    render();
  };

  try {
    activeSpeechRecognition = recognition;
    state.activeDictationFieldKey = fieldKey;
    state.statusMessage = "";
    render();
    recognition.start();
  } catch (error) {
    activeSpeechRecognition = null;
    state.activeDictationFieldKey = "";
    state.statusMessage = `Voice input could not start: ${getErrorMessage(error)}`;
    state.statusTone = "warning";
    render();
  }
}

function stopActiveDictation({ preserveStatus = false, renderAfter = true } = {}) {
  const recognition = activeSpeechRecognition;
  activeSpeechRecognition = null;
  state.activeDictationFieldKey = "";

  if (!preserveStatus) {
    state.statusMessage = "";
  }

  if (recognition) {
    recognition.onresult = null;
    recognition.onerror = null;
    recognition.onend = null;
    try {
      recognition.stop();
    } catch (_error) {
      // Ignore stop errors from browsers that already ended recognition.
    }
  }

  if (renderAfter) {
    render();
  }
}

function appendTranscriptToField(fieldKey, transcript) {
  const currentValue = String(state.answers[fieldKey] ?? "");
  const nextValue = joinFieldTextAndTranscript(currentValue, transcript);
  state.answers[fieldKey] = nextValue;
  delete state.validationErrors[fieldKey];
  delete state.aiInferences[fieldKey];
}

function joinFieldTextAndTranscript(existingText, transcript) {
  const existing = String(existingText ?? "").trim();
  const nextTranscript = String(transcript ?? "").trim();
  if (!existing) {
    return nextTranscript;
  }
  if (!nextTranscript) {
    return existing;
  }

  const separator = existing.includes("\n") || existing.length > 140 ? "\n" : " ";
  return `${existing}${separator}${nextTranscript}`.trim();
}

function formatSpeechRecognitionError(errorCode) {
  const normalizedCode = String(errorCode || "").trim().toLowerCase();
  if (normalizedCode === "not-allowed" || normalizedCode === "service-not-allowed") {
    return "Microphone permission was blocked. Allow microphone access and try again.";
  }
  if (normalizedCode === "no-speech") {
    return "No speech was detected. Try again and speak more clearly.";
  }
  if (normalizedCode === "audio-capture") {
    return "No microphone was found for voice input. Check your device settings and try again.";
  }
  if (normalizedCode === "network") {
    return "Voice input failed in the browser. Check your connection and try again.";
  }
  if (normalizedCode === "aborted") {
    return "Voice input stopped.";
  }
  return "Voice input could not capture a transcript. Please type manually or try again.";
}

async function handleCleanFieldNotes(event) {
  const fieldKey = event.currentTarget.dataset.fieldKey;
  if (!fieldKey || isFormReadOnly()) {
    return;
  }

  const sourceText = String(state.answers[fieldKey] ?? "").trim();
  if (!sourceText) {
    state.statusMessage = "Add some notes to this field before using Clean up notes.";
    state.statusTone = "warning";
    render();
    return;
  }

  state.cleaningFieldKey = fieldKey;
  state.statusMessage = "";
  render();

  try {
    const result = await postJson("/api/clean-field-notes", {
      text: sourceText,
      fieldKey,
      providerSettings: getProviderPayload(),
    });
    state.answers[fieldKey] = result.cleanedText ?? sourceText;
    delete state.validationErrors[fieldKey];
    delete state.aiInferences[fieldKey];
    state.statusMessage = result.notes?.length
      ? result.notes.join(" ")
      : "Field notes cleaned. Review the text before saving or generating.";
    state.statusTone = "success";
  } catch (error) {
    state.statusMessage = getErrorMessage(error);
    state.statusTone = "warning";
  } finally {
    state.cleaningFieldKey = "";
    render();
  }
}

function handleOptionSelect(event) {
  if (isFormReadOnly()) {
    return;
  }
  const fieldKey = event.currentTarget.dataset.fieldKey;
  const optionKey = event.currentTarget.dataset.optionKey;
  const selectionMode = event.currentTarget.dataset.selectionMode || "single";
  if (selectionMode === "multiple") {
    const nextSelection = new Set(getOutcomeSelection(state.answers[fieldKey]));
    if (nextSelection.has(optionKey)) {
      nextSelection.delete(optionKey);
    } else {
      nextSelection.add(optionKey);
    }
    state.answers[fieldKey] = Array.from(nextSelection);
  } else {
    state.answers[fieldKey] = optionKey;
  }
  delete state.validationErrors[fieldKey];
  state.statusMessage = "";
  render();
}

function handleToneSelect(event) {
  if (isFormReadOnly()) {
    return;
  }
  state.answers.story_tone = event.currentTarget.dataset.toneKey || STORY_TONE_OPTIONS[0].key;
  state.statusMessage = state.generatedStory
    ? "Story style updated. Generate again to apply the new tone or length settings."
    : "";
  state.statusTone = state.generatedStory ? "info" : state.statusTone;
  render();
}

function handleStoryStyleInput(event) {
  if (isFormReadOnly()) {
    return;
  }
  const fieldKey = event.target.dataset.styleField;
  if (!fieldKey) {
    return;
  }
  if (fieldKey === "story_tone") {
    state.answers.story_tone = normalizeStoryTone(event.target.value);
  } else if (fieldKey === "story_length_min") {
    const nextLengthMin = normalizeStoryLengthStartValue(event.target.value, STORY_LENGTH_LIMITS.defaultMin);
    state.answers.story_length_min = nextLengthMin;
    state.answers.story_length_max = getStoryLengthMax(nextLengthMin);
  }
  state.statusMessage = state.generatedStory
    ? "Story style updated. Generate again to apply the new tone or length settings."
    : "";
  state.statusTone = state.generatedStory ? "info" : state.statusTone;
  render();
}

function handleEditReviewSection(event) {
  const stepIndex = Number(event.currentTarget.dataset.stepIndex);
  if (Number.isNaN(stepIndex)) {
    return;
  }

  state.reviewReturnStepIndex = getSteps().length - 1;
  state.currentStepIndex = stepIndex;
  state.statusMessage = "";
  render();
}

function handleReturnToReview() {
  if (state.reviewReturnStepIndex == null) {
    return;
  }

  state.currentStepIndex = state.reviewReturnStepIndex;
  state.statusMessage = "";
  render();
}

async function handleNext() {
  if (isFormReadOnly()) {
    if (state.currentStepIndex < getSteps().length - 1) {
      state.currentStepIndex += 1;
      state.statusMessage = "";
      render();
    }
    return;
  }

  const currentStep = getCurrentStep();
  const analyzedCurrentStep = await analyzeCurrentStepIfNeeded(currentStep);
  const errors = validateStep(currentStep);

  if (Object.keys(errors).length > 0) {
    state.validationErrors = {
      ...state.validationErrors,
      ...errors,
    };
    state.statusMessage = "Please fix the highlighted fields before moving to the next step.";
    state.statusTone = "warning";
    render();
    focusFirstErroredField(errors);
    return;
  }

  state.validationErrors = {};
  if (!analyzedCurrentStep) {
    state.statusMessage = "";
    state.statusTone = "info";
  }
  state.currentStepIndex += 1;
  render();
}

async function analyzeCurrentStepIfNeeded(currentStep) {
  if (!currentStep?.postStepBehavior?.aiAnalysis) {
    return false;
  }

  const sourceText = normalizeAnswer(state.answers.project_source_text);
  if (!sourceText) {
    return false;
  }

  state.pendingAction = "analyzing";
  state.statusMessage = `Analyzing the pasted project context with ${getCurrentModeLabel().toLowerCase()}...`;
  state.statusTone = "info";
  render();

  try {
    const result = await postJson("/api/analyze-context", {
      sourceText,
      existingAnswers: state.answers,
      providerSettings: getProviderPayload(),
    });
    applyAiInferences(result.inferredFields ?? [], result.providerLabel);
    state.statusMessage = result.inferredFields?.length
      ? `${result.providerLabel || "AI"} prefilled ${result.inferredFields.length} field${result.inferredFields.length === 1 ? "" : "s"}. Review the highlighted answers before you continue.`
      : result.summary || "Project context analyzed. No safe prefills were inferred, so continue manually.";
    state.statusTone = "success";
  } catch (error) {
    state.statusMessage = `${getErrorMessage(error)} Continue manually.`;
    state.statusTone = "warning";
  } finally {
    state.pendingAction = "";
  }

  return true;
}

function applyAiInferences(inferredFields, providerLabel) {
  for (const inference of inferredFields) {
    const fieldKey = inference.fieldKey;
    if (!fieldKey) {
      continue;
    }

    if (!normalizeAnswer(state.answers[fieldKey])) {
      state.answers[fieldKey] = inference.value ?? "";
      state.aiInferences[fieldKey] = {
        confidence: Number(inference.confidence) || 0,
        rationale: inference.rationale ?? "",
        providerLabel: providerLabel || "",
      };
    }
  }
}

function handleBack() {
  if (state.currentStepIndex === 0 || state.pendingAction) {
    return;
  }

  state.currentStepIndex -= 1;
  state.statusMessage = "";
  state.validationErrors = {};
  render();
}

async function handleGenerateStory() {
  if (isFormReadOnly()) {
    state.statusMessage = "This shared interview is read-only. Copy it to My Drafts before generating a new draft.";
    state.statusTone = "warning";
    render();
    return;
  }

  const blockingIssues = validateBlockingGenerationRules();
  const storyStyleValidationMessage = getStoryStyleValidationMessage();

  if (blockingIssues.length > 0) {
    state.statusMessage = "Required fields are still missing. Go back and complete them before generating.";
    state.statusTone = "warning";
    state.reviewNotes = blockingIssues;
    render();
    return;
  }

  if (storyStyleValidationMessage) {
    state.statusMessage = storyStyleValidationMessage;
    state.statusTone = "warning";
    render();
    return;
  }

  state.pendingAction = "generating";
  state.statusMessage = `Generating the impact story draft with ${getCurrentModeLabel().toLowerCase()}...`;
  state.statusTone = "info";
  render();

  try {
    const result = await postJson("/api/generate-story", {
      answers: buildPersistedAnswers(),
      tone: getStoryTone(),
      lengthMin: getStoryLengthMin(),
      lengthMax: getStoryLengthMax(),
      outcomeTypes: getOutcomeSelection(),
      providerSettings: getProviderPayload(),
    });
    state.generatedStory = result.storyDraft ?? "";
    state.conciseVersion = "";
    state.reviewNotes = result.reviewNotes ?? [];
    state.statusMessage = `${result.providerLabel || "AI"} generated a draft (${result.wordCount ?? state.generatedStory.split(/\s+/).filter(Boolean).length} words). You can edit the text and copy it to the clipboard.`;
    state.statusTone = "success";
  } catch (error) {
    state.statusMessage = getErrorMessage(error);
    state.statusTone = "warning";
  } finally {
    state.pendingAction = "";
    render();
  }
}

async function handleGenerateConciseVersion() {
  if (isFormReadOnly()) {
    state.statusMessage = "This shared interview is read-only. Copy it to My Drafts before creating a concise version.";
    state.statusTone = "warning";
    render();
    return;
  }

  if (!state.generatedStory) {
    state.statusMessage = "Generate the full impact story before creating the concise version.";
    state.statusTone = "warning";
    render();
    return;
  }

  state.pendingAction = "generating_concise";
  state.statusMessage = `Creating the concise public-facing version with ${getCurrentModeLabel().toLowerCase()}...`;
  state.statusTone = "info";
  render();

  try {
    const result = await postJson("/api/generate-concise-version", {
      generatedStory: state.generatedStory,
      answers: buildPersistedAnswers(),
      providerSettings: getProviderPayload(),
    });
    state.conciseVersion = result.conciseVersion ?? "";
    state.statusMessage = `${result.providerLabel || "AI"} created a concise version (${result.wordCount ?? state.conciseVersion.split(/\s+/).filter(Boolean).length} words).`;
    state.statusTone = "success";
  } catch (error) {
    state.statusMessage = getErrorMessage(error);
    state.statusTone = "warning";
  } finally {
    state.pendingAction = "";
    render();
  }
}

function validateStep(step) {
  const errors = {};

  for (const field of step.fields ?? []) {
    const error = validateField(field, state.answers[field.fieldKey]);
    if (error) {
      errors[field.fieldKey] = error;
    }
  }

  return errors;
}

function validateField(field, rawValue) {
  const rules = field.validationRule ?? {};
  const value = normalizeAnswer(rawValue);

  if (field.inputType === "single_select_cards" || field.inputType === "multi_select_cards") {
    const selectedValues = normalizeOutcomeSelection(rawValue);
    if (rules.required && selectedValues.length === 0) {
      return field.inputType === "multi_select_cards"
        ? "Choose at least one outcome type to continue."
        : "Choose one outcome type to continue.";
    }
    return "";
  }

  if (rules.required && !value) {
    return "This field is required.";
  }

  if (!value) {
    return "";
  }

  if (rules.minLength && value.length < rules.minLength) {
    return `Enter at least ${rules.minLength} characters.`;
  }

  if (rules.minLengthIfPresent && value.length < rules.minLengthIfPresent) {
    return `Enter at least ${rules.minLengthIfPresent} characters or leave this field blank.`;
  }

  if (rules.maxLength && value.length > rules.maxLength) {
    return `Keep this answer under ${rules.maxLength} characters.`;
  }

  return "";
}

function validateBlockingGenerationRules() {
  const messages = [];

  for (const field of getAllFields()) {
    const rawValue = state.answers[field.fieldKey];
    const blocksGeneration = field.missingStateDisplayRule?.blocksGeneration;
    const hasValue =
      field.inputType === "multi_select_cards"
        ? normalizeOutcomeSelection(rawValue).length > 0
        : Boolean(normalizeAnswer(rawValue));
    if (blocksGeneration && !hasValue) {
      messages.push(`${field.label}: required before generation.`);
    }
  }

  return messages;
}

async function copyStoryToClipboard() {
  if (!state.generatedStory) {
    state.statusMessage = "Generate a story before copying.";
    state.statusTone = "warning";
    render();
    return;
  }

  try {
    await navigator.clipboard.writeText(state.generatedStory);
    state.statusMessage = "Story copied to the clipboard.";
    state.statusTone = "success";
  } catch (error) {
    state.statusMessage = "Clipboard access was blocked by the browser. Copy the text manually from the draft area.";
    state.statusTone = "warning";
  }

  render();
}

async function copyConciseToClipboard() {
  if (!state.conciseVersion) {
    state.statusMessage = "Create the concise version before copying it.";
    state.statusTone = "warning";
    render();
    return;
  }

  try {
    await navigator.clipboard.writeText(state.conciseVersion);
    state.statusMessage = "Concise version copied to the clipboard.";
    state.statusTone = "success";
  } catch (error) {
    state.statusMessage = "Clipboard access was blocked by the browser. Copy the concise text manually from the draft area.";
    state.statusTone = "warning";
  }

  render();
}

function focusFirstErroredField(errors) {
  const firstFieldKey = Object.keys(errors)[0];
  const target = document.getElementById(firstFieldKey);
  if (target) {
    target.focus();
  }
}

function normalizeOutcomeSelection(value) {
  const rawValues = Array.isArray(value) ? value : typeof value === "string" ? [value] : [];
  return Array.from(
    new Set(
      rawValues
        .map((item) => (typeof item === "string" ? item.trim() : ""))
        .filter(Boolean)
    )
  );
}

function normalizeStoryTone(value) {
  const toneKey = normalizeAnswer(value) || STORY_TONE_OPTIONS[0].key;
  if (toneKey === "formal") {
    return "professional";
  }
  return STORY_TONE_OPTIONS.some((item) => item.key === toneKey) ? toneKey : STORY_TONE_OPTIONS[0].key;
}

function normalizeStoryLengthValue(value, fallback) {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(STORY_LENGTH_LIMITS.min, Math.min(STORY_LENGTH_LIMITS.max, parsed));
}

function normalizeStoryLengthStartValue(value, fallback, pairedMaxValue = null) {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  let normalizedValue = Number.isFinite(parsed) ? parsed : fallback;
  if (!Number.isFinite(parsed) && pairedMaxValue != null) {
    const parsedMax = Number.parseInt(String(pairedMaxValue ?? ""), 10);
    if (Number.isFinite(parsedMax)) {
      normalizedValue = parsedMax - STORY_LENGTH_LIMITS.window;
    }
  }
  return Math.max(STORY_LENGTH_LIMITS.min, Math.min(STORY_LENGTH_LIMITS.startMax, normalizedValue));
}

function deriveStoryLengthMax(lengthMin) {
  const safeLengthMin = normalizeStoryLengthStartValue(lengthMin, STORY_LENGTH_LIMITS.defaultMin);
  return Math.max(
    STORY_LENGTH_LIMITS.min + STORY_LENGTH_LIMITS.window,
    Math.min(STORY_LENGTH_LIMITS.max, safeLengthMin + STORY_LENGTH_LIMITS.window)
  );
}

function normalizeAnswer(value) {
  if (typeof value !== "string") {
    return "";
  }
  return value.trim();
}

function getErrorMessage(error) {
  if (error instanceof Error) {
    return error.message;
  }
  return "Unexpected request error.";
}

async function fetchCurrentUserOrNull() {
  try {
    return await fetchJson("/api/me");
  } catch (error) {
    if (error instanceof Error && Number(error.status) === 401) {
      return null;
    }
    throw error;
  }
}

async function fetchJson(url) {
  const response = await fetch(url, {
    credentials: "same-origin",
  });
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    data = {};
  }
  if (!response.ok) {
    const requestError = new Error(data.error || `Unable to load ${url} (${response.status})`);
    requestError.status = response.status;
    throw requestError;
  }
  return data;
}

async function postJson(url, payload) {
  return sendJson(url, "POST", payload);
}

async function patchJson(url, payload) {
  return sendJson(url, "PATCH", payload);
}

async function deleteJson(url) {
  return sendJson(url, "DELETE");
}

async function sendJson(url, method, payload) {
  const options = {
    method,
    credentials: "same-origin",
    headers: {},
  };

  if (payload !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }

  const response = await fetch(url, options);

  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    data = {};
  }

  if (!response.ok) {
    const requestError = new Error(data.error || `Request failed (${response.status})`);
    requestError.status = response.status;
    throw requestError;
  }

  return data;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
