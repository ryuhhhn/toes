/* toes customer chatbot embed.
   Usage: <script src="/path/to/toes-widget.js" data-chatbot-url="/path/to/chatbot.html"></script> */
(function () {
  const script = document.currentScript;
  const url =
    script?.dataset.chatbotUrl ||
    new URL("chatbot.html", script?.src || location.href).href;
  const frame = document.createElement("iframe");
  frame.title = "toes shopping assistant";
  frame.src = url;
  frame.style.cssText =
    "position:fixed;right:24px;bottom:24px;width:390px;height:720px;border:0;border-radius:18px;z-index:2147483647;box-shadow:0 18px 60px rgba(40,43,59,.16);background:#fff;";
  frame.setAttribute("loading", "lazy");
  document.body.appendChild(frame);
  const media = window.matchMedia("(max-width:500px)");
  const resize = () => {
    frame.style.right = media.matches ? "0" : "24px";
    frame.style.bottom = media.matches ? "0" : "24px";
    frame.style.width = media.matches ? "100vw" : "390px";
    frame.style.height = media.matches ? "100vh" : "720px";
    frame.style.borderRadius = media.matches ? "0" : "18px";
  };
  media.addEventListener?.("change", resize);
  resize();
})();
