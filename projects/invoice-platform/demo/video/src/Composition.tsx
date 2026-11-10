import { AbsoluteFill, Composition, OffthreadVideo, Sequence, Still, interpolate, staticFile, useCurrentFrame } from "remotion";

const FPS = 30;
const s = (sec: number) => Math.round(sec * FPS);

// Scene times come from demo/out/markers.json (seconds into each real recording).
type Cap = { from: number; text: string };
type Clip = { file: string; trim: number; end: number; who: string; caps: Cap[] };

const CLIPS: Clip[] = [
  {
    file: "approver.webm", trim: 4.0, end: 33.5, who: "Signed in as: approver",
    caps: [
      { from: 4.0, text: "The approver's invoice queue, before any email arrives" },
      { from: 8.07, text: "5 supplier emails land in the AP inbox (real SMTP → IMAP)" },
      { from: 11.77, text: "The intake worker filed 4 invoices. The email with no PDF was skipped" },
      { from: 15.78, text: "A scanned PDF was read by OCR and flagged so a human double-checks the numbers" },
      { from: 22.87, text: "The approver approves it" },
      { from: 28.24, text: "Four-eyes rule: whoever approved it gets no \"Mark paid\" button" },
    ],
  },
  {
    file: "payer.webm", trim: 0, end: 19.6, who: "Signed in as: payer",
    caps: [
      { from: 0, text: "A second person signs in as the payer" },
      { from: 5.37, text: "The payer marks the invoice paid" },
      { from: 10.71, text: "Append-only history: intake bot → approver → payer, with field diffs" },
    ],
  },
];

const INTRO = s(4), OUTRO = s(5);
const clipLen = (c: Clip) => s(c.end - c.trim);
const TOTAL = INTRO + CLIPS.reduce((n, c) => n + clipLen(c), 0) + OUTRO;

const BG = "#0b1220", ACCENT = "#3b82f6", FONT = "Inter, Helvetica, Arial, sans-serif";

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
          <Title kicker="Invoice Platform · real recording" title="From supplier email to approved payment"
            sub="Email intake → PDF/OCR extraction → approval panel with roles, four-eyes payments and an audit trail. One docker compose up." />
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
            sub="Python · Laravel 13 + Filament 5 · Tesseract OCR · Docker Compose · verified end to end in GitHub Actions" />
        </Fade>
      </Sequence>
    </AbsoluteFill>
  );
};

export const Thumb: React.FC = () => (
  <AbsoluteFill style={{ background: BG, fontFamily: FONT }}>
    <div style={{ position: "absolute", right: -80, top: 120, width: 1100, height: 688, borderRadius: 18, overflow: "hidden", transform: "rotate(-3deg)", boxShadow: "0 30px 80px rgba(0,0,0,.6)" }}>
      <OffthreadVideo src={staticFile("approver.webm")} trimBefore={s(13)} muted style={{ width: 1100, height: 688 }} />
    </div>
    <div style={{ position: "absolute", left: 90, top: 200, width: 820, color: "white" }}>
      <div style={{ color: ACCENT, fontSize: 40, fontWeight: 800, letterSpacing: 3 }}>AUTOMATION</div>
      <div style={{ fontSize: 110, fontWeight: 900, lineHeight: 1.0, marginTop: 20 }}>Email → OCR → Approved payment</div>
      <div style={{ fontSize: 40, color: "#94a3b8", marginTop: 40 }}>Invoice Platform · Python + Laravel + Docker</div>
    </div>
  </AbsoluteFill>
);

export const MyComposition = () => (
  <>
    <Composition id="InvoicePlatform" component={Video} durationInFrames={TOTAL} fps={FPS} width={1920} height={1080} />
    <Still id="Thumbnail" component={Thumb} width={1920} height={1080} />
  </>
);
