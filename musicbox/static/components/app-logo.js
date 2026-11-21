// The "Tap to Play" logo: markup, behaviour and styles all live here.
//
// Use it anywhere in a template:
//     <app-logo></app-logo>            medium (the page header)
//     <app-logo size="sm"></app-logo>  small
//     <app-logo size="lg"></app-logo>  large
//     <app-logo :mark="false"></app-logo>  the word only, no icon
//
// The styles are injected once, and every rule is prefixed ".ttp-logo", so
// nothing here leaks into the page and nothing in style.css is needed.
// Colours follow the page's light/dark accent when it has one
// (--mb-accent, --mb-text), and fall back to fixed values elsewhere.

const STYLE_ID = "ttp-logo-styles";

const CSS = `
.ttp-logo {
  --ttp-blue: var(--mb-accent, #2463d6);
  --ttp-ink: var(--mb-text, #1e2733);
  --ttp-ring: color-mix(in srgb, var(--ttp-blue) 35%, transparent);
  --ttp-bg: var(--pico-background-color, #fff); /* behind the tap ring */
  display: inline-flex; align-items: center; gap: 0.42em;
  font-size: 1.6rem; line-height: 1; white-space: nowrap;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  color: var(--ttp-ink); text-decoration: none; user-select: none;
}
.ttp-logo.ttp-sm { font-size: 1.05rem; }
.ttp-logo.ttp-md { font-size: 1.6rem; }
.ttp-logo.ttp-lg { font-size: 2.6rem; }

/* The mark: a rounded blue tile with a play triangle and a "tap" ripple. */
.ttp-logo .ttp-mark {
  position: relative; flex: none; width: 1.18em; height: 1.18em; border-radius: 0.32em;
  background: linear-gradient(145deg, color-mix(in srgb, var(--ttp-blue) 82%, #fff) 0%, var(--ttp-blue) 70%);
  box-shadow: 0 0.06em 0.18em color-mix(in srgb, var(--ttp-blue) 45%, transparent);
}
.ttp-logo .ttp-mark::before {
  /* play triangle, nudged right so it looks centred */
  content: ""; position: absolute; left: 0.43em; top: 0.31em; width: 0.42em; height: 0.56em;
  background: #fff; clip-path: polygon(0 0, 100% 50%, 0 100%); border-radius: 0.04em;
}
.ttp-logo .ttp-mark::after {
  /* the tap: a ring around the tile's corner */
  content: ""; position: absolute; right: -0.2em; bottom: -0.2em; width: 0.42em; height: 0.42em;
  border-radius: 50%; border: 0.07em solid var(--ttp-blue); background: var(--ttp-bg);
  box-shadow: 0 0 0 0.07em var(--ttp-ring);
}

/* The word: "Tap" blue, bold and italic; "to Play" plain. */
/* Plain inline, so the real space between the words shows (and is read aloud). */
.ttp-logo .ttp-word { display: inline; letter-spacing: -0.015em; }
.ttp-logo .ttp-tap {
  color: var(--ttp-blue); font-weight: 800; font-style: italic;
  padding-right: 0.08em; /* room for the italic's lean */
}
.ttp-logo .ttp-rest { font-weight: 600; }
`;

function injectStyles() {
  if (typeof document === "undefined" || document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.appendChild(style);
}

export default {
  name: "AppLogo",
  props: {
    size: { type: String, default: "md", validator: (v) => ["sm", "md", "lg"].includes(v) },
    mark: { type: Boolean, default: true },
    tag: { type: String, default: "span" },
  },
  created() {
    injectStyles();
  },
  template: `
    <component :is="tag" class="ttp-logo" :class="'ttp-' + size">
      <span v-if="mark" class="ttp-mark" aria-hidden="true"></span>
      <span class="ttp-word"><span class="ttp-tap">Tap</span> <span class="ttp-rest">to Play</span></span>
    </component>`,
};
