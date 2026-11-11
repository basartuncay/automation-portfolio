import { AbsoluteFill, Composition, Img, OffthreadVideo, Sequence, Still, interpolate, staticFile, useCurrentFrame } from "remotion";

const FPS = 30;
const s = (sec: number) => Math.round(sec * FPS);

// Scene times come from demo/out/markers.json (seconds into each real recording).
type Cap = { from: number; text: string };
type Clip = { file: string; trim: number; end: number; who: string; hideWhoFrom?: number; caps: Cap[] };

const CLIPS: Clip[] = [
  {
    file: "watch.webm", trim: 2.24, end: 51.45, who: "Pricing team", hideWhoFrom: 45.04,
    caps: [
      { from: 2.24, text: "Every product we sell, against 2 competitors: right now we're the cheapest on all 12" },
      { from: 6.64, text: "Competitors change prices. \"Crawl now\" (normally it runs on a schedule)" },
      { from: 9.53, text: "VoltMart now undercuts us on headphones. A rival's charger went out of stock" },
      { from: 14.6, text: "Price history per competitor, against our price (dashed)" },
      { from: 20.54, text: "Alerts only for products we sell, and each one is also sent to Slack ✓" },
      { from: 25.4, text: "A competitor redesigns its site: the crawl finds 0 products..." },
      { from: 31.09, text: "...so it's marked suspicious and nothing is changed: no flood of false \"removed\" alerts" },
      { from: 41.15, text: "Weekly digest, on schedule or on demand" },
      { from: 45.17, text: "The digest email as it arrived: what's undercut, by how much, and crawl health" },
    ],
  },
];

const INTRO = s(4), OUTRO = s(5);
const clipLen = (c: Clip) => s(c.end - c.trim);
const TOTAL = INTRO + CLIPS.reduce((n, c) => n + clipLen(c), 0) + OUTRO;

const BG = "#8a0019", ACCENT = "#ffffff", FONT = "'Avenir Next Condensed', 'Arial Narrow', sans-serif";

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
      {t < (clip.hideWhoFrom ?? Infinity) && <div style={{ position: "absolute", left: 186, top: 320, background: ACCENT, color: BG, fontWeight: 700, fontSize: 24, padding: "8px 16px", borderRadius: 8 }}>{clip.who}</div>}
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
          <Title kicker="Price Watch · real recording" title="Know the moment a competitor undercuts you"
            sub="Scheduled competitor crawls → price history → alerts in Slack/Telegram → weekly digest email, in one panel. One docker compose up." />
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
            sub="Python · httpx · FastAPI · SQLite · Slack/Telegram · Docker Compose · e2e in GitHub Actions. The competitor shops in this demo are local stand-ins, so it never scrapes a real site." />
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
    <div style={{ position: "absolute", left: 90, top: 200, width: 740, color: "white" }}>
      <div style={{ color: ACCENT, fontSize: 40, fontWeight: 800, letterSpacing: 3 }}>SCRAPING + AUTOMATION</div>
      <div style={{ fontSize: 110, fontWeight: 900, lineHeight: 1.0, marginTop: 20 }}>Competitor price tracking</div>
      <div style={{ fontSize: 40, color: "#94a3b8", marginTop: 40 }}>Price Watch · Python + Slack + Docker</div>
    </div>
  </AbsoluteFill>
);

export const MyComposition = () => (
  <>
    <Composition id="PriceWatch" component={Video} durationInFrames={TOTAL} fps={FPS} width={1920} height={1080} />
    <Still id="Thumbnail" component={Thumb} width={1920} height={1080} />
  </>
);
