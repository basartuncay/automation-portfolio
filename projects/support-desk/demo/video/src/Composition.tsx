import { AbsoluteFill, Composition, Img, OffthreadVideo, Sequence, Still, interpolate, staticFile, useCurrentFrame } from "remotion";

const FPS = 30;
const s = (sec: number) => Math.round(sec * FPS);

// Scene times come from demo/out/markers.json (seconds into each real recording).
type Cap = { from: number; text: string };
type Clip = { file: string; trim: number; end: number; who: string; caps: Cap[] };

const CLIPS: Clip[] = [
  {
    file: "agent.webm", trim: 3.5, end: 43.8, who: "Signed in as: agent",
    caps: [
      { from: 3.5, text: "The support queue starts empty" },
      { from: 6.56, text: "6 customer emails reach the support inbox (real SMTP → IMAP)" },
      { from: 9.96, text: "Each email is triaged in seconds: 2 drafts, 2 held for a person, spam closed, an invoice sent to finance" },
      { from: 14.96, text: "The draft quotes only the company's docs, and every sentence shows its source" },
      { from: 23.2, text: "The agent approves it, and it goes out as a threaded reply to the customer" },
      { from: 27.17, text: "Prompt injection detected: no AI draft, a person has to read it" },
      { from: 32.86, text: "Urgent ticket: only a lead can send this reply" },
      { from: 38.99, text: "Questions the docs couldn't answer become a list of what to write next" },
    ],
  },
  {
    file: "lead.webm", trim: 0, end: 14.5, who: "Signed in as: lead",
    caps: [
      { from: 0, text: "A lead signs in" },
      { from: 3.66, text: "The lead approves the urgent reply" },
      { from: 9.5, text: "All handled. Nothing went out without a person approving it" },
    ],
  },
];

const INTRO = s(4), OUTRO = s(5);
const clipLen = (c: Clip) => s(c.end - c.trim);
const TOTAL = INTRO + CLIPS.reduce((n, c) => n + clipLen(c), 0) + OUTRO;

const BG = "#0b1220", ACCENT = "#6366f1", FONT = "Inter, Helvetica, Arial, sans-serif";

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
      <div style={{ position: "absolute", left: 186, top: 320, background: ACCENT, color: "white", fontWeight: 700, fontSize: 24, padding: "8px 16px", borderRadius: 8 }}>{clip.who}</div>
      <div style={{ position: "absolute", left: 220, right: 220, bottom: 90, display: "flex", justifyContent: "center" }}>
        <div key={cap.from} style={{ background: "rgba(15,23,42,.92)", color: "white", fontSize: 40, fontWeight: 600, padding: "18px 30px", borderRadius: 12, borderLeft: `8px solid ${ACCENT}`, lineHeight: 1.3 }}>{cap.text}</div>
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
          <Title kicker="Support Desk · real recording" title="An AI support inbox with a human in the loop"
            sub="Emails are triaged, answered from your own docs with sources, and sent only after a person approves. One docker compose up." />
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
            sub="Python · FastAPI · Claude API (citations, structured outputs) · Docker Compose · e2e in GitHub Actions. Recorded with the offline engine: add an API key and Claude writes the drafts." />
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
      <div style={{ color: ACCENT, fontSize: 40, fontWeight: 800, letterSpacing: 3 }}>AI + AUTOMATION</div>
      <div style={{ fontSize: 110, fontWeight: 900, lineHeight: 1.0, marginTop: 20 }}>Support email → cited draft → human sends</div>
      <div style={{ fontSize: 40, color: "#94a3b8", marginTop: 40 }}>Support Desk · Python + Claude + Docker</div>
    </div>
  </AbsoluteFill>
);

export const MyComposition = () => (
  <>
    <Composition id="SupportDesk" component={Video} durationInFrames={TOTAL} fps={FPS} width={1920} height={1080} />
    <Still id="Thumbnail" component={Thumb} width={1920} height={1080} />
  </>
);
