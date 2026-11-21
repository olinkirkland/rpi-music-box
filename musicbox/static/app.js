import { createApp } from "./vendor/vue-3.5.13.esm-browser.prod.js";

const RESULT_LABELS = {
  played: "played",
  unknown: "unknown card",
  missing: "file missing",
  blocked: "not played (safety check)",
};
const NOW_LABELS = {
  idle: "Idle",
  playing: "Playing",
  finished: "Finished",
  waiting: "Card on reader",
  unknown: "Unknown card",
  missing: "File missing",
  blocked: "Not played",
};
const MAX_TAPS = 500;
const THEME_KEY = "musicbox-theme";

function savedTheme() {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return t === "light" || t === "dark" ? t : null;
  } catch {
    return null;
  }
}
const dateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });

async function api(method, path, body) {
  const opts = { method, cache: "no-store", headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
  return data;
}

createApp({
  data() {
    return {
      now: { state: "idle" },
      clockOffset: 0, // server clock minus this device's clock, in seconds
      tick: Date.now() / 1000,
      cards: [],
      tracks: [],
      taps: [],
      freshTap: null,
      form: { uid: "", track: "" },
      connection: "connecting",
      theme: document.documentElement.getAttribute("data-theme") || "light",
      toast: { text: "", kind: "", show: false },
    };
  },

  computed: {
    connectionLabel() {
      return { live: "Live", connecting: "Connecting…", offline: "Reconnecting…" }[this.connection];
    },
    nowLabel() {
      return NOW_LABELS[this.now.state] || this.now.state;
    },
    serverNow() {
      return this.tick + this.clockOffset;
    },
    inChime() {
      return this.now.state === "playing" && this.serverNow < this.now.song_starts_at;
    },
    elapsed() {
      if (this.now.state !== "playing") return 0;
      const e = Math.max(0, this.serverNow - this.now.song_starts_at);
      return this.now.duration ? Math.min(e, this.now.duration) : e;
    },
    cardUids() {
      return new Set(this.cards.map((c) => c.uid));
    },
  },

  methods: {
    clock(seconds) {
      const s = Math.floor(seconds || 0);
      return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
    },
    dateTime(t) {
      return dateFmt.format(new Date(t * 1000));
    },
    resultLabel(r) {
      return RESULT_LABELS[r] || r;
    },
    cardName(uid) {
      const c = this.cards.find((c) => c.uid === uid);
      return c ? c.track.replace(/\.[^./]+$/, "") : "";
    },

    applyTheme(theme) {
      this.theme = theme;
      document.documentElement.setAttribute("data-theme", theme);
    },
    toggleTheme() {
      const next = this.theme === "dark" ? "light" : "dark";
      this.applyTheme(next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch {
        // Private mode or storage blocked: the toggle still works for this visit.
      }
    },

    notify(text, kind = "") {
      clearTimeout(this._toastTimer);
      this.toast = { text, kind, show: true };
      this._toastTimer = setTimeout(() => (this.toast.show = false), 2500);
    },

    async loadCards() {
      try {
        [this.tracks, this.cards] = await Promise.all([api("GET", "/api/tracks"), api("GET", "/api/cards")]);
      } catch (e) {
        this.notify("Couldn't load cards: " + e.message, "error");
      }
    },
    async loadTaps() {
      try {
        this.taps = await api("GET", "/api/taps?limit=" + MAX_TAPS);
      } catch (e) {
        this.notify("Couldn't load the tap log: " + e.message, "error");
      }
    },

    async assign(uid, track) {
      try {
        await api("PUT", "/api/cards/" + encodeURIComponent(uid), { track });
        this.notify("Card " + uid + " → " + track);
      } catch (e) {
        this.notify(e.message, "error");
      }
      this.loadCards();
    },
    async addCard() {
      if (!this.form.track) return this.notify("Choose a song first", "error");
      await this.assign(this.form.uid.toLowerCase(), this.form.track);
      this.form = { uid: "", track: "" };
    },
    async removeCard(uid) {
      if (!confirm("Remove card " + uid + "? It will stop playing anything.")) return;
      try {
        await api("DELETE", "/api/cards/" + encodeURIComponent(uid));
        this.notify("Removed card " + uid);
      } catch (e) {
        this.notify(e.message, "error");
      }
      this.loadCards();
    },
    prefill(uid) {
      this.form.uid = uid;
      const input = this.$refs.uidInput;
      input.scrollIntoView({ behavior: "smooth", block: "center" });
      input.focus({ preventScroll: true });
    },

    setNow(data) {
      this.clockOffset = data.server_time - Date.now() / 1000;
      this.now = data;
    },
    addTap(row) {
      if (this.taps.some((t) => t.id === row.id)) return;
      this.taps = [row, ...this.taps].slice(0, MAX_TAPS);
      this.freshTap = row.id;
    },

    connect() {
      const source = new EventSource("/api/events");
      source.onopen = () => {
        this.connection = "live";
        // Catch up on anything missed while disconnected.
        this.loadCards();
        this.loadTaps();
      };
      source.onerror = () => {
        // EventSource reconnects by itself.
        this.connection = "offline";
      };
      source.addEventListener("now", (e) => this.setNow(JSON.parse(e.data)));
      source.addEventListener("tap", (e) => this.addTap(JSON.parse(e.data)));
      source.addEventListener("cards", () => this.loadCards());
    },
  },

  mounted() {
    // Follow the device's light/dark setting until a choice is saved here.
    const media = window.matchMedia && matchMedia("(prefers-color-scheme: dark)");
    media?.addEventListener?.("change", (e) => {
      if (!savedTheme()) this.applyTheme(e.matches ? "dark" : "light");
    });
    this.connect();
    setInterval(() => (this.tick = Date.now() / 1000), 250);
  },
}).mount("#app");
