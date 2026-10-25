const AUTH_ERROR_MESSAGES = {
    otp_expired: "인증 링크가 만료되었습니다. 새 인증 메일을 요청하거나 다시 회원가입해 주세요.",
    access_denied: "인증 링크를 사용할 수 없습니다. 링크가 만료되었거나 이미 사용되었는지 확인해 주세요.",
};

function valuesFromLocation(locationLike) {
    const query = new URLSearchParams(locationLike?.search || "");
    const hash = new URLSearchParams((locationLike?.hash || "").replace(/^#/, ""));
    const value = key => query.get(key) || hash.get(key);
    return { query, hash, value };
}

export function authRedirectNotice(locationLike = globalThis.location) {
    const { value } = valuesFromLocation(locationLike);
    const errorCode = value("error_code") || value("error");
    if (errorCode) {
        return {
            kind: "error",
            message: AUTH_ERROR_MESSAGES[errorCode] ||
                "이메일 인증을 완료할 수 없습니다. 인증 링크를 다시 확인해 주세요.",
        };
    }
    if (value("code") || value("access_token") || value("type") === "signup") {
        return { kind: "success", message: "이메일 인증이 완료되었습니다." };
    }
    return null;
}

export function clearAuthRedirectUrl(locationLike = globalThis.location, historyLike = globalThis.history) {
    if (!locationLike || !historyLike?.replaceState) return;
    const url = new URL(locationLike.href);
    for (const key of ["error", "error_code", "error_description", "code", "type"]) {
        url.searchParams.delete(key);
    }
    url.hash = "";
    historyLike.replaceState({}, "", `${url.pathname}${url.search}`);
}

export function confirmationRedirectUrl(locationLike = globalThis.location) {
    if (!locationLike?.href) throw new Error("confirmation redirect URL을 만들 수 없습니다.");
    return new URL("./login.html", locationLike.href).href;
}
