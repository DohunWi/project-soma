// Pure Front lifecycle. Backend phases, not score/progress, enable measurement.
const PHASE_ORDER = { OFF: 0, CALIBRATING: 1, READY: 2, MEASURING: 3 };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SESSION_MEMORY_LIMIT = 32; // Bounded UI race guard, not persistent session history.

export function initialLifecycle(connectionGeneration = 0) {
    return {
        phase: "OFF", sessionId: null, calibration: null, phaseTime: null,
        connected: false, connectionGeneration, requestGeneration: 0,
        pendingAction: null, stopRequested: false, error: null,
        bufferedPhases: [], closedSessions: [], presentationRevision: 0,
        allowSynchronization: true,
    };
}

function validPhase(event) {
    if (!event || event.v !== 1 || !Number.isFinite(event.t)) return false;
    if (!Object.hasOwn(PHASE_ORDER, event.phase)) return false;
    if (event.phase === "OFF") return event.active === false;
    return event.active === true && UUID.test(event.session_id) &&
        ["COLLECTING", "READY", "ACCEPTED", "FAILED"].includes(event.calibration?.status);
}

function closed(state, sessionId) {
    // Session memory is bounded; it is a UI guard, not an ownership/security boundary.
    return [...new Set([...state.closedSessions, sessionId].filter(Boolean))].slice(-SESSION_MEMORY_LIMIT);
}

function applyPhase(state, event) {
    if (state.sessionId === event.session_id && state.phaseTime !== null &&
        event.t < state.phaseTime) return state;
    if (event.phase === "OFF") {
        return {
            ...state, phase: "OFF", sessionId: null, calibration: null, phaseTime: event.t,
            closedSessions: closed(state, event.session_id || state.sessionId),
            presentationRevision: state.presentationRevision + 1,
            pendingAction: state.pendingAction === "stop" ? "stop" : null,
            allowSynchronization: false,
        };
    }
    if (state.sessionId === event.session_id) {
        if (PHASE_ORDER[event.phase] < PHASE_ORDER[state.phase]) return state;
        if (state.calibration?.status === "FAILED" && event.calibration.status !== "FAILED") {
            return state;
        }
    }
    const newSession = state.sessionId !== event.session_id;
    return {
        ...state, phase: event.phase, sessionId: event.session_id,
        calibration: event.calibration, phaseTime: event.t, error: null,
        pendingAction: state.pendingAction === "sync" ? null : state.pendingAction,
        allowSynchronization: false,
        presentationRevision: state.presentationRevision + (newSession ? 1 : 0),
    };
}

export function reduceLifecycle(state, action) {
    if (action.type === "CONNECTION_RESET") {
        return {
            ...initialLifecycle(action.generation),
            closedSessions: action.newUser ? [] : state.closedSessions,
            requestGeneration: state.requestGeneration + 1,
            presentationRevision: state.presentationRevision + 1,
        };
    }
    if (action.generation !== state.connectionGeneration) return state;
    switch (action.type) {
        case "CONNECTED":
            return { ...state, connected: true, error: null };
        case "START":
            return {
                ...state, phase: "OFF", sessionId: null, calibration: null, phaseTime: null,
                pendingAction: "start", stopRequested: false, error: null,
                bufferedPhases: [], requestGeneration: state.requestGeneration + 1,
                presentationRevision: state.presentationRevision + 1,
            };
        case "STOP":
            return {
                ...state, pendingAction: "stop", stopRequested: true, error: null,
                requestGeneration: state.requestGeneration + 1,
                presentationRevision: state.presentationRevision + 1,
            };
        case "START_ACK": {
            if (action.request !== state.requestGeneration || state.stopRequested) return state;
            if (state.closedSessions.includes(action.sessionId)) {
                return { ...state, pendingAction: null, bufferedPhases: [] };
            }
            let updated = { ...state, sessionId: action.sessionId, pendingAction: "sync" };
            const event = state.bufferedPhases.find(item => item.session_id === action.sessionId);
            if (event) updated = applyPhase(updated, event);
            return { ...updated, bufferedPhases: [] };
        }
        case "STOP_ACK":
            if (action.request !== state.requestGeneration) return state;
            return {
                ...applyPhase(state, { phase: "OFF", session_id: action.sessionId, t: state.phaseTime ?? 0 }),
                pendingAction: null, stopRequested: false, error: null, bufferedPhases: [],
            };
        case "ERROR":
            if (action.request !== undefined && action.request !== state.requestGeneration) {
                return state;
            }
            return { ...state, pendingAction: null, error: action.error };
        case "PHASE": {
            const event = action.event;
            if (!validPhase(event)) return state;
            if (event.session_id && state.closedSessions.includes(event.session_id)) return state;
            if (state.pendingAction === "start" && !state.sessionId) {
                const old = state.bufferedPhases.find(item => item.session_id === event.session_id);
                if (old && (old.phase === "OFF" || event.t < old.t ||
                    (event.phase !== "OFF" && (PHASE_ORDER[event.phase] < PHASE_ORDER[old.phase] ||
                        (old.calibration?.status === "FAILED" && event.calibration.status !== "FAILED"))))) {
                    return state;
                }
                return {
                    ...state,
                    bufferedPhases: [...state.bufferedPhases.filter(
                        item => item.session_id !== event.session_id,
                    ), event].slice(-SESSION_MEMORY_LIMIT),
                };
            }
            if (state.sessionId && event.session_id !== state.sessionId) return state;
            if (state.stopRequested && event.phase !== "OFF") return state;
            if (!state.sessionId && !state.allowSynchronization &&
                !["OFF", "CALIBRATING"].includes(event.phase)) return state;
            return applyPhase(state, event);
        }
        default:
            return state;
    }
}

export function canRenderState(state, generation = state.connectionGeneration) {
    return generation === state.connectionGeneration && state.connected &&
        state.phase === "MEASURING" && !!state.sessionId && !state.stopRequested &&
        !["start", "stop", "sync"].includes(state.pendingAction);
}

export function feedbackPresentation(state, event, generation = state.connectionGeneration) {
    if (!canRenderState(state, generation) || event?.v !== 1 ||
        !Number.isFinite(event.t) || event.session_id !== state.sessionId ||
        event.transition !== true) return null;
    if (event.level === "NORMAL") {
        return event.reason === undefined || event.reason === "ABSENT" ? { dismiss: true } : null;
    }
    const messages = {
        NOTICE: { icon: "warning", title: "수치 변화가 감지됐어요",
            text: "현재 상태를 확인하고 잠깐 움직여보세요." },
        WARNING: { icon: "warning", title: "작업 부하가 누적되고 있어요",
            text: "작업을 잠시 멈추고 편하게 움직여보세요." },
        BREAK: { icon: "info", title: "잠깐 쉬어갈까요?",
            text: "관측된 작업 부하와 사용 시간을 바탕으로 휴식을 권합니다." },
    };
    const message = messages[event.level];
    if (!message) return null;
    const reasons = {
        FUSION_CAUTION: "상태 지표의 변화가 관측되었습니다.",
        FUSION_DANGER: "상태 지표의 변화가 지속되고 있습니다.",
        LOW_SCORE: "관측된 작업 부하가 누적되었습니다.",
        SUSTAINED_DANGER: "높은 작업 부하 상태가 지속되고 있습니다.",
        CONTINUOUS_WORK: "연속 작업 시간이 길어지고 있습니다.",
    };
    return { ...message, text: reasons[event.reason] || message.text,
        persistent: event.level === "BREAK" };
}
