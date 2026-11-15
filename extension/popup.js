const $ = id => document.getElementById(id);

chrome.runtime.sendMessage({ type: "ping" }, r => {
  const on = r && r.ok;
  $("dot").className = "dot " + (on ? "on" : "off");
  $("st").textContent = on ? "Insta অ্যাপ চালু আছে" : "Insta অ্যাপ চালু নেই";
  $("send").disabled = !on;
  if (!on) $("msg").textContent = "আগে Insta.exe চালু করুন।";
});

chrome.storage.local.get({ sendCookies: false }, v => { $("ck").checked = v.sendCookies; });
$("ck").addEventListener("change", () => chrome.storage.local.set({ sendCookies: $("ck").checked }));

$("send").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  $("send").disabled = true; $("msg").textContent = "পাঠানো হচ্ছে…";
  chrome.runtime.sendMessage({ type: "send", url: tab && tab.url, title: (tab && tab.title) || "" }, r => {
    $("send").disabled = false;
    $("msg").textContent = r && r.ok ? "✓ পাঠানো হয়েছে, ডাউনলোড শুরু।" : ((r && r.error) || "পাঠানো যায়নি।");
    $("msg").style.color = r && r.ok ? "#1f8f4e" : "#D9363E";
  });
});
