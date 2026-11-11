import { AbsoluteFill, Composition, Img, OffthreadVideo, Sequence, Still, interpolate, staticFile, useCurrentFrame } from "remotion";

const FPS = 30;
const s = (sec: number) => Math.round(sec * FPS);

// Scene times come from demo/out/markers.json (seconds into each real recording).
type Cap = { from: number; text: string };
type Clip = { file: string; trim: number; end: number; who: string; hideWhoFrom?: number; caps: Cap[] };

const CLIPS: Clip[] = [
  {
    file: "hub.webm", trim: 2.22, end: 62.94, who: "Signed in as: ops team",
    caps: [
      { from: 2.22, text: "The store is connected. The CRM isn't yet" },
      { from: 5.25, text: "Connect the CRM with OAuth, the way HubSpot does it" },
      { from: 8.76, text: "The CRM's consent screen: allow access to contacts" },
      { from: 12.31, text: "Connected. Two customers sign up in the store, and the hub creates them in the CRM" },
      { from: 17.49, text: "Both systems side by side: store on the left, CRM on the right" },
      { from: 20.62, text: "A sales rep edits Ana in the CRM while she places an order. Both changes arrive, each in its own direction" },
      { from: 26.17, text: "Every decision is logged. \"No change\" is the hub's own write echoing back: recognised, so nothing loops" },
      { from: 34.34, text: "Ana's phone was changed in both systems. The hub overwrites neither and asks a person" },
      { from: 46.48, text: "The CRM goes down. A new customer is retried, then parked with the error" },
      { from: 50.46, text: "The CRM is back: replay, and nothing was lost" },
      { from: 57.37, text: "Both systems in sync" },
    ],
  },
];

const INTRO = s(4), OUTRO = s(5);
const clipLen = (c: Clip) => s(c.end - c.trim);
const TOTAL = INTRO + CLIPS.reduce((n, c) => n + clipLen(c), 0) + OUTRO;

const BG = "#1b2130", ACCENT = "#ff8a4c", FONT = "Verdana, Geneva, sans-serif";

const Fade: React.FC<{ len: number; children: React.ReactNode }> = ({ len, children }) => {
  const f = useCurrentFrame();
  const o = interpolate(f, [0, 10, len - 10, len], [0, 1, 1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return <AbsoluteFill style={{ opacity: o }}>{children}</AbsoluteFill>;
};

const Title: React.FC<{ kicker: string; title: string; sub: string; size?: number }> = ({ kicker, title, sub, size = 92 }) => (
  <AbsoluteFill style={{ background: BG, color: "white", fontFamily: FONT, justifyContent: "center", padding: 140 }}>
    <div style={{ color: ACCENT, fontSize: 34, fontWeight: 700, letterSpacing: 3, textTransform: "uppercase" }}>{kicker}</div>
    <div style={{ fontSize: size, fontWeight: 800, lineHeight: 1.05, marginTop: 24, maxWidth: 1500 }}>{title}</div>
    <div style={{ fontSize: 38, color: "#94a3b8", marginTop: 36, maxWidth: 1500, lineHeight: 1.35 }}>{sub}</div>
  </AbsoluteFill>
);

const ClipScene: React.FC<{ clip: Clip }> = ({ clip }) => {
  const t = clip.trim + useCurrentFrame() / FPS;
  const cap = [...clip.caps].reverse().find((c) => t >= c.from) ?? clip.caps[0];
  return (
    <AbsoluteFill style={{ background: BG, fontFamily: FONT }}>
      <div style={{ position: "absolute", left: 160, top: 24, width: 1600, height: 1000, borderRadius: 14, overflow: "hidden", boxShadow: "0 20px 60px rgba(0,0,0,.5)" }}>
        <OffthreadVideo src={staticFile(clip.file)} trimBefore={s(clip.trim)} muted style={{ width: 1600, height: 1000 }} />
      </div>
      {t < (clip.hideWhoFrom ?? Infinity) && <div style={{ position: "absolute", left: 186, top: 320, background: ACCENT, color: "white", fontWeight: 700, fontSize: 24, padding: "8px 16px", borderRadius: 8 }}>{clip.who}</div>}
      <div style={{ position: "absolute", left: 220, right: 220, bottom: 90, display: "flex", justifyContent: "center" }}>
        <div key={cap.from} style={{ background: BG + "eb", color: "white", fontSize: 40, fontWeight: 600, padding: "18px 30px", borderRadius: 12, borderLeft: `8px solid ${ACCENT}`, lineHeight: 1.3 }}>{cap.text}</div>
      </div>
    </AbsoluteFill>
  );
};

export const Video: React.FC = () => {
  let at = INTRO;
  return (
    <AbsoluteFill style={{ background: BG }}>
      <Sequence durationInFrames={INTRO}>
        <Fade len={INTRO}>
          <Title kicker="Sync Hub · real recording" title="Your store and your CRM, kept in sync both ways"
            sub="OAuth and signed webhooks, field ownership, conflicts sent to a person, no echo loops, retries with a dead-letter queue. One docker compose up." />
        </Fade>
      </Sequence>
      {CLIPS.map((c) => {
        const from = at;
        at += clipLen(c);
        return (
          <Sequence key={c.file} from={from} durationInFrames={clipLen(c)}>
            <ClipScene clip={c} />
          </Sequence>
        );
      })}
      <Sequence from={at} durationInFrames={OUTRO}>
        <Fade len={OUTRO}>
          <Title kicker="Source, tests and CI" title="github.com/basartuncay/automation-portfolio" size={76}
            sub="Python · OAuth 2 · HMAC webhooks · FastAPI · Docker · e2e in GitHub Actions. The store and CRM here are local Shopify- and HubSpot-shaped stand-ins." />
        </Fade>
      </Sequence>
    </AbsoluteFill>
  );
};

export const Thumb: React.FC = () => (
  <AbsoluteFill style={{ background: BG, fontFamily: FONT }}>
    <div style={{ position: "absolute", right: -80, top: 120, width: 1100, height: 688, borderRadius: 18, overflow: "hidden", transform: "rotate(-3deg)", boxShadow: "0 30px 80px rgba(0,0,0,.6)" }}>
      <Img src={staticFile("queue.png")} style={{ width: 1100, height: 688 }} />
    </div>
    <div style={{ position: "absolute", left: 90, top: 200, width: 820, color: "white" }}>
      <div style={{ color: ACCENT, fontSize: 40, fontWeight: 800, letterSpacing: 3 }}>INTEGRATIONS</div>
      <div style={{ fontSize: 110, fontWeight: 900, lineHeight: 1.0, marginTop: 20 }}>Two-way store ⇄ CRM sync</div>
      <div style={{ fontSize: 40, color: "#94a3b8", marginTop: 40 }}>Sync Hub · OAuth + webhooks + Docker</div>
    </div>
  </AbsoluteFill>
);

export const MyComposition = () => (
  <>
    <Composition id="SyncHub" component={Video} durationInFrames={TOTAL} fps={FPS} width={1920} height={1080} />
    <Still id="Thumbnail" component={Thumb} width={1920} height={1080} />
  </>
);
