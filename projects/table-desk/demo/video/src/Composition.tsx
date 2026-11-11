import { AbsoluteFill, Composition, Img, OffthreadVideo, Sequence, Still, interpolate, staticFile, useCurrentFrame } from "remotion";
import demo from "./demo.json";

// demo.json is written by demo/record.py: the real recording's file, where to trim it, and captions timed to the
// moments the recorder marked while it drove the app.
type Cap = { from: number; text: string };
type Demo = {
  file: string; trim: number; end: number; who: string; caps: Cap[]; accent: string;
  kicker: string; title: string; sub: string; outro: string; thumbKicker: string; thumbTitle: string; thumbSub: string;
};
const D = demo as Demo;

const FPS = 30;
const s = (sec: number) => Math.round(sec * FPS);
const INTRO = s(4), OUTRO = s(5), CLIP = s(D.end - D.trim);
const TOTAL = INTRO + CLIP + OUTRO;
const BG = "#0b1220", FONT = "Inter, Helvetica, Arial, sans-serif";

const Fade: React.FC<{ len: number; children: React.ReactNode }> = ({ len, children }) => {
  const f = useCurrentFrame();
  const o = interpolate(f, [0, 10, len - 10, len], [0, 1, 1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return <AbsoluteFill style={{ opacity: o }}>{children}</AbsoluteFill>;
};

const Title: React.FC<{ kicker: string; title: string; sub: string; size?: number }> = ({ kicker, title, sub, size = 92 }) => (
  <AbsoluteFill style={{ background: BG, color: "white", fontFamily: FONT, justifyContent: "center", padding: 140 }}>
    <div style={{ color: D.accent, fontSize: 34, fontWeight: 700, letterSpacing: 3, textTransform: "uppercase" }}>{kicker}</div>
    <div style={{ fontSize: size, fontWeight: 800, lineHeight: 1.05, marginTop: 24, maxWidth: 1500 }}>{title}</div>
    <div style={{ fontSize: 38, color: "#94a3b8", marginTop: 36, maxWidth: 1500, lineHeight: 1.35 }}>{sub}</div>
  </AbsoluteFill>
);

const ClipScene: React.FC = () => {
  const t = D.trim + useCurrentFrame() / FPS;
  const cap = [...D.caps].reverse().find((c) => t >= c.from) ?? D.caps[0];
  return (
    <AbsoluteFill style={{ background: BG, fontFamily: FONT }}>
      <div style={{ position: "absolute", left: 160, top: 24, width: 1600, height: 1000, borderRadius: 14, overflow: "hidden", boxShadow: "0 20px 60px rgba(0,0,0,.5)" }}>
        <OffthreadVideo src={staticFile(D.file)} trimBefore={s(D.trim)} muted style={{ width: 1600, height: 1000 }} />
      </div>
      <div style={{ position: "absolute", left: 160, top: 1032, color: "#94a3b8", fontWeight: 600, fontSize: 22 }}>{D.who}</div>
      <div style={{ position: "absolute", left: 220, right: 220, bottom: 90, display: "flex", justifyContent: "center" }}>
        <div key={cap.from} style={{ background: "rgba(15,23,42,.92)", color: "white", fontSize: 40, fontWeight: 600, padding: "18px 30px", borderRadius: 12, borderLeft: `8px solid ${D.accent}`, lineHeight: 1.3 }}>{cap.text}</div>
      </div>
    </AbsoluteFill>
  );
};

export const Video: React.FC = () => (
  <AbsoluteFill style={{ background: BG }}>
    <Sequence durationInFrames={INTRO}><Fade len={INTRO}><Title kicker={D.kicker} title={D.title} sub={D.sub} /></Fade></Sequence>
    <Sequence from={INTRO} durationInFrames={CLIP}><ClipScene /></Sequence>
    <Sequence from={INTRO + CLIP} durationInFrames={OUTRO}>
      <Fade len={OUTRO}><Title kicker="Source, tests and CI" title="github.com/basartuncay/automation-portfolio" size={76} sub={D.outro} /></Fade>
    </Sequence>
  </AbsoluteFill>
);

export const Thumb: React.FC = () => (
  <AbsoluteFill style={{ background: BG, fontFamily: FONT }}>
    <div style={{ position: "absolute", right: -80, top: 120, width: 1100, height: 688, borderRadius: 18, overflow: "hidden", transform: "rotate(-3deg)", boxShadow: "0 30px 80px rgba(0,0,0,.6)" }}>
      <Img src={staticFile("queue.png")} style={{ width: 1100, height: 688 }} />
    </div>
    <div style={{ position: "absolute", left: 90, top: 200, width: 820, color: "white" }}>
      <div style={{ color: D.accent, fontSize: 40, fontWeight: 800, letterSpacing: 3 }}>{D.thumbKicker}</div>
      <div style={{ fontSize: 104, fontWeight: 900, lineHeight: 1.0, marginTop: 20 }}>{D.thumbTitle}</div>
      <div style={{ fontSize: 40, color: "#94a3b8", marginTop: 40 }}>{D.thumbSub}</div>
    </div>
  </AbsoluteFill>
);

export const MyComposition = () => (
  <>
    <Composition id="Demo" component={Video} durationInFrames={TOTAL} fps={FPS} width={1920} height={1080} />
    <Still id="Thumbnail" component={Thumb} width={1920} height={1080} />
  </>
);
