// Insta এক্সটেনশন — ব্যাকগ্রাউন্ড (service worker)
// তৈরি করেছেন MH & MIK
const API = "http://127.0.0.1:8765";
const OFFLINE = "Insta অ্যাপ চালু নেই। আগে Insta.exe চালু করুন।";

async function call(path, options = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 6000);
  try {
    const r = await fetch(API + path, { ...options, signal: ctrl.signal });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: j.error || "অ্যাপ থেকে সমস্যা এসেছে।" };
    return j;
  } catch (e) {
    return { ok: false, offline: true, error: OFFLINE };
  } finally {
    clearTimeout(timer);
  }
}

async function ping() {
  // POST ব্যবহার হয় কারণ Chrome শুধু POST-এ Origin হেডার পাঠায়, আর অ্যাপ Origin দেখেই এক্সটেনশন চেনে
  const j = await call("/ext/ping", { method: "POST" });
  return j && j.ok ? { ok: true, version: j.version } : { ok: false, error: j.error || OFFLINE };
}

// লিংক পাঠায়; সেটিংস চালু থাকলে লগইন কুকিও (প্রাইভেট/লগইন-লাগা ভিডিওর জন্য)
async function send(url, title) {
  if (!/^https?:\/\//i.test(url || "")) return { ok: false, error: "এটি ভিডিওর লিংক নয়।" };
  const { sendCookies } = await chrome.storage.local.get({ sendCookies: false });
  let cookies = [];
  if (sendCookies) {
    try {
      cookies = (await chrome.cookies.getAll({ url })).map(c => ({
        name: c.name, value: c.value, domain: c.domain, path: c.path,
        secure: c.secure, httpOnly: c.httpOnly, expirationDate: c.expirationDate
      }));
    } catch (e) { /* এই সাইটের জন্য অনুমতি নেই, কুকি ছাড়াই চলবে */ }
  }
  return call("/ext/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, title: (title || "").slice(0, 150), cookies })
  });
}

function badge(ok) {
  chrome.action.setBadgeBackgroundColor({ color: ok ? "#0B5D45" : "#D9363E" });
  chrome.action.setBadgeText({ text: ok ? "✓" : "!" });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 3000);
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg && msg.type === "send") { send(msg.url, msg.title).then(reply); return true; }
  if (msg && msg.type === "ping") { ping().then(reply); return true; }
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "insta-send",
    title: "Insta-তে ডাউনলোড করুন",
    contexts: ["link", "page", "video"]
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "insta-send") return;
  const url = info.linkUrl || info.pageUrl;
  const title = info.linkUrl ? "" : (tab && tab.title) || "";
  const r = await send(url, title);
  badge(!!(r && r.ok));
});
