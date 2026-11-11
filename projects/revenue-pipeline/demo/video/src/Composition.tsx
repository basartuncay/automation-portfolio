import { AbsoluteFill, Composition, Img, OffthreadVideo, Sequence, Still, interpolate, staticFile, useCurrentFrame } from "remotion";

const FPS = 30;
const s = (sec: number) => Math.round(sec * FPS);

// Scene times come from demo/out/markers.json (seconds into each real recording).
type Cap = { from: number; text: string };
type Clip = { file: string; trim: number; end: number; who: string; hideWhoFrom?: number; caps: Cap[] };

const CLIPS: Clip[] = [
  {
    file: "pipeline.webm", trim: 3.34, end: 69.86, who: "Signed in as: finance",
    caps: [
      { from: 3.34, text: "Revenue from three sources: store orders, Stripe payments, Google and Meta ad spend. Every number passed 20 data tests" },
      { from: 8.42, text: "ROAS and CAC per channel, and how many customers come back" },
      { from: 12.42, text: "Built with dbt on DuckDB: raw copies of each source, cleaned staging, tested marts" },
      { from: 17.58, text: "Every run extracts, builds and tests. Now an ad platform sends a negative spend row…" },
      { from: 23.99, text: "A data test catches it. The run fails and is not published; downstream models are skipped" },
      { from: 40.04, text: "The dashboard says so, and keeps showing the last verified numbers" },
      { from: 45.71, text: "Someone renames a column in the Google Ads export: the run stops before loading shifted data" },
      { from: 55.59, text: "Fixed, and a refund for a September order arrives today: that past day is corrected, all tests pass again" },
    ],
  },
];

const INTRO = s(4), OUTRO = s(5);
const clipLen = (c: Clip) => s(c.end - c.trim);
const TOTAL = INTRO + CLIPS.reduce((n, c) => n + clipLen(c), 0) + OUTRO;

const BG = "#0b3954", ACCENT = "#5fc4cc", FONT = "'Hoefler Text', Baskerville, Georgia, serif";

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
          <Title kicker="Revenue Pipeline · real recording" title="Revenue numbers you can trust, every morning"
            sub="Shopify, Stripe and ad platforms → a dbt + DuckDB warehouse → a dashboard that only shows data that passed every test. One docker compose up." />
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
            sub="Python · dbt · DuckDB · FastAPI · Docker · e2e in GitHub Actions. The sources here are local Shopify-, Stripe- and ad-platform-shaped stand-ins." />
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
      <div style={{ color: ACCENT, fontSize: 40, fontWeight: 800, letterSpacing: 3 }}>DATA PIPELINES</div>
      <div style={{ fontSize: 110, fontWeight: 900, lineHeight: 1.0, marginTop: 20 }}>Revenue warehouse</div>
      <div style={{ fontSize: 40, color: "#94a3b8", marginTop: 40 }}>Revenue Pipeline · dbt + DuckDB + Docker</div>
    </div>
  </AbsoluteFill>
);

export const MyComposition = () => (
  <>
    <Composition id="RevenuePipeline" component={Video} durationInFrames={TOTAL} fps={FPS} width={1920} height={1080} />
    <Still id="Thumbnail" component={Thumb} width={1920} height={1080} />
  </>
);
