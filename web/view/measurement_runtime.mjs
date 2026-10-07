// Small effect adapter around the pure lifecycle. No Supabase/CDN import: injectable in tests.
import {
    initialLifecycle, reduceLifecycle, canRenderState, feedbackPresentation,
} from "./measurement_phase.mjs";

export class WorkflowError extends Error {
    constructor(kind, message) {
        super(message);
        this.name = "WorkflowError";
        this.kind = kind;
    }
}

function authError(error) {
    const network = error instanceof TypeError || error?.name === "AuthRetryableFetchError";
    return new WorkflowError(network ? "network" : "auth", network ?
        "인증 서비스에 연결할 수 없습니다. 네트워크를 확인해주세요." :
        "인증을 확인할 수 없습니다. 다시 로그인해주세요.");
}

export function createAuthenticatedFetch(auth, fetchImpl = globalThis.fetch) {
    return async (url, options = {}, expectedUserId = null) => {
        let result;
        try { result = await auth.getSession(); } catch (error) { throw authError(error); }
        if (result.error) throw authError(result.error);
        let session = result.data?.session;
        const userId = expectedUserId || session?.user?.id;
        for (let attempt = 0; attempt < 2; attempt++) {
            if (!session?.access_token || !userId || session.user?.id !== userId) {
                throw new WorkflowError("auth", "로그인이 필요합니다. 다시 로그인해주세요.");
            }
            const headers = new Headers(options.headers);
            headers.set("Authorization", `Bearer ${session.access_token}`);
            let response;
            try { response = await fetchImpl(url, { ...options, headers }); } catch {
                throw new WorkflowError("network", "서버에 연결할 수 없습니다. 네트워크를 확인해주세요.");
            }
            if (response.status === 401 && attempt === 0) {
                try { result = await auth.refreshSession(); } catch (error) { throw authError(error); }
                if (result.error) throw authError(result.error);
                session = result.data?.session;
                continue;
            }
            if (response.status === 401) {
                throw new WorkflowError("auth", "인증이 만료되었습니다. 다시 로그인해주세요.");
            }
            if (!response.ok) {
                let code;
                try { code = (await response.json()).error?.code; } catch { /* Safe generic error. */ }
                if (response.status === 409 && code === "measurement_in_use") {
                    throw new WorkflowError("conflict", "다른 사용자의 측정이 진행 중입니다.");
                }
                throw new WorkflowError("backend", response.status === 409 ?
                    "활성 측정 세션을 확인할 수 없습니다. 연결 상태를 확인해주세요." :
                    "Backend 요청을 완료하지 못했습니다. 잠시 후 다시 시도해주세요.");
            }
            return response;
        }
    };
}

export function createMeasurementRuntime({
    auth, authenticatedFetch, socketFactory, backendUrl,
    onChange = () => {}, onState = () => {}, onFeedback = () => {}, onSession = () => {},
}) {
    let model = initialLifecycle();
    let session = null;
    let socket = null;
    let userGeneration = 0;
    let authRevision = 0;
    let queue = Promise.resolve();
    let stopPromise = null;
    let handlers = [];
    const dispatch = action => {
        const next = reduceLifecycle(model, action);
        if (next !== model) { model = next; onChange(model); }
    };
    const fail = (error, request, generation = model.connectionGeneration) => dispatch({
        type: "ERROR", generation, request,
        error: error instanceof WorkflowError ? { kind: error.kind, message: error.message } :
            { kind: "backend", message: "응답을 처리할 수 없습니다. 다시 시도해주세요." },
    });
    function resetConnection(newUser = false) {
        dispatch({ type: "CONNECTION_RESET", generation: model.connectionGeneration + 1, newUser });
    }
    function unbindEvents(target) {
        for (const [name, handler] of handlers) target.off(name, handler);
        handlers = [];
    }
    function bindEvents(target, owner) {
        const generation = model.connectionGeneration;
        let feedbackSessionId = null;
        let lastFeedbackTime = null;
        const valid = () => target === socket && owner === userGeneration;
        const bind = (name, handler) => {
            target.on(name, handler);
            handlers.push([name, handler]);
        };
        // Initial server phase may be delivered during the connect handshake.
        bind("measurement_phase", event => {
            if (valid()) dispatch({ type: "PHASE", generation, event });
        });
        bind("state", event => {
            // No session_id in public state: lifecycle/generation gates are mitigation only.
            if (valid() && canRenderState(model, generation) && event?.v === 1 &&
                ["NORMAL", "CAUTION", "DANGER", "ABSENT"].includes(event.state)) onState(event);
        });
        bind("feedback", event => {
            if (!valid()) return;
            const presentation = feedbackPresentation(model, event, generation);
            if (!presentation) return;
            if (feedbackSessionId !== model.sessionId) {
                feedbackSessionId = model.sessionId;
                lastFeedbackTime = null;
            }
            // Old/duplicate transitions must not dismiss or replace a newer toast.
            if (lastFeedbackTime !== null && event.t <= lastFeedbackTime) return;
            lastFeedbackTime = event.t;
            onFeedback(presentation);
        });
    }
    function setSession(nextSession) {
        const userId = nextSession?.user?.id || null;
        if (userId && userId === session?.user?.id && socket) {
            session = nextSession;
            // Only future handshakes change; a healthy measurement lease stays connected.
            socket.auth = { token: session.access_token };
            onSession(session);
            // A server-denied handshake does not auto-reconnect. Retry with the new token,
            // but leave a healthy socket or ongoing transport reconnection alone.
            if (!socket.connected && !socket.active) socket.connect();
            return;
        }
        userGeneration++;
        const previous = socket;
        socket = null;
        if (previous) { unbindEvents(previous); previous.disconnect(); }
        session = userId ? nextSession : null;
        queue = Promise.resolve();
        stopPromise = null;
        resetConnection(true);
        onSession(session);
        if (!session?.access_token) return;
        const owner = userGeneration;
        const target = socketFactory(backendUrl, {
            autoConnect: false, forceNew: true, auth: { token: session.access_token },
        });
        socket = target;
        bindEvents(target, owner);
        target.on("connect", () => {
            if (target !== socket || owner !== userGeneration) return;
            dispatch({ type: "CONNECTED", generation: model.connectionGeneration });
        });
        target.on("disconnect", () => {
            if (target !== socket || owner !== userGeneration) return;
            resetConnection();
            unbindEvents(target);
            bindEvents(target, owner); // New captured generation before automatic reconnection.
        });
        target.on("connect_error", () => {
            if (target !== socket || owner !== userGeneration) return;
            fail(new WorkflowError("connection", "인증된 서버 연결에 실패했습니다. 로그인과 연결 상태를 확인해주세요."));
        });
        target.connect();
    }
    const scope = () => ({
        owner: userGeneration, userId: session?.user?.id,
        generation: model.connectionGeneration, request: model.requestGeneration,
    });
    const current = item => item.owner === userGeneration &&
        item.generation === model.connectionGeneration && item.userId === session?.user?.id;
    function enqueue(action, item) {
        const task = async () => {
            if (!current(item)) return false;
            try {
                const response = await authenticatedFetch(
                    `${backendUrl}/api/measurement/${action}`, { method: "POST" }, item.userId,
                );
                const body = await response.json();
                const measurement = body.measurement;
                if (body.status !== "success" || measurement?.user_id !== item.userId ||
                    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(measurement?.session_id || "") ||
                    measurement.status !== (action === "start" ? "ACTIVE" : "STOPPED")) {
                    throw new WorkflowError("backend", "측정 응답 형식이 올바르지 않습니다.");
                }
                if (!current(item)) return false;
                dispatch({ type: action === "start" ? "START_ACK" : "STOP_ACK",
                    generation: item.generation, request: item.request,
                    sessionId: measurement.session_id });
                return true;
            } catch (error) {
                if (current(item)) fail(error, item.request, item.generation);
                return false;
            }
        };
        const result = queue.then(task);
        queue = result.then(() => {});
        return result;
    }
    function start() {
        if (!session || !model.connected || model.pendingAction || model.sessionId ||
            model.stopRequested) return Promise.resolve(false);
        dispatch({ type: "START", generation: model.connectionGeneration });
        return enqueue("start", scope());
    }
    function stop() {
        if (stopPromise) return stopPromise;
        if (!session) return Promise.resolve(false);
        dispatch({ type: "STOP", generation: model.connectionGeneration });
        // Never cancel/forget a POST start: it may already have created a Backend session.
        const result = enqueue("stop", scope());
        stopPromise = result;
        result.finally(() => { if (stopPromise === result) stopPromise = null; });
        return result;
    }
    async function retry() {
        if (model.phase !== "CALIBRATING" || model.calibration?.status !== "FAILED") return false;
        const owner = userGeneration;
        if (!await stop() || owner !== userGeneration) return false;
        return start();
    }
    async function restore() {
        const revision = authRevision;
        try {
            const { data, error } = await auth.getSession();
            if (revision !== authRevision) return;
            if (error) throw authError(error);
            setSession(data.session);
        } catch (error) {
            if (revision === authRevision) fail(error instanceof WorkflowError ? error : authError(error));
        }
    }
    return {
        get model() { return model; }, start, stop, retry, restore,
        canRender: () => canRenderState(model),
        hasSession: () => !!session,
        authChanged(_event, nextSession) {
            authRevision++;
            setSession(nextSession); // Synchronous: no Supabase calls under the Auth callback lock.
        },
        destroy() { authRevision++; setSession(null); },
    };
}
