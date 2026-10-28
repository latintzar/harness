/* The Rocoto theme for Base Web — and THE ONE PLACE the brand lives.
 *
 * ═══ THE RE-THEME LAW (Peter, 2026-08-21, SUPER KEY) ═══════════════════════════════════════
 * Changing the brand — colours, font, corner radii, the dots — must be a change to the BRAND
 * block below and NOTHING else. Every component, every kit page, and every scenario screen
 * reads the theme; none of them owns a colour, a font, or a radius. This is enforced, not
 * hoped for:
 *   · `node tools/theme-gate.mjs` fails on hard-coded paint outside this file
 *   · open the kit with `?theme=proof` — a deliberately garish brand — and EVERYTHING must
 *     move: if some corner or colour stays put, that element is hard-coded and it is a bug.
 * ═══════════════════════════════════════════════════════════════════════════════════════════
 *
 * WHAT BASE SUPPLIES: everything the brand does not — the type ramp, spacing, motion, elevation,
 * density, and every component's states and transitions, unmodified.
 *
 * THE ONE LAW THAT SURVIVES ANY RE-THEME: colour is not a status system. Ink does the work;
 * the accent marks the AI's own act and nothing else; negative/warning/positive collapse onto
 * ink — there is no traffic light in this product.
 */
import { createLightTheme } from 'baseui';

const ROCOTO_SANS = '"Inter", system-ui, sans-serif';
const ROCOTO_SERIF = '"Fraunces", Georgia, serif';
/** Current brand choice. Point this at ROCOTO_SANS to return headings and the wordmark to sans. */
const ROCOTO_HEADING_FONT = ROCOTO_SERIF;

/** ── THE BRAND BLOCK — edit here, everything adapts ──────────────────────────────────────── */
export type Brand = {
  ink: string;      // the colour that does the work
  accent: string;   // the AI's own act, and nothing else
  paper: string;    // the ground
  font: string;
  /** Display and heading roles; operational copy stays on `font`. */
  headingFont: string;
  /** Product name and other explicit Rocoto wordmarks. */
  wordmarkFont?: string;
  /** §138.12 — dates have their own typographic voice. */
  dateFont?: string; // document dates; a second face without introducing monospace
  /** corner radii; omit a step to keep Base's own value for it */
  radius?: Partial<Record<'radius100' | 'radius200' | 'radius300' | 'radius400' | 'radius500', string>>;
  /** the dotted paper: dot grid tile size */
  dotTile: string;
};

export const BRAND: Brand = {
  dateFont: 'Georgia, "Times New Roman", serif',
  ink: '#1A1413',
  accent: '#C01B2C', // saffron
  paper: '#FFFFFF',
  font: ROCOTO_SANS,
  headingFont: ROCOTO_HEADING_FONT,
  wordmarkFont: ROCOTO_HEADING_FONT,
  // radius omitted: Base's own corners, per the founder's call ("Rocoto supplies only colour")
  dotTile: '14px 14px',
};

/** the proof brand — deliberately garish. `?theme=proof` renders the whole kit in it; anything
 *  that does NOT change is hard-coded somewhere and is a bug. Never ships anywhere. */
export const PROOF_BRAND: Brand = {
  dateFont: '"Arial", sans-serif',
  ink: '#1B3A6B',      // navy ink
  accent: '#0E7C4A',   // green accent
  paper: '#FAF6EE',    // cream paper
  font: 'Georgia, "Times New Roman", serif',
  headingFont: 'Georgia, "Times New Roman", serif',
  wordmarkFont: '"Arial", sans-serif',
  radius: { radius100: '10px', radius200: '14px', radius300: '18px', radius400: '22px', radius500: '28px' },
  dotTile: '22px 22px',
};

/* ── THE FACTORY — every pass parameterised by the brand ─────────────────────────────────── */

function hexToHsl(hex: string): { h: number; s: number; l: number } | null {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  const r = (n >> 16) / 255, g = ((n >> 8) & 255) / 255, b = (n & 255) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return { h: 0, s: 0, l };
  const d = max - min;
  const s = d / (l > 0.5 ? 2 - max - min : max + min);
  const h =
    max === r ? ((g - b) / d + (g < b ? 6 : 0)) / 6 :
    max === g ? ((b - r) / d + 2) / 6 :
    ((r - g) / d + 4) / 6;
  return { h, s, l };
}

function hslToHex(h: number, s: number, l: number): string {
  const f = (n: number) => {
    const k = (n + h * 12) % 12;
    const a = s * Math.min(l, 1 - l);
    const c = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return Math.round(c * 255).toString(16).padStart(2, '0');
  };
  return `#${f(0)}${f(8)}${f(4)}`;
}

/** re-hue a token onto the brand colour while PRESERVING the token's own lightness — a light
 *  wash stays a light wash, a deep tone stays deep. Crushing every hue to full-strength ink
 *  turned pale menu/list backgrounds into solid ink behind ink text (`?theme=proof` caught it:
 *  navy-on-navy dropdowns). */
function rehue(original: string, target: string): string {
  const o = hexToHsl(original), t = hexToHsl(target);
  if (!o || !t) return target;
  return hslToHex(t.h, t.s, o.l);
}

export function makeTheme(brand: Brand) {
  /* baseui 16's createLightTheme reads foundation COLOURS from `overrides.colors`; top-level
   * keys (`primaryA`, `primaryFontFamily`) are silently inert — that bug shipped here once. */
  const theme = createLightTheme({
    colors: {
      primaryA: brand.ink,
      primaryB: brand.paper,
      primary: brand.ink,     // ordinary interaction is the person's hand: ink, never accent
      accent: brand.accent,   // reserved: the AI's own act
      // collapsed onto ink deliberately — colour is not a status system
      negative: brand.ink,
      warning: brand.ink,
      positive: brand.ink,
      contentPrimary: brand.ink,
    },
  });

  /* Button loading spinners: stock Base spins its ACCENT on every kind — Uber's own choice.
   * Here the accent is reserved for the AI, and a person's button loading is the person's act,
   * so each spinner takes its button's own contrast pair instead. */
  Object.assign(theme.colors as Record<string, string>, {
    buttonPrimarySpinnerForeground: brand.paper,
    buttonPrimarySpinnerBackground: 'rgba(255,255,255,0.32)',
    buttonSecondarySpinnerForeground: brand.ink,
    buttonSecondarySpinnerBackground: theme.colors.mono400,
    buttonTertiarySpinnerForeground: brand.ink,
    buttonTertiarySpinnerBackground: theme.colors.mono400,
    buttonDangerPrimarySpinnerForeground: brand.paper,
    buttonDangerPrimarySpinnerBackground: 'rgba(255,255,255,0.32)',
    buttonDangerSecondarySpinnerForeground: brand.ink,
    buttonDangerSecondarySpinnerBackground: theme.colors.mono400,
  });

  /* the company face, stamped onto every typography step — v16 has no single font primitive */
  for (const step of Object.values(theme.typography as Record<string, { fontFamily?: string }>)) {
    if (step && typeof step === 'object' && 'fontFamily' in step) step.fontFamily = brand.font;
  }

  /* Semantic titles carry the calm editorial face. Clone aliased Base roles so raw font steps,
   * paragraphs, labels, fields, buttons and number-heavy operational text remain on `brand.font`. */
  const typography = theme.typography as Record<string, { fontFamily?: string }>;
  for (const role of Object.keys(typography)) {
    if (/^(Display|Heading)/.test(role)) {
      typography[role] = { ...typography[role], fontFamily: brand.headingFont };
    }
  }

  /* the corner knob */
  if (brand.radius) Object.assign(theme.borders as Record<string, unknown>, brand.radius);

  /* THE COLOUR LAW, ENFORCED. Base derives component colours from semantic colours from
   * foundation colours, so a hue still surfaces on the progress bar and the error field after
   * the foundation is swapped. Walk every resolved colour: accent-keyed hues take the brand
   * accent, every other hue takes ink. Greys, whites and alphas pass untouched, so Base's
   * elevation and hairline work survives exactly as built. */
  const ACCENT_KEY = /accent/i;
  // the brand's own colours are exempt: a deliberately warm paper (PROOF's cream is s=0.54)
  // must not be "corrected" by the very sweep that exists to protect it
  const BRAND_OWN = new Set([brand.ink, brand.accent, brand.paper].map((c) => c.toLowerCase()));
  for (const [key, value] of Object.entries(theme.colors as Record<string, string>)) {
    if (typeof value !== 'string' || BRAND_OWN.has(value.toLowerCase())) continue;
    const hsl = hexToHsl(value);
    if (!hsl || hsl.s < 0.15) continue;   // grey/ink/white: leave Base's work alone
    (theme.colors as Record<string, string>)[key] = rehue(value, ACCENT_KEY.test(key) ? brand.accent : brand.ink);
  }

  // surfaces read brand extras (dot tile) off the theme, so the proof brand moves them too
  (theme as unknown as { brand: Brand }).brand = brand;
  return theme;
}

export const RocotoTheme = makeTheme(BRAND);

/** legacy import shape — several files read `rocoto.ink`/`rocoto.saffron` */
export const rocoto = { ink: BRAND.ink, saffron: BRAND.accent };

/** THE DOTTED PAPER. Image and tile size must be SEPARATE CSS properties — the shorthand form
 *  is silently dropped by `background-image`. Pick the dot colour for contrast against its
 *  ground (`borderOpaque` equals `backgroundSecondary` in Base's light theme — invisible). */
export const dots = (color: string) =>
  `radial-gradient(circle at 1px 1px, ${color} 1px, transparent 0)`;

export const DOT_TILE = BRAND.dotTile;

/* ── THE TINTED DOTS ARE GONE, AND THE REPEAL IS THE RECORD (FEED-IA §11) ─────────────────────
 * THE-APP-V2 §6.3 asked for brand in the ground's dots — «we need colour in the dots behind, a
 * subtle red maybe» — and this file carried the recipe for it: `DOT_TINT = 22` and
 * `dotInk(accent, mono)`, a color-mix of two theme tokens.
 *
 * §11 REPEALED THE WISH ITSELF, not merely its call sites: «the background is kinda making it hard
 * to see shit» took the dotted paper off the app's ground everywhere, and left the dot grid meaning
 * exactly one thing — A CONVERSATION. Its closing sentence is explicit: «Supersedes THE-APP-V2
 * §6.3's tinted ground (the tint went with it).» THE-AUDIT-VERDICT's U6 records the same reversal
 * as holding, and notes where the red survives instead: the hairline round the embedded chat (§17),
 * which is a border and not a ground.
 *
 * SO THE RECIPE IS PRUNED RATHER THAN LEFT STANDING. Every chat surface paints
 * `dots(theme.colors.mono400)` on `mono100` — a grey grid on paper — and that is §11's answer, not
 * an unfinished §6.3: a wash of accent under every conversation in the app would be the accent
 * saying something other than «Rocoto acted», which is the one law no re-theme may bend. A helper
 * with no callers is a standing invitation to re-open a settled question, so it goes, and this
 * paragraph is what it leaves behind.
 *
 * The one sanctioned brand-in-a-ground is the connect wash below (§51.20), which is licensed by
 * name, bounded to three surfaces, and greppable.
 */

/* ══ THE CONNECT ZONE — THE ONE SANCTIONED DELIGHT (FEED-IA §51.20) ═══════════════════════════
 * «THE CONNECT IS A FLAGSHIP FEELING — the inbox/ERP hookup should feel exciting: a main icon,
 * maybe drops of colour or a gradient — a sanctioned delight zone for integrations (make them feel
 * they're handling a plug into our product).» His explicit licence to break austerity THERE and
 * only there.
 *
 * ═══ WHAT THE LICENCE COVERS, EXACTLY ══════════════════════════════════════════════════════
 * Three surfaces, and nothing else in the product may draw these:
 *   1 · the connect rows and cards in Settings (the inbox, the ERP)
 *   2 · the export centre's unconnected rails
 *   3 · Stock's connect-inbox door
 * A fourth place drawing this wash is a bug, and it is checkable: the recipes are exported from
 * here and grep finds every caller.
 *
 * ═══ WHY THIS DOES NOT BREAK THE COLOUR LAW ════════════════════════════════════════════════
 * «The accent marks the AI's own act and nothing else» stands, and this is not the accent: it is a
 * WASH — the brand hue at 4–13% of the paper, a ground rather than an ink. The saffron `ActDot`
 * still means Rocoto acted and still appears nowhere near a connect card; the plug's own ink is
 * the accent at full strength on that ground, which is the one place §51.20 licences it. Nothing
 * here is a status: an unconnected rail and a connected one differ by the CARD being present, not
 * by a hue meaning good or bad.
 *
 * ═══ IT IS A MIX OF TWO THEME TOKENS AND HOLDS NO COLOUR OF ITS OWN ════════════════════════
 * The discipline every recipe in this file keeps: `?theme=proof` has to move it with everything
 * else, and the theme gate has to stay green without an escape hatch. A gradient with a
 * hex in it would be the very bug the gate exists to catch — so the caller passes
 * `theme.colors.accent` and `theme.colors.backgroundPrimary` in, and the brand block above remains
 * the only place a colour is written.
 */

/** how much brand is in the connect ground at its strongest corner, and where it fades to */
export const CONNECT_TINT = 13;
export const CONNECT_TINT_FAR = 4;
/** how much brand is in the connect edge — enough to read as a lit rim, short of a status border */
export const CONNECT_EDGE_TINT = 38;

/** THE CONNECT GROUND. A diagonal wash off the brand hue: strongest under the plug at the top
 *  left, gone by the far corner, so a card reads as lit from where the mark stands rather than as
 *  a coloured box. Two theme tokens in, no colour of its own. */
export const connectWash = (accent: string, paper: string): string =>
  `linear-gradient(135deg, color-mix(in srgb, ${accent} ${CONNECT_TINT}%, ${paper}) 0%,`
  + ` color-mix(in srgb, ${accent} ${CONNECT_TINT_FAR}%, ${paper}) 62%, ${paper} 100%)`;

/** THE CONNECT EDGE — the hairline around that ground, the brand carried into Base's own border. */
export const connectEdge = (accent: string, border: string): string =>
  `color-mix(in srgb, ${accent} ${CONNECT_EDGE_TINT}%, ${border})`;

/** THE PLUG'S OWN SEAT — the tile the mark stands in, a step deeper than the card it sits on so
 *  the icon reads as the object being handled rather than as decoration on a panel. */
export const connectTile = (accent: string, paper: string): string =>
  `color-mix(in srgb, ${accent} 18%, ${paper})`;

/* ══ THE EMULATED PHONE — THE ONE PALETTE EXCEPTION, LICENSED BY NAME (FEED-IA §91.4.8) ═══════
 * «The test chat wears the ROLEPLAYING UI of the current .com/demo, rebuilt in the kit: TWO
 * VIEWS, toggleable — the WhatsApp side (the customer's, WhatsApp colors) and their side (the
 * house's view).»
 *
 * ═══ WHY THIS IS NOT A HOLE IN THE COLOUR LAW ══════════════════════════════════════════════
 * «The accent marks the AI's own act and nothing else» is a law about ROCOTO'S OWN SURFACES. The
 * WhatsApp pane is not one: it is a DRAWING OF SOMEBODY ELSE'S APP, shown to the operator so they
 * can see what their customer sees. Painting it in Rocoto's palette would be the lie — a person
 * would be looking at «what arrives on their phone» in colours that never arrive on any phone.
 * The exception is therefore bounded exactly as §51.20's connect wash is: named here, exported
 * from here and greppable. It reaches TWO surfaces, and a third is a bug:
 *   1 · the emulated phone — the customer's side of the test chat
 *   2 · the sign-up's referral bubble (`start/referrals.tsx`), which previews the invitation the
 *       owner is about to hand to their OWN WhatsApp, drawn as it will arrive there. Same reason
 *       as the pane: a picture of what lands on somebody's phone, in the colours it lands in.
 *
 * ═══ AND IT IS A SCOPED THEME, THE SAME MECHANISM THE .COM DEMO USES ═══════════════════════
 * `webapp/src/demo/wa.css` is a stylesheet that «only re-declares custom properties»: the
 * geometry, the radii, the gesture machine and every component stay the app's, and only the paint
 * changes. The kit's equivalent of a scoped custom-property block is a scoped Base theme, so this
 * function takes the live theme and hands back the same theme with FOUR ROLES repointed. Every
 * component inside the provider — the Message bubbles, the Composer, the send circle, the voice
 * button — repaints without knowing anything about WhatsApp, and a change to any of them changes
 * the emulated pane with it. Nothing is redrawn.
 *
 * ═══ WHAT MOVES, AND NOTHING ELSE ══════════════════════════════════════════════════════════
 *   accent                 the send circle, the mic, the caret — one role in the kit, and on a
 *                          phone that role IS WhatsApp's green
 *   backgroundTertiary     the OUTGOING bubble: WhatsApp's pale green, mixed over the card white
 *   backgroundPrimary      the incoming bubble and the top bar: white, as WhatsApp draws them
 *   mono100                the wallpaper the conversation stands on
 * Type, spacing, radii, elevation, hairlines and every other token stay the kit's own — which is
 * what keeps the pane recognisably the same components rather than a second chat.
 */

/** WhatsApp's own three, and the only place in this repo they are written. */
export const WHATSAPP: { green: string; onGreen: string; paper: string } = {
  green: '#25D366',
  onGreen: '#FFFFFF',
  paper: '#ECE5DD',
};

/** how much of WhatsApp's green is in the customer's own bubble — their pale green, mixed rather
 *  than a fourth literal, so the card white underneath is still the theme's */
export const WHATSAPP_BUBBLE_TINT = 22;

type AnyTheme = ReturnType<typeof makeTheme>;

/** THE SCOPED RE-THEME. Four roles repointed on a copy; the original theme is untouched. */
export function whatsappTheme(base: AnyTheme): AnyTheme {
  const colors = {
    ...base.colors,
    accent: WHATSAPP.green,
    backgroundTertiary: `color-mix(in srgb, ${WHATSAPP.green} ${WHATSAPP_BUBBLE_TINT}%, ${base.colors.backgroundPrimary})`,
    mono100: WHATSAPP.paper,
    backgroundInversePrimary: WHATSAPP.green,
    /* THE SEND CIRCLE IS THE SAME ROLE, REPAINTED. The kit's send is INK on purpose («sending is
       the person's act»); on a phone that same control is WhatsApp's green disc, so the primary
       button's own three states move with the theme rather than a component being told about
       WhatsApp. The glyph on it stays warm white, which is what WhatsApp draws. */
    buttonPrimaryFill: WHATSAPP.green,
    buttonPrimaryHover: `color-mix(in srgb, ${WHATSAPP.green} 88%, ${base.colors.contentPrimary})`,
    buttonPrimaryActive: `color-mix(in srgb, ${WHATSAPP.green} 78%, ${base.colors.contentPrimary})`,
    buttonPrimaryText: WHATSAPP.onGreen,
  };
  return { ...base, colors } as AnyTheme;
}

/* ══ THE CHANNEL A MESSAGE LEAVES ON, SAID ON THE BUTTON (FEED-IA §196.7) ═════════════════════
 * Peter, 2026-09-04, verbatim:
 *
 *   «maybe it should denote that by putting the icon in the composer or making the composer's
 *    outline green, or actually better: the send/record button. and if it's email, it can be blue
 *    button.»
 *
 * ═══ WHY IT IS THE SAME LICENCE AS THE EMULATED PHONE ABOVE ═════════════════════════════════
 * «The accent marks the AI's own act and nothing else» is a law about ROCOTO'S OWN acts. A channel
 * is not one: WhatsApp's green and mail's blue are OTHER PEOPLE'S marks, and the whole point of
 * putting one on the button is that a person recognises it without reading a word. Painting the
 * send disc in Rocoto's saffron would say «Rocoto is doing this», which is the opposite of what
 * the control means — the person sends, and the colour says down which pipe. So the exception is
 * bounded exactly as the emulated pane's is: named here, exported from here, greppable, reaching
 * exactly ONE control per composer.
 *
 * ═══ AND IT IS A SCOPED THEME, NOT A PAINTED COMPONENT ══════════════════════════════════════
 * `channelTheme` repoints the SAME FOUR button roles `whatsappTheme` repoints, so the Composer's
 * send disc and the Voice Button's record disc — «the send/record button», both of them — take the
 * channel's colour without either component learning what a channel is. Nothing is redrawn.
 */

/** MAIL'S OWN BLUE, and the ink that reads on it. One literal, one home, same rule as WHATSAPP.
 *  It is the deeper of the mail blues on purpose: white on it measures 6.4:1, so the arrow inside
 *  the 44px disc is legible rather than merely coloured. */
export const EMAIL: { blue: string; onBlue: string } = {
  blue: '#0B57D0',
  onBlue: '#FFFFFF',
};

export type ChannelId = 'whatsapp' | 'email';

/** THE TWO CHANNELS' MARKS, in one map, so a surface asks for a channel and never for a colour. */
export const CHANNEL_INK: Record<ChannelId, { fill: string; on: string }> = {
  whatsapp: { fill: WHATSAPP.green, on: WHATSAPP.onGreen },
  email: { fill: EMAIL.blue, on: EMAIL.onBlue },
};

/** THE SCOPED RE-THEME FOR ONE COMPOSER. Six roles repointed on a copy; the original is
 *  untouched, and everything else in the composer — type, spacing, radii, the field, the ink of
 *  the words being written — stays the kit's own.
 *
 *  ⚠ THE WAITING STATE IS THE CHANNEL TOO (§196.7, found by E193.17). The send control used to
 *  vanish on an empty box and the RECORD disc carried the channel in its place; with the mic in a
 *  seat of its own the send is always there, and Base's `buttonDisabledFill` painted it the app's
 *  neutral grey — so an empty composer stopped saying which pipe it was pointed down, which is
 *  exactly the fact «or actually better: the send/record button» exists to state. It is the
 *  channel WAITING, so it is the channel's own colour standing back, never a different colour. */
export function channelTheme(base: AnyTheme, channel: ChannelId): AnyTheme {
  const { fill, on } = CHANNEL_INK[channel];
  const colors = {
    ...base.colors,
    buttonPrimaryFill: fill,
    buttonPrimaryHover: `color-mix(in srgb, ${fill} 88%, ${base.colors.contentPrimary})`,
    buttonPrimaryActive: `color-mix(in srgb, ${fill} 78%, ${base.colors.contentPrimary})`,
    buttonPrimaryText: on,
    buttonDisabledFill: `color-mix(in srgb, ${fill} 32%, ${base.colors.backgroundPrimary})`,
    buttonDisabledText: on,
  };
  return { ...base, colors } as AnyTheme;
}

/* ══ YAPE'S OWN PURPLE — THE THIRD NAMED EXCEPTION, ON THE SAME LICENCE (FEED-IA §268) ════════
 * Peter, 20 September 2026: «we ask them if they wanna connect their Yape so that they can auto
 * send yape links when people order … with nicely branded shit».
 *
 * ═══ WHY THIS IS NOT A HOLE IN THE COLOUR LAW ══════════════════════════════════════════════
 * Identical to WHATSAPP above, and bounded the same way. «The accent marks the AI's own act and
 * nothing else» is a law about ROCOTO'S OWN surfaces; the Yape field on the connect screen is a
 * drawing of SOMEBODY ELSE'S product, shown so a shopkeeper recognises in one glance which app
 * this screen is about. Painting it saffron would say «Rocoto did this», which is the opposite of
 * what the field means: the shop connects its own Yape.
 *
 * ═══ WHAT IT REACHES, EXACTLY ═════════════════════════════════════════════════════════════
 * ONE surface — the `Connect your Yape` step of the sign-up walk and the Yape block of the
 * customer's payment page. It is exported from here, so grep finds every caller; a fourth place
 * drawing it is a bug. It is a GROUND and a MARK, never a status: a connected Yape and a Yape
 * still to connect differ by what the screen says, never by a hue meaning good or bad.
 */
export const YAPE: { purple: string; onPurple: string } = {
  purple: '#742384',
  onPurple: '#FFFFFF',
};

/** THE YAPE GROUND — the brand at a wash over the card's own paper, so the field reads as Yape's
 *  seat rather than as a purple box. Two colours in, no third literal. It is the block on the
 *  customer's payment page, where Yape stands beside the rest of the page's own paper. */
export const yapeWash = (paper: string): string =>
  `linear-gradient(160deg, color-mix(in srgb, ${YAPE.purple} 14%, ${paper}) 0%,`
  + ` color-mix(in srgb, ${YAPE.purple} 5%, ${paper}) 70%, ${paper} 100%)`;

/** THE LIFT ON A FIELD THAT IS YAPE'S PURPLE OUTRIGHT — the light falling from where the mark
 *  stands, so the sign-up's Yape stage reads as a lit surface rather than as a flat rectangle.
 *  Yape's own two colours, mixed; no third literal. Drawn by `start/yape.tsx` alone. */
export const yapeLift = (): string =>
  `radial-gradient(124% 84% at 50% 10%, color-mix(in srgb, ${YAPE.onPurple} 17%, ${YAPE.purple}) 0%,`
  + ` ${YAPE.purple} 74%)`;

/* THE WEBSITE'S WARM GROUND (soyrocoto.com), for candidates that carry the site's aesthetic into
 * an app (Peter, 2026-09-30: «kit with the aesthetic of the website»). Ink and accent stay the
 * brand's own; these are the site's paper, its sand wash, its hairlines and its muted text. Line
 * drawings take their neutral fills from here so a re-theme moves them too. */
export const SITE: {
  paper: string; sand: string; wash: string; line: string; stone: string; clay: string;
  muted: string; faint: string; water: string; blush: string; rose: string;
} = {
  paper: '#FAF7F3',  // the page
  sand: '#F1EDE6',   // a quiet panel
  wash: '#F4F0E9',   // a drawing's floor
  line: '#E7E2D9',   // hairlines, empty tracks
  stone: '#D6CFC3',  // neutral product fill
  clay: '#B9B1A4',   // a switched-off drawing's stroke
  muted: '#6B675F',  // secondary text
  faint: '#8A857C',  // inactive tab
  water: '#E3EEF0',  // bottled water in a drawing
  blush: '#FBE3E5',  // the accent's pale face on a drawn box
  rose: '#F4CDD1',   // the accent's side face on a drawn box
};

/** A heatmap's ramp, coolest to hottest: warm paper through ochre and sienna to burnt umber, the
 *  palette of the site's painted shop scenes, so heat reads as heat without borrowing the accent
 *  (which stays Rocoto's own act). Used by Sofia's «Where people go». */
export const HEATMAP: string[] = ['#F7EFE2', '#F0D9AE', '#E3B26E', '#C97D43', '#9C4A2A', '#5E2A1C'];

