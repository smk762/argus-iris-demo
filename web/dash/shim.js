// Loaded before the dashboard's own app.js, which is served exactly as published. It gives the page recorded answers
// instead of a broker: the clock, the API reads and the event stream come from the replay in the parent window.
(() => {
  const replay = window.parent !== window && window.parent.irisReplay;
  if (!replay) {
    location.replace("../../");
    return;
  }
  Date.now = () => replay.now();

  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  window.fetch = async (input, init = {}) => {
    const path = new URL(input.url ?? input, location.href).pathname.split("/dash/")[1] ?? "";
    if ((init.method ?? "GET") !== "GET") return json({ detail: "This is a recording: there is no broker to act on." }, 403);
    const body = replay.http(path);
    return body === undefined ? json({ detail: "not in the recording" }, 404) : json(body);
  };

  window.EventSource = class {
    static CLOSED = 2;
    readyState = 1;
    #on = {};
    constructor() {
      setTimeout(() => replay.attach(this));
    }
    addEventListener(name, fn) {
      (this.#on[name] ??= []).push(fn);
    }
    close() {}
    emit(name, data) {
      for (const fn of this.#on[name] ?? []) fn({ data: data && JSON.stringify(data) });
      // The dashboard says "live" when its stream opens. Here it is not.
      if (name === "open") document.querySelector("#live").lastElementChild.textContent = "recorded";
    }
  };

  // Evict and Warm answer "this is a recording" at once instead of first asking for a token.
  try { sessionStorage.setItem("iris-broker-token", "recording"); } catch { /* storage blocked: it will ask */ }
  addEventListener("load", () => {
    for (const el of document.querySelectorAll(".snippet code")) el.textContent = el.textContent.replace(/https?:\/\/[^\s]+\/dash/g, "http://<broker>:8120");
    for (const a of document.querySelectorAll('a[href^="http"]')) a.target = "_top";
  });
})();
