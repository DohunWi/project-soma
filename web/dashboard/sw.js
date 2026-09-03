// Service Worker — 백그라운드 알림용.
//
// 탭이 백그라운드여도 알림이 뜨게 합니다.
// 다만 브라우저 프로세스 자체가 살아 있어야 합니다.
// 브라우저를 완전히 종료하면 알림은 뜨지 않습니다 — 웹 대시보드의 구조적 한계입니다.

self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

// 알림을 클릭하면 대시보드 탭으로 이동합니다
self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if ("focus" in c) return c.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow("./index.html");
    })
  );
});
