// Supabase 인증 공용 모듈.
// index.html 과 signup.html 이 같은 클라이언트를 쓰도록 여기 한 곳에서 만듭니다.
// ES module 이라 file:// 로 열면 import 가 막힙니다. http 서버로 띄우세요.
import { createClient } from "https://cdn.jsdelivr.net/npm/@supabase/supabase-js/+esm";

const SUPABASE_URL = "https://dzkionspeweesqwlrxex.supabase.co";
const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImR6a2lvbnNwZXdlZXNxd2xyeGV4Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkzNzkzNjAsImV4cCI6MjA5NDk1NTM2MH0.VoYhfIg7h1PGpm72NYPTe3sXGqk0pt54k_UCsNcQxVI";

const PASSWORD_MIN_LENGTH = 8;

export const MAIN_URL = "./index.html";
export const LOGIN_URL = "./login.html";
export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// 이미 로그인돼 있으면 가입 화면을 보여줄 이유가 없습니다
export async function redirectIfAuthenticated() {
    const { data, error } = await supabase.auth.getSession();
    if (error) throw error;
    if (data.session) window.location.replace(MAIN_URL);
}

// 검증 메시지는 signup.html 이 문구로 분기하므로 바꿀 때 같이 고치세요
export async function signup(email, password, confirmation) {
    email = (email || "").trim();
    if (!email) throw new Error("이메일을 입력해주세요.");
    if (!password) throw new Error("비밀번호를 입력해주세요.");
    if (password.length < PASSWORD_MIN_LENGTH) {
        throw new Error(`비밀번호는 ${PASSWORD_MIN_LENGTH}자 이상이어야 합니다.`);
    }
    if (password !== confirmation) throw new Error("비밀번호가 일치하지 않습니다.");

    const { data, error } = await supabase.auth.signUp({ email, password });
    if (error) throw error;

    // 이메일 인증이 꺼져 있으면 가입과 동시에 세션이 나옵니다
    if (data.session) {
        localStorage.setItem("access_token", data.session.access_token);
        return { confirmationRequired: false };
    }
    return { confirmationRequired: true };
}

export async function login(email, password) {
    email = (email || "").trim();
    if (!email) throw new Error("이메일을 입력해주세요.");
    if (!password) throw new Error("비밀번호를 입력해주세요.");

    const { data, error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) throw error;

    localStorage.setItem("access_token", data.session.access_token);
    return data.session;
}

// 이메일이 없는 것과 비밀번호가 틀린 것을 Supabase 가 구분하지 않습니다 (계정 존재 여부 노출 방지)
export function isInvalidCredentials(error) {
    return error?.code === "invalid_credentials" ||
        /invalid login credentials/i.test(error?.message || "");
}

export function isEmailNotConfirmed(error) {
    return error?.code === "email_not_confirmed" ||
        /email not confirmed/i.test(error?.message || "");
}

export function isNetworkError(error) {
    if (!error) return false;
    if (error instanceof TypeError) return true;               // fetch 자체 실패
    if (error.name === "AuthRetryableFetchError") return true; // supabase-js 의 네트워크 오류
    return /fetch|network/i.test(error.message || "");
}
