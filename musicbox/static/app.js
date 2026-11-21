import { createApp } from "./vendor/vue-3.5.13.esm-browser.prod.js";
import AppLogo from "./components/app-logo.js";

const RESULT_LABELS = {
  played: "played",
  unknown: "unknown card",
  missing: "file missing",
  blocked: "not played (safety check)",
  assigning: "read for assigning",
};
const NOW_LABELS = {
  idle: "Idle",
  playing: "Playing",
  paused: "Paused",
  finished: "Finished",
  waiting: "Card on reader",
  unknown: "Unknown card",
  missing: "File missing",
  blocked: "Not played",
  assigning: "Assigning",
};
const LATEST_TAPS = 10; // shown on the page; "View more" has them all
const THEME_KEY = "musicbox-theme";
const UID_RE = /^[0-9a-f]{4,32}$/;
const ASSIGN_RENEW_MS = 5000; // the box's assigning mode lasts 20 s unless renewed

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

function sameList(a, b) {
  return !!a && !!b && a.length === b.length && a.every((x, i) => x === b[i]);
}

function matches(query, ...fields) {
  const q = query.trim().toLowerCase();
  return !q || fields.some((f) => f && String(f).toLowerCase().includes(q));
}

// Pick several tracks and put them in order. v-model is an array of paths.
const TrackPicker = {
  props: { items: { type: Array, default: () => [] }, modelValue: { type: Array, default: () => [] } },
  emits: ["update:modelValue"],
  data: () => ({ query: "" }),
  computed: {
    filtered() {
      return this.items.filter((i) => matches(this.query, i.name, i.artist, i.album, i.path));
    },
    chosen() {
      // The selected tracks, in order, with their details (or a placeholder if a file is gone).
      return this.modelValue.map((path) => this.items.find((i) => i.path === path)
        || { path, name: path.split("/").pop(), artist: null, missing: true });
    },
  },
  methods: {
    meta(item) {
      return [item.artist, item.album].filter(Boolean).join(" · ") || item.path;
    },
    isChosen(item) {
      return this.modelValue.includes(item.path);
    },
    toggle(item) {
      const list = this.modelValue.filter((p) => p !== item.path);
      this.$emit("update:modelValue", this.isChosen(item) ? list : [...this.modelValue, item.path]);
    },
    move(index, step) {
      const list = [...this.modelValue];
      const to = index + step;
      if (to < 0 || to >= list.length) return;
      [list[index], list[to]] = [list[to], list[index]];
      this.$emit("update:modelValue", list);
    },
    removeAt(index) {
      this.$emit("update:modelValue", this.modelValue.filter((_, i) => i !== index));
    },
  },
  template: `
    <div class="picker">
      <div class="picked" aria-label="Selected tracks">
        <p class="picked-head">
          <strong>Selected:</strong>
          <span v-if="!modelValue.length">Pick from available tracks.</span>
          <span v-else-if="modelValue.length === 1">1 track.</span>
          <span v-else>{{ modelValue.length }} tracks, played in this order.</span>
        </p>
        <p v-if="!modelValue.length" class="picked-empty">Nothing selected yet.</p>
        <ol v-else class="picked-list">
          <li v-for="(item, i) in chosen" :key="item.path" :class="{ missing: item.missing }" :title="item.path">
            <span class="pd-num">{{ i + 1 }}</span>
            <span class="pk-text">
              <span class="pk-name">{{ item.name }}</span>
              <span class="pk-meta">{{ item.missing ? "File missing" : meta(item) }}</span>
            </span>
            <span class="pd-tools">
              <button type="button" class="icon-btn" @click="move(i, -1)" :disabled="i === 0"
                      :aria-label="'Move ' + item.name + ' up'" title="Move up">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 15l6-6 6 6"></path></svg>
              </button>
              <button type="button" class="icon-btn" @click="move(i, 1)" :disabled="i === chosen.length - 1"
                      :aria-label="'Move ' + item.name + ' down'" title="Move down">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"></path></svg>
              </button>
              <button type="button" class="icon-btn" @click="removeAt(i)"
                      :aria-label="'Remove ' + item.name" title="Remove">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>
              </button>
            </span>
          </li>
        </ol>
      </div>
      <div class="available">
      <input type="search" v-model="query" placeholder="Search tracks" aria-label="Search tracks">
      <ul class="picker-list" role="listbox" aria-multiselectable="true" aria-label="Available tracks">
        <li v-for="item in filtered" :key="item.path" role="option" tabindex="0"
            :aria-selected="isChosen(item)" :class="{ selected: isChosen(item) }"
            :title="item.path" @click="toggle(item)" @keydown.enter.prevent="toggle(item)" @keydown.space.prevent="toggle(item)">
          <span class="pk-box" aria-hidden="true">
            <svg v-if="isChosen(item)" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"
                 stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"></path></svg>
          </span>
          <span class="pk-text">
            <span class="pk-name">{{ item.name }}</span>
            <span class="pk-meta">{{ meta(item) }}</span>
          </span>
          <span v-if="isChosen(item)" class="pk-order">#{{ modelValue.indexOf(item.path) + 1 }}</span>
        </li>
        <li v-if="!filtered.length" class="pk-empty">{{ items.length ? "No matches" : "No tracks found in the music folder" }}</li>
      </ul>
      </div>
    </div>`,
};

function clockText(seconds) {
  const s = Math.floor(seconds || 0);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

// Transport buttons, seek slider and Shuffle/Repeat for the playing card.
const PlaybackControls = {
  props: {
    now: { type: Object, required: true },
    position: { type: Number, default: 0 },
    inChime: Boolean,
    disabled: Boolean,
    hint: { type: String, default: "" },
    large: Boolean,
  },
  emits: ["toggle-pause", "previous", "next", "shuffle", "repeat", "draft", "seek"],
  computed: {
    multi() {
      return this.now.parts > 1;
    },
    paused() {
      return this.now.state === "paused";
    },
  },
  methods: { clock: clockText },
  template: `
    <div class="controls" :class="{ large }">
      <div class="scrub">
        <input v-if="now.duration" type="range" min="0" :max="Math.floor(now.duration)" step="1"
               :value="Math.floor(position)" :disabled="disabled"
               @input="$emit('draft', Number($event.target.value))"
               @change="$emit('seek', Number($event.target.value))"
               aria-label="Position in the track">
        <progress v-else></progress>
        <p class="now-time">
          <span>{{ inChime ? "Chime…" : clock(position) }}</span>
          <span v-if="now.duration">{{ clock(now.duration) }}</span>
        </p>
      </div>
      <div class="transport">
        <button v-if="multi" type="button" class="mode-btn" :class="{ on: now.shuffle }"
                :aria-pressed="!!now.shuffle" :disabled="disabled" @click="$emit('shuffle', !now.shuffle)"
                :title="now.shuffle ? 'Shuffle is on' : 'Shuffle is off'" aria-label="Shuffle">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M16 3h5v5M4 20L21 3M21 16v5h-5M15 15l6 6M4 4l5 5"></path>
          </svg>
        </button>
        <button v-if="multi" type="button" class="skip-btn" :disabled="disabled"
                @click="$emit('previous')" aria-label="Previous track" title="Previous track (or the start of this one)">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5.5v13" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"></path><path d="M19 6.2v11.6a1 1 0 0 1-1.55.84L9.6 13.5a1.8 1.8 0 0 1 0-3l7.85-5.14A1 1 0 0 1 19 6.2z" fill="currentColor"></path></svg>
        </button>
        <button type="button" class="play-toggle" @click="$emit('toggle-pause')" :disabled="disabled"
                :aria-label="paused ? 'Resume' : 'Pause'" :title="hint || (paused ? 'Resume' : 'Pause')">
          <svg v-if="paused" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.4-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5z" fill="currentColor"></path>
          </svg>
          <svg v-else viewBox="0 0 24 24" aria-hidden="true">
            <rect x="6.5" y="5" width="4" height="14" rx="1" fill="currentColor"></rect>
            <rect x="13.5" y="5" width="4" height="14" rx="1" fill="currentColor"></rect>
          </svg>
        </button>
        <button v-if="multi" type="button" class="skip-btn" :disabled="disabled"
                @click="$emit('next')" aria-label="Next track" title="Next track">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 5.5v13" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"></path><path d="M5 6.2v11.6a1 1 0 0 0 1.55.84l7.85-5.14a1.8 1.8 0 0 0 0-3L6.55 5.36A1 1 0 0 0 5 6.2z" fill="currentColor"></path></svg>
        </button>
        <button type="button" class="mode-btn" :class="{ on: now.repeat }" :aria-pressed="!!now.repeat"
                :disabled="disabled" @click="$emit('repeat', !now.repeat)"
                :title="now.repeat ? 'Repeat is on' : 'Repeat is off'" aria-label="Repeat">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M17 2l4 4-4 4"></path><path d="M3 11v-1a4 4 0 0 1 4-4h14"></path><path d="M7 22l-4-4 4-4"></path><path d="M21 13v1a4 4 0 0 1-4 4H3"></path>
          </svg>
        </button>
      </div>
      <p v-if="hint && now.controls" class="controls-hint">{{ hint }}</p>
    </div>`,
};

// A card's saved settings, as checkboxes: Shuffle, Repeat, Remember playback.
const CardOptions = {
  props: {
    trackCount: { type: Number, default: 0 },
    shuffle: Boolean,
    repeat: Boolean,
    rememberPlayback: Boolean,
  },
  emits: ["update:shuffle", "update:repeat", "update:rememberPlayback"],
  computed: {
    options() {
      const one = this.trackCount < 2;
      return [
        { key: "shuffle", name: "Shuffle", on: this.shuffle && !one, disabled: one,
          text: one ? "Needs 2 or more tracks." : "Play the tracks in a random order." },
        { key: "repeat", name: "Repeat", on: this.repeat, disabled: false,
          text: "Start over when every track has played." },
        { key: "rememberPlayback", name: "Remember playback", on: this.rememberPlayback, disabled: false,
          text: "The next tap carries on where it left off." },
      ];
    },
  },
  // Each option is a card you click as a whole; the checkbox shows its state.
  template: `
    <div class="card-options">
      <label v-for="o in options" :key="o.key" class="option-card" :class="{ on: o.on, disabled: o.disabled }">
        <span class="oc-head">
          <input type="checkbox" :checked="o.on" :disabled="o.disabled"
                 @change="$emit('update:' + o.key, $event.target.checked)">
          <strong>{{ o.name }}</strong>
        </span>
        <span class="oc-text">{{ o.text }}</span>
      </label>
    </div>`,
};

// The volume button bar (tile and Play Mode).
const VolumeBar = {
  props: { volume: { type: Object, required: true }, busy: Boolean },
  emits: ["set"],
  data: () => ({ labelId: "volume-label-" + Math.random().toString(36).slice(2, 8) }),
  template: `
    <div v-if="volume.available" class="volume">
      <span class="volume-label" :id="labelId">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" fill="currentColor"></path>
          <path v-if="volume.level === 0" d="M16 9.5l5 5M21 9.5l-5 5"></path>
          <path v-if="volume.level > 0" d="M15.5 9a4 4 0 0 1 0 6"></path>
          <path v-if="volume.level > 40" d="M18.5 6.5a7.5 7.5 0 0 1 0 11"></path>
        </svg>
        Volume
      </span>
      <div role="group" :aria-labelledby="labelId" class="volume-steps">
        <button v-for="v in volume.steps" :key="v" type="button"
                :class="{ on: v === volume.level }" :aria-pressed="v === volume.level"
                :disabled="busy" @click="$emit('set', v)">{{ v }}%</button>
      </div>
    </div>`,
};

const app = createApp({
  data() {
    return {
      now: { state: "idle" },
      clockOffset: 0, // server clock minus this device's clock, in seconds
      tick: Date.now() / 1000,
      cards: [],
      libraryItems: [], // [{path, kind: "file" | "folder", name, artist, parts}]
      coverFailed: null, // the Now Playing cover path that had no art
      noCover: {}, // card grid: paths known to have no cover art
      cardQuery: "",
      taps: [],
      freshTap: null,
      seekDraft: null, // slider position while it's being dragged
      volume: { level: null, steps: [], available: false },
      volumeBusy: false,
      playMode: false,
      fm: { open: false, path: "", absolute: "", crumbs: [], folders: [], files: [], disk: null,
            loading: false, busy: false, error: "", renaming: null, renameTo: "", renameExt: "",
            uploads: [], dragging: false, dragDepth: 0, dropTop: 0, dropHeight: 0 },
      newFolder: { open: false, name: "", busy: false, error: "" },
      wifi: { mode: "unavailable", saved: [], log: [] },
      settings: { open: false, ssid: "", password: "", inRange: [], scanning: false, busy: false, error: "" },
      setup: { open: false, choice: "", ssid: "", password: "", inRange: [], busy: false, error: "",
               done: false, joined: "" },
      allTaps: { open: false, query: "", page: 1, pages: 1, total: 0, rows: [], busy: false, error: "" },
      controlBusy: false,
      edit: { open: false, orig: null, uid: "", name: "", nameBase: "", tracks: [], shuffle: false, repeat: false,
              remember: false, artPending: null,
              art_version: null, busy: false, error: "" },
      artBusy: false,
      artError: "",
      artSearch: { open: false, q: "", artist: "", busy: false, searched: false, results: [] },
      assign: { open: false, uid: "", tracks: [], shuffle: false, repeat: false, remember: false,
                busy: false, error: "" },
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
      if (this.now.state === "paused") return this.now.position || 0;
      if (this.now.state !== "playing") return 0;
      const e = Math.max(0, this.serverNow - this.now.song_starts_at);
      return this.now.duration ? Math.min(e, this.now.duration) : e;
    },
    shownPosition() {
      return this.seekDraft !== null ? this.seekDraft : this.elapsed;
    },
    controlHint() {
      if (!this.now.controls) return "Pause and seek aren't available";
      if (this.inChime) return "Wait for the chime to finish";
      return "";
    },
    playing() {
      return ["playing", "paused", "finished"].includes(this.now.state);
    },
    canControl() {
      return !this.controlBusy && !this.controlHint;
    },
    libraryCount() {
      const n = this.libraryItems.length;
      return n + (n === 1 ? " track" : " tracks");
    },
    coverUrl() {
      // The card's own artwork wins over the music files' cover.
      const card = this.cards.find((c) => c.uid === this.now.uid);
      if (card && card.art_version) return this.cardArtUrl(card);
      const c = this.now.cover;
      return c && c !== this.coverFailed ? this.coverFor(c) : "";
    },
    filteredCards() {
      return this.cards.filter((c) => matches(this.cardQuery, c.name, c.tag_name, c.uid, c.artist, ...c.tracks));
    },

    // Edit modal
    editIdError() {
      const uid = this.edit.uid.toLowerCase();
      if (!UID_RE.test(uid)) return "Use 4–32 hex characters (0–9, a–f), like 04a1b2c3.";
      if (uid !== this.edit.orig?.uid && this.cards.some((c) => c.uid === uid)) {
        return "Another card already has this ID.";
      }
      return "";
    },
    editName() {
      return this.edit.name.trim().replace(/\s+/g, " ");
    },
    editTagName() {
      // The tag name for whatever is picked now (it may differ from the card's).
      return this.edit.tracks.length ? this.selectionName(this.edit.tracks) : this.edit.orig?.tag_name || "";
    },
    setupSsid() {
      return (this.setup.choice === "__other" ? this.setup.ssid : this.setup.choice).trim();
    },
    setupErrors() {
      return (this.wifi.log || []).filter((e) => e.level === "error");
    },
    editNameTouched() {
      // Typed in: differs from what the field opened with (or was set to by the box).
      return this.editName !== this.edit.nameBase;
    },
    editNameDropped() {
      // Picking different tracks drops a custom name you didn't retype (see saveEdit).
      const o = this.edit.orig;
      return !!o && !!o.custom_name && !sameList(this.edit.tracks, o.tracks) && !this.editNameTouched;
    },
    editNameToSave() {
      // The custom name to store; "" means "use the name from the tags".
      const o = this.edit.orig;
      if (!this.editNameTouched) return sameList(this.edit.tracks, o.tracks) ? o.custom_name || "" : "";
      return this.editName === this.editTagName ? "" : this.editName;
    },
    editArtDropped() {
      const o = this.edit.orig;
      return !!o && !!this.edit.art_version && !this.edit.artPending && !sameList(this.edit.tracks, o.tracks);
    },
    editHasOwnArt() {
      const p = this.edit.artPending;
      return p ? p.kind !== "remove" : !!this.edit.art_version;
    },
    artPendingText() {
      const p = this.edit.artPending;
      if (!p) return "";
      return { upload: "the uploaded image", musicbrainz: "the cover from MusicBrainz",
               remove: "back to the music files' artwork" }[p.kind] + " is applied when you press Save.";
    },
    editArtKey() {
      return this.edit.art_version ? "card:" + this.edit.orig.uid + ":" + this.edit.art_version : this.edit.orig?.track;
    },
    editArtUrl() {
      const o = this.edit.orig;
      const p = this.edit.artPending;
      if (p && p.url) return p.url; // a preview of what Save will apply
      if (p && p.kind === "remove") return o && !this.noCover[o.track] ? this.coverFor(o.track) : "";
      if (!o || this.noCover[this.editArtKey]) return "";
      return this.edit.art_version ? this.cardArtUrl({ ...o, art_version: this.edit.art_version }) : this.coverFor(o.track);
    },
    editChanged() {
      const o = this.edit.orig;
      return !!o && (this.edit.uid.toLowerCase() !== o.uid || !sameList(this.edit.tracks, o.tracks)
        || this.edit.shuffle !== o.shuffle || this.edit.repeat !== o.repeat
        || this.edit.remember !== o.remember_playback || !!this.edit.artPending
        || this.editNameToSave !== (o.custom_name || ""));
    },

    // Assign modal
    assignExisting() {
      return this.cards.find((c) => c.uid === this.assign.uid) || null;
    },
    assignSame() {
      return !!this.assignExisting && sameList(this.assignExisting.tracks, this.assign.tracks);
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
      return c ? c.name : "";
    },
    trackName(path) {
      const item = this.libraryItems.find((i) => i.path === path);
      if (item) return item.name;
      const card = this.cards.find((c) => c.track === path);
      return card ? card.name : path;
    },
    selectionName(paths) {
      // Like the box names a card: one title, or the shared album, or "first + N more".
      const items = paths.map((p) => this.libraryItems.find((i) => i.path === p) || { name: p.split("/").pop() });
      if (items.length === 1) return items[0].name;
      const album = items[0].album;
      if (album && items.every((i) => i.album === album)) return album;
      return items[0].name + " + " + (items.length - 1) + " more";
    },
    coverFor(path) {
      return "/api/cover?path=" + encodeURIComponent(path);
    },
    cardArtUrl(card) {
      return card.art_version
        ? "/api/cards/" + encodeURIComponent(card.uid) + "/art?v=" + card.art_version
        : this.coverFor(card.track);
    },
    artKey(card) {
      // What noCover remembers: a card's own artwork, or its track's cover.
      return card.art_version ? "card:" + card.uid + ":" + card.art_version : card.track;
    },

    applyTheme(theme) {
      // Switch in one step: Pico's colour transitions would otherwise fade
      // buttons and fields while the rest of the page changes instantly.
      const root = document.documentElement;
      root.classList.add("theme-switching");
      this.theme = theme;
      root.setAttribute("data-theme", theme);
      void root.offsetHeight; // apply the new colours before re-enabling transitions
      requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove("theme-switching")));
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
      const el = this.$refs.toast;
      try {
        // Re-show so it lands on top of whatever opened since (a modal, full screen).
        if (el.matches(":popover-open")) el.hidePopover();
        el.showPopover();
      } catch {
        // No popover support: it still shows, just not above an open modal.
      }
      this._toastTimer = setTimeout(() => {
        this.toast.show = false;
        setTimeout(() => {
          try {
            if (!this.toast.show) el.hidePopover();
          } catch {
            // see above
          }
        }, 250); // after the fade
      }, 2500);
    },

    async copy(text) {
      let ok = false;
      // The clipboard API needs HTTPS; berry.local is plain HTTP, so fall
      // back to the older copy command, which works there.
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(text);
          ok = true;
        } catch {
          ok = false;
        }
      }
      if (!ok) {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.top = "0";
        ta.style.opacity = "0";
        // Inside an open modal (the rest of the page can't be selected then).
        const host = document.querySelector("dialog[open]") || this.$refs.playModeEl || document.body;
        host.appendChild(ta);
        ta.select();
        try {
          ok = document.execCommand("copy");
        } catch {
          ok = false;
        }
        ta.remove();
      }
      this.notify(ok ? "Copied!" : "Couldn't copy", ok ? "" : "error");
    },

    async loadCards() {
      try {
        [this.libraryItems, this.cards] = await Promise.all([api("GET", "/api/library"), api("GET", "/api/cards")]);
      } catch (e) {
        this.notify("Couldn't load cards: " + e.message, "error");
      }
    },
    async loadTaps() {
      try {
        this.taps = await api("GET", "/api/taps?limit=" + LATEST_TAPS);
      } catch (e) {
        this.notify("Couldn't load the tap log: " + e.message, "error");
      }
    },

    // --- File manager ----------------------------------------------------------
    bytes(n) {
      if (n == null) return "";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let i = 0;
      while (n >= 1024 && i < units.length - 1) {
        n /= 1024;
        i += 1;
      }
      return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + " " + units[i];
    },
    usedBy(uids) {
      return "Used by " + uids.length + (uids.length === 1 ? " card" : " cards");
    },
    usedByTitle(uids) {
      return uids.map((u) => this.cardName(u) || u).join(", ");
    },
    openFiles(path) {
      this.fm = { ...this.fm, open: true, error: "", uploads: [], renaming: null, dragging: false, dragDepth: 0 };
      this.$nextTick(() => this.$refs.filesDialog.showModal());
      this.openFolder(path);
    },
    closeFiles() {
      if (this.fm.uploads.some((u) => u.state === "uploading")) {
        if (!confirm("An upload is still running. Close anyway? It will be cancelled.")) return;
        this.fm.uploads.forEach((u) => u.xhr?.abort());
      }
      this.$refs.filesDialog.close();
      this.fm.open = false;
    },
    async openFolder(path) {
      this.fm.loading = true;
      this.fm.error = "";
      this.fm.renaming = null;
      try {
        const data = await api("GET", "/api/files?" + new URLSearchParams({ path }));
        Object.assign(this.fm, { path: data.path, absolute: data.absolute, crumbs: data.crumbs,
                                 folders: data.folders, files: data.files, disk: data.disk });
      } catch (e) {
        this.fm.error = e.message;
      } finally {
        this.fm.loading = false;
      }
    },
    async fmAction(fn) {
      this.fm.busy = true;
      this.fm.error = "";
      try {
        await fn();
        await this.openFolder(this.fm.path);
      } catch (e) {
        this.fm.error = e.message;
      } finally {
        this.fm.busy = false;
      }
    },
    openNewFolder() {
      this.newFolder = { open: true, name: "", busy: false, error: "" };
      this.$nextTick(() => {
        this.$refs.folderDialog.showModal();
        this.$refs.newFolderInput?.focus();
      });
    },
    closeNewFolder() {
      if (this.newFolder.busy) return;
      this.$refs.folderDialog.close();
      this.newFolder.open = false;
    },
    async makeFolder() {
      const name = this.newFolder.name.trim();
      if (!name) return;
      this.newFolder.busy = true;
      this.newFolder.error = "";
      try {
        await api("POST", "/api/files/folder", { path: this.fm.path, name });
        this.newFolder.busy = false;
        this.closeNewFolder();
        this.notify("Created folder " + name);
        await this.openFolder(this.fm.path);
      } catch (e) {
        this.newFolder.error = e.message;
        this.newFolder.busy = false;
      }
    },
    startRename(item) {
      // Files: edit the name only; the extension stays as it is.
      const dot = item.name.lastIndexOf(".");
      const isFile = this.fm.files.includes(item) && dot > 0;
      this.fm.renaming = item.path;
      this.fm.renameTo = isFile ? item.name.slice(0, dot) : item.name;
      this.fm.renameExt = isFile ? item.name.slice(dot) : "";
      // Ready to type: focus the name and select it (only one rename is open at a time).
      this.$nextTick(() => {
        const input = this.$refs.filesDialog?.querySelector(".fm-row .fm-inline input");
        input?.focus();
        input?.select();
      });
    },
    doRename(item) {
      const name = this.fm.renameTo.trim() + this.fm.renameExt;
      return this.fmAction(async () => {
        const res = await api("POST", "/api/files/rename", { path: item.path, name });
        this.fm.renaming = null;
        if (res.cards.length) this.notify("Renamed; updated " + res.cards.length + (res.cards.length === 1 ? " card" : " cards"));
      });
    },
    deleteItem(item, kind) {
      let question = "Delete " + item.name + "?";
      if (kind === "file" && item.used_by.length) {
        question += "\n\nIt's used by " + this.usedByTitle(item.used_by) + ". It will be taken out of "
          + (item.used_by.length === 1 ? "that card's" : "those cards'") + " track lists"
          + " (a card with no other tracks will show “file missing”).";
      }
      if (!confirm(question + "\n\nThis can't be undone.")) return;
      return this.fmAction(() => api("DELETE", "/api/files?" + new URLSearchParams({ path: item.path })));
    },
    pickUploads(event) {
      const files = [...event.target.files];
      event.target.value = "";
      this.uploadAll(files);
    },
    // Dragging files in. Enter/leave also fire for every row the pointer
    // crosses, so count them: the overlay stays until the drag truly leaves.
    isFileDrag(event) {
      return [...(event.dataTransfer?.types || [])].includes("Files");
    },
    placeDropOverlay() {
      // Cover just the part of the (scrollable) window that's on screen.
      const el = this.$refs.fmArticle;
      if (!el) return;
      this.fm.dropTop = el.scrollTop;
      this.fm.dropHeight = el.clientHeight;
    },
    dragEnter(event) {
      if (!this.isFileDrag(event)) return;
      this.fm.dragDepth += 1;
      this.placeDropOverlay();
      this.fm.dragging = true;
    },
    dragOver(event) {
      if (!this.isFileDrag(event)) return;
      event.dataTransfer.dropEffect = "copy";
      if (!this.fm.dragging) {
        this.placeDropOverlay();
        this.fm.dragging = true;
      }
    },
    dragLeave(event) {
      if (!this.isFileDrag(event)) return;
      this.fm.dragDepth = Math.max(0, this.fm.dragDepth - 1);
      if (this.fm.dragDepth === 0) this.fm.dragging = false;
    },
    dropFiles(event) {
      this.fm.dragging = false;
      this.fm.dragDepth = 0;
      this.uploadAll([...(event.dataTransfer?.files || [])]);
    },
    uploadLabel(u) {
      return { waiting: "Waiting", uploading: Math.floor((100 * u.done) / (u.size || 1)) + "%",
               done: "Uploaded", failed: u.error, skipped: "Skipped" }[u.state];
    },
    async uploadAll(files) {
      if (!files.length) return;
      const folder = this.fm.path;
      const items = files.map((file, i) => ({ id: Date.now() + "-" + i, file, name: file.name, size: file.size,
                                              done: 0, state: "waiting", error: "", xhr: null }));
      this.fm.uploads = [...items, ...this.fm.uploads.filter((u) => u.state === "uploading")];
      for (const u of items) {
        await this.uploadOne(u, folder, false);
      }
      await this.openFolder(this.fm.path);
    },
    uploadOne(u, folder, overwrite) {
      return new Promise((resolve) => {
        // XMLHttpRequest rather than fetch: it reports upload progress.
        const xhr = new XMLHttpRequest();
        u.xhr = xhr;
        u.state = "uploading";
        const query = new URLSearchParams({ path: folder, name: u.name });
        if (overwrite) query.set("overwrite", "1");
        xhr.open("PUT", "/api/files/upload?" + query);
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        xhr.upload.onprogress = (e) => (u.done = e.loaded);
        xhr.onload = async () => {
          let data = {};
          try {
            data = JSON.parse(xhr.responseText);
          } catch {
            data = {};
          }
          if (xhr.status === 409 && !overwrite) {
            if (confirm(u.name + " is already in this folder. Replace it?")) {
              resolve(await this.uploadOne(u, folder, true));
            } else {
              u.state = "skipped";
              resolve();
            }
            return;
          }
          if (xhr.status >= 200 && xhr.status < 300) {
            u.state = "done";
            u.done = u.size;
          } else {
            u.state = "failed";
            u.error = data.error || "HTTP " + xhr.status;
          }
          resolve();
        };
        xhr.onerror = () => {
          u.state = "failed";
          u.error = "the connection dropped";
          resolve();
        };
        xhr.onabort = () => {
          u.state = "failed";
          u.error = "cancelled";
          resolve();
        };
        xhr.send(u.file);
      });
    },

    // --- All taps ------------------------------------------------------------
    openAllTaps() {
      this.allTaps = { open: true, query: "", page: 1, pages: 1, total: 0, rows: [], busy: false, error: "" };
      this.$nextTick(() => this.$refs.tapsDialog.showModal());
      this.loadTapsPage(1);
    },
    closeAllTaps() {
      clearTimeout(this._tapSearchTimer);
      this.$refs.tapsDialog.close();
      this.allTaps.open = false;
    },
    searchTapsSoon() {
      clearTimeout(this._tapSearchTimer);
      this._tapSearchTimer = setTimeout(() => this.loadTapsPage(1), 300);
    },
    async loadTapsPage(page) {
      const want = { q: this.allTaps.query.trim(), page: String(Math.max(1, page)) };
      this.allTaps.busy = true;
      try {
        const data = await api("GET", "/api/taps/search?" + new URLSearchParams(want));
        if (!this.allTaps.open || want.q !== this.allTaps.query.trim()) return; // a newer search won
        Object.assign(this.allTaps, { rows: data.rows, total: data.total, page: data.page,
                                      pages: data.pages, error: "" });
      } catch (e) {
        this.allTaps.error = "Couldn't load taps: " + e.message;
      } finally {
        this.allTaps.busy = false;
      }
    },

    // --- Edit card ---------------------------------------------------------
    openEdit(card) {
      // The name field opens with the name the card shows, selected, ready to type over.
      const shown = card.custom_name || card.tag_name || "";
      this.edit = { open: true, orig: { ...card }, uid: card.uid, name: shown, nameBase: shown,
                    tracks: [...card.tracks], shuffle: card.shuffle, repeat: card.repeat,
                    remember: card.remember_playback, artPending: null,
                    art_version: card.art_version, busy: false, error: "" };
      this.artError = "";
      this.artSearch = { open: false, q: card.name, artist: card.artist || "", busy: false, searched: false, results: [] };
      this.$nextTick(() => {
        this.$refs.editDialog.showModal();
        const input = this.$refs.editNameInput;
        input?.focus();
        input?.select();
      });
    },
    closeEdit() {
      if (this.edit.busy) return;
      this.dropArtPending(); // Cancel/X: nothing pending is applied
      this.$refs.editDialog.close();
      this.edit.open = false;
    },
    async saveEdit() {
      const o = this.edit.orig;
      const uid = this.edit.uid.toLowerCase();
      this.edit.busy = true;
      this.edit.error = "";
      try {
        if (uid !== o.uid) {
          await api("POST", "/api/cards/" + encodeURIComponent(o.uid) + "/rename", { uid });
        }
        if (!sameList(this.edit.tracks, o.tracks) || this.editNameToSave !== (o.custom_name || "")
            || this.edit.shuffle !== o.shuffle || this.edit.repeat !== o.repeat
            || this.edit.remember !== o.remember_playback) {
          const name = this.editNameToSave;
          await api("PUT", "/api/cards/" + encodeURIComponent(uid), {
            tracks: this.edit.tracks, name, shuffle: this.edit.shuffle && this.edit.tracks.length > 1,
            repeat: this.edit.repeat, remember_playback: this.edit.remember,
          });
        }
        // Artwork last: changed tracks drop the old artwork on the box first.
        await this.applyArtPending(uid);
        this.edit.busy = false;
        this.closeEdit();
        this.notify("Card saved");
      } catch (e) {
        this.edit.error = e.message;
        this.edit.busy = false;
      }
      this.loadCards();
    },
    // --- Artwork (applied when the card is saved) -----------------------------
    artPath(uid) {
      return "/api/cards/" + encodeURIComponent(uid) + "/art";
    },
    dropArtPending() {
      const p = this.edit.artPending;
      if (p && p.kind === "upload") URL.revokeObjectURL(p.url);
      this.edit.artPending = null;
    },
    uploadArt(event) {
      const file = event.target.files[0];
      event.target.value = ""; // so picking the same file again works
      if (!file) return;
      this.artError = "";
      if (file.size > 15 * 1024 * 1024) {
        this.artError = "That image is larger than 15 MB.";
        return;
      }
      this.dropArtPending();
      this.edit.artPending = { kind: "upload", file, url: URL.createObjectURL(file) };
    },
    toggleArtSearch() {
      this.artSearch.open = !this.artSearch.open;
    },
    async searchArt() {
      const { q, artist } = this.artSearch;
      this.artSearch.busy = true;
      this.artError = "";
      try {
        const params = new URLSearchParams({ q: q.trim() });
        if (artist.trim()) params.set("artist", artist.trim());
        const data = await api("GET", "/api/artwork/search?" + params);
        this.artSearch.results = data.results;
      } catch (e) {
        this.artSearch.results = [];
        this.artError = e.message;
      } finally {
        this.artSearch.busy = false;
        this.artSearch.searched = true;
      }
    },
    pickArt(result) {
      this.artError = "";
      this.dropArtPending();
      this.edit.artPending = { kind: "musicbrainz", id: result.id, url: "/api/artwork/thumb/" + result.id };
      this.artSearch.open = false;
    },
    removeArt() {
      this.artError = "";
      const p = this.edit.artPending;
      this.dropArtPending();
      // Removing a not-yet-saved image just drops it; otherwise remove the card's own.
      if (!(p && p.kind !== "remove") && this.edit.art_version) this.edit.artPending = { kind: "remove" };
    },
    async applyArtPending(uid) {
      const p = this.edit.artPending;
      if (!p) return;
      if (p.kind === "upload") {
        const res = await fetch(this.artPath(uid), {
          method: "PUT", body: p.file, headers: { "Content-Type": p.file.type || "image/jpeg" },
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error("Artwork: " + (data.error || "HTTP " + res.status));
      } else if (p.kind === "musicbrainz") {
        await api("POST", this.artPath(uid) + "/musicbrainz", { id: p.id });
      } else if (p.kind === "remove") {
        await api("DELETE", this.artPath(uid)).catch(() => {}); // already gone is fine
      }
      this.dropArtPending();
    },

    async removeEditing() {
      const uid = this.edit.orig.uid;
      if (!confirm("Remove card " + uid + "? It will stop playing anything.")) return;
      this.edit.busy = true;
      try {
        await api("DELETE", "/api/cards/" + encodeURIComponent(uid));
        this.edit.busy = false;
        this.closeEdit();
        this.notify("Removed card " + uid);
      } catch (e) {
        this.edit.error = e.message;
        this.edit.busy = false;
      }
      this.loadCards();
    },

    // --- Assign new card ---------------------------------------------------
    async openAssign(uid = "") {
      this.assign = { open: true, uid, tracks: [], shuffle: false, repeat: false, remember: false,
                      busy: false, error: "" };
      this.$nextTick(() => this.$refs.assignDialog.showModal());
      await this.renewAssigning();
      clearInterval(this._assignTimer);
      this._assignTimer = setInterval(() => this.renewAssigning(), ASSIGN_RENEW_MS);
    },
    async renewAssigning() {
      if (!this.assign.open) return;
      try {
        await api("POST", "/api/assign/start", {});
        if (this.assign.error.startsWith("Lost")) this.assign.error = "";
      } catch (e) {
        this.assign.error = "Lost contact with the box: " + e.message;
      }
    },
    closeAssign() {
      if (this.assign.busy) return;
      clearInterval(this._assignTimer);
      this.$refs.assignDialog.close();
      this.assign.open = false;
      api("POST", "/api/assign/stop", {}).catch(() => {}); // it also times out by itself
    },
    onAssignTap(data) {
      if (!this.assign.open) return;
      this.assign.uid = data.uid;
      this.assign.error = "";
    },
    async confirmAssign() {
      const { uid, tracks } = this.assign;
      const verb = this.assignExisting ? "Reassigned" : "Assigned";
      this.assign.busy = true;
      this.assign.error = "";
      try {
        await api("PUT", "/api/cards/" + encodeURIComponent(uid), {
          tracks, shuffle: this.assign.shuffle && tracks.length > 1, repeat: this.assign.repeat,
          remember_playback: this.assign.remember,
        });
        this.assign.busy = false;
        this.closeAssign();
        this.notify(verb + " card " + uid + " → " + this.selectionName(tracks));
      } catch (e) {
        this.assign.error = e.message;
        this.assign.busy = false;
      }
      this.loadCards();
    },

    // --- Playback ----------------------------------------------------------
    async control(action, body = {}) {
      this.controlBusy = true;
      try {
        this.setNow(await api("POST", "/api/player/" + action, body));
      } catch (e) {
        this.notify(e.message, "error");
      } finally {
        this.controlBusy = false;
      }
    },
    // Escape never closes a modal. The dialogs block it, but Chrome still
    // closes one on a repeated Escape; if a dialog closes while its window is
    // meant to be open, open it again.
    keepOpen(ref) {
      const open = { settingsDialog: this.settings.open, setupDialog: this.setup.open, filesDialog: this.fm.open, folderDialog: this.newFolder.open, tapsDialog: this.allTaps.open,
                     editDialog: this.edit.open, assignDialog: this.assign.open }[ref];
      const el = this.$refs[ref];
      if (open && el && !el.open) el.showModal();
    },

    // --- Settings -------------------------------------------------------------
    setTheme(theme) {
      if (theme === this.theme) return;
      this.applyTheme(theme);
      try {
        localStorage.setItem(THEME_KEY, theme);
      } catch {
        // storage blocked: still applies for this visit
      }
    },
    openSettings() {
      this.settings = { ...this.settings, open: true, error: "", busy: false };
      this.$nextTick(() => this.$refs.settingsDialog.showModal());
      api("GET", "/api/wifi").then((w) => (this.wifi = w)).catch(() => {});
    },
    closeSettings() {
      this.$refs.settingsDialog.close();
      this.settings.open = false;
    },
    async scanWifi() {
      this.settings.scanning = true;
      this.settings.error = "";
      try {
        this.settings.inRange = (await api("POST", "/api/wifi/scan", {})).networks;
      } catch (e) {
        this.settings.error = e.message;
      } finally {
        this.settings.scanning = false;
      }
    },
    async addWifi() {
      this.settings.busy = true;
      this.settings.error = "";
      try {
        this.wifi = await api("POST", "/api/wifi/networks",
                              { ssid: this.settings.ssid.trim(), password: this.settings.password });
        this.notify("Saved " + this.settings.ssid.trim());
        this.settings.ssid = "";
        this.settings.password = "";
      } catch (e) {
        this.settings.error = e.message;
      } finally {
        this.settings.busy = false;
      }
    },
    async removeWifi(net) {
      if (!confirm("Remove the saved network " + net.ssid + "?")) return;
      this.settings.busy = true;
      this.settings.error = "";
      try {
        this.wifi = await api("DELETE", "/api/wifi/networks?" + new URLSearchParams({ uuid: net.uuid }));
      } catch (e) {
        this.settings.error = e.message;
      } finally {
        this.settings.busy = false;
      }
    },

    // --- Wi-Fi setup (the box is hosting "Tap to Play Setup") -------------------
    setWifi(status) {
      this.wifi = status;
      if (status.mode === "setup" && !this.setup.open) this.openSetup();
    },
    async openSetup() {
      this.setup = { ...this.setup, open: true, error: "", done: false };
      this.$nextTick(() => this.$refs.setupDialog.showModal());
      try {
        this.setup.inRange = (await api("POST", "/api/wifi/scan", {})).networks;
      } catch {
        this.setup.inRange = [];
      }
    },
    async submitSetup() {
      const ssid = this.setupSsid;
      this.setup.busy = true;
      this.setup.error = "";
      try {
        await api("POST", "/api/wifi/setup", { ssid, password: this.setup.password });
        this.setup.done = true;
        this.setup.joined = ssid;
      } catch (e) {
        this.setup.error = e.message;
      } finally {
        this.setup.busy = false;
      }
    },

    // --- Play Mode ------------------------------------------------------------
    async openPlayMode() {
      this.playMode = true;
      await this.$nextTick();
      const el = this.$refs.playModeEl;
      el.focus();
      // Real full screen where the browser allows it (not on iPhone Safari);
      // otherwise the overlay still covers the whole window.
      if (el.requestFullscreen) {
        try {
          await el.requestFullscreen({ navigationUI: "hide" });
        } catch {
          // fine: stays as a full-window overlay
        }
      }
    },
    closePlayMode() {
      this.playMode = false;
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    },

    setMode(kind, on) {
      this.control(kind, { on });
    },
    togglePause() {
      this.control(this.now.state === "paused" ? "resume" : "pause");
    },
    async commitSeek(position) {
      await this.control("seek", { position });
      this.seekDraft = null;
    },

    async setVolume(level) {
      if (level === this.volume.level) return;
      this.volumeBusy = true;
      try {
        const v = await api("POST", "/api/volume", { level });
        this.volume = v;
        if (!v.applied) this.notify("Volume set to " + level + "%. The speaker didn't confirm it yet; it's checked again before the next sound.", "error");
      } catch (e) {
        this.notify(e.message, "error");
      } finally {
        this.volumeBusy = false;
      }
    },

    // --- Live updates ------------------------------------------------------
    setNow(data) {
      this.clockOffset = data.server_time - Date.now() / 1000;
      // A different track, or none: drop any half-finished drag.
      if (data.uid !== this.now.uid || data.track !== this.now.track) this.seekDraft = null;
      this.now = data;
    },
    addTap(row) {
      if (this.taps.some((t) => t.id === row.id)) return;
      this.taps = [row, ...this.taps].slice(0, LATEST_TAPS);
      if (this.allTaps.open && this.allTaps.page === 1 && !this.allTaps.query.trim()) this.loadTapsPage(1);
      this.freshTap = row.id;
    },

    connect() {
      const source = new EventSource("/api/events");
      source.onopen = () => {
        this.connection = "live";
        // Catch up on anything missed while disconnected.
        this.loadCards();
        this.loadTaps();
        this.renewAssigning();
      };
      source.onerror = () => {
        // EventSource reconnects by itself.
        this.connection = "offline";
      };
      source.addEventListener("now", (e) => this.setNow(JSON.parse(e.data)));
      source.addEventListener("tap", (e) => this.addTap(JSON.parse(e.data)));
      source.addEventListener("cards", () => this.loadCards());
      source.addEventListener("volume", (e) => (this.volume = JSON.parse(e.data)));
      source.addEventListener("wifi", (e) => this.setWifi(JSON.parse(e.data)));
      source.addEventListener("assign_tap", (e) => this.onAssignTap(JSON.parse(e.data)));
    },
  },

  watch: {
    "edit.tracks"() {
      if (this.edit.open && !this.editNameTouched) {
        const next = sameList(this.edit.tracks, this.edit.orig.tracks)
          ? this.edit.orig.custom_name || this.edit.orig.tag_name || ""
          : this.editTagName;
        this.edit.name = this.edit.nameBase = next;
      }
    },
  },

  mounted() {
    // Follow the device's light/dark setting until a choice is saved here.
    const media = window.matchMedia && matchMedia("(prefers-color-scheme: dark)");
    media?.addEventListener?.("change", (e) => {
      if (!savedTheme()) this.applyTheme(e.matches ? "dark" : "light");
    });
    // Leaving the page with the Assign window open: let the box know.
    // (If the browser refuses, the box's 20 s timeout ends it anyway.)
    window.addEventListener("pagehide", () => {
      if (!this.assign.open) return;
      try {
        navigator.sendBeacon?.("/api/assign/stop", new Blob(["{}"], { type: "application/json" }));
      } catch {
        // ignored: see above
      }
    });
    // Leaving browser full screen (Esc, swipe) also leaves Play Mode.
    // Escape closes Play Mode (the one exception to "Escape never closes"):
    // in browser full screen, Escape leaves full screen, which closes it too.
    document.addEventListener("fullscreenchange", () => {
      if (!document.fullscreenElement && this.playMode) this.playMode = false;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.playMode) {
        e.preventDefault();
        this.closePlayMode();
      }
    });
    this.connect();
    setInterval(() => (this.tick = Date.now() / 1000), 250);
  },
});

app.component("app-logo", AppLogo);
app.component("track-picker", TrackPicker);
app.component("playback-controls", PlaybackControls);
app.component("volume-bar", VolumeBar);
app.component("card-options", CardOptions);
app.mount("#app");
