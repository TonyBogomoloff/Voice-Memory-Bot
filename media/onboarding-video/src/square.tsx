import React, {CSSProperties, ReactNode} from 'react';
import {AbsoluteFill, Img, Sequence, spring, staticFile, useCurrentFrame} from 'remotion';
import {loadFont} from '@remotion/fonts';
import {Bubble, ExportKeys, Icon, Phone, PhotoBubble, Reply, Tap, UI, Waveform, ramp} from './phone';
import {FPS, SCENES} from './timing';
import copy from './demo-copy.json';

for (const [weight, file] of [['400', 'Regular'], ['600', 'SemiBold'], ['700', 'Bold']] as const) {
  loadFont({family: 'Plex', url: staticFile(`fonts/IBMPlexSans-${file}.ttf`), weight});
}
const C = {text: '#f0f4f1', dim: '#a5b8c2', blue: '#77baff', mint: '#75e4c6', violet: '#c1adff', gold: '#f1cf8c'};
const enter = (f: number, delay = 0): CSSProperties => ({
  opacity: ramp(f, delay, delay + 10),
  transform: `translateY(${(1 - spring({frame: f - delay, fps: FPS, config: {damping: 20, stiffness: 130}})) * 24}px)`,
});
const fade = (f: number, duration: number) => ({opacity: 1 - ramp(f, duration - 9, duration)});

const Background: React.FC = () => {
  const f = useCurrentFrame();
  return <AbsoluteFill style={{background: '#10191f'}}>
    <AbsoluteFill style={{background: 'radial-gradient(ellipse at 90% 30%, #193c5580, transparent 60%), radial-gradient(ellipse at 10% 85%, #1c453e70, transparent 60%)'}}/>
    <AbsoluteFill style={{opacity: .13, backgroundImage: 'radial-gradient(#91b5c5 .8px, transparent .8px)', backgroundSize: '28px 28px'}}/>
    <div style={{position: 'absolute', left: 64, right: 64, bottom: 64, display: 'flex', gap: 10}}>{SCENES.map(s => <div key={s.id} style={{flex: 1, height: 4, background: '#334a54', borderRadius: 4, overflow: 'hidden'}}><div style={{height: '100%', width: `${ramp(f, s.from, s.from + s.duration) * 100}%`, background: s.color}}/></div>)}</div>
  </AbsoluteFill>;
};
const Brand: React.FC = () => <div style={{position: 'absolute', top: 112, left: 64, width: 500, display: 'flex', alignItems: 'center', gap: 14}}>
  <Img src={staticFile('bot-avatar-v2.jpg')} style={{width: 48, height: 48, borderRadius: 14}}/>
  <div style={{fontSize: 23, fontWeight: 600, letterSpacing: 1.3}}>VOICE MEMORY BOT</div>
</div>;
const Label: React.FC<{children: ReactNode; color: string}> = ({children, color}) => <div style={{display: 'inline-flex', padding: '10px 14px', borderRadius: 12, background: `${color}10`, border: `1px solid ${color}40`, fontSize: 21, letterSpacing: 1, fontWeight: 600, color}}>{children}</div>;

// Scale the phone and its tap cues together, preserving the original UI proportions.
const Device: React.FC<{children: ReactNode; active?: string; recording?: boolean; keyboard?: ReactNode; tap?: {frame: number; from: number; x: number; y: number}}> = ({children, active, recording, keyboard, tap}) => {
  const f = useCurrentFrame();
  return <div style={{position: 'absolute', left: 608, top: 48, width: 360, height: 800, ...enter(f, 3), transform: `${enter(f, 3).transform} scale(1.18)`, transformOrigin: 'top left'}}>
    <Phone active={active} recording={recording} keyboard={keyboard} style={{left: 0, top: 0}}>{children}</Phone>
    {tap && <Tap {...tap}/>}
  </div>;
};
const Copy: React.FC<{label: string; color: string; title: ReactNode; quote: ReactNode; note: ReactNode; delay: number}> = ({label, color, title, quote, note, delay}) => {
  const f = useCurrentFrame();
  return <div style={{position: 'absolute', left: 64, top: 202, width: 500}}>
    <div style={enter(f)}><Label color={color}>{label}</Label>
      <div style={{marginTop: 29, fontSize: 68, lineHeight: 1.08, letterSpacing: -2, fontWeight: 700}}>{title}</div>
    </div>
    <div style={{marginTop: 43, padding: '23px 25px', borderLeft: `4px solid ${color}`, background: `${color}0d`, borderRadius: '0 18px 18px 0', fontSize: 34, lineHeight: 1.3, ...enter(f, delay)}}>{quote}</div>
    <div style={{marginTop: 32, display: 'flex', alignItems: 'flex-start', gap: 13, fontSize: 30, lineHeight: 1.4, color: C.dim, ...enter(f, delay + 9)}}><Icon name="check" size={30} color={color}/><span>{note}</span></div>
  </div>;
};
const Recorder: React.FC = () => {
  const f = useCurrentFrame();
  return <Img src={staticFile('blue-recorder-cutout-v2.png')} style={{position: 'absolute', left: 534, top: 220, width: 496, height: 496, objectFit: 'contain', filter: 'drop-shadow(0 22px 22px #0007)', opacity: ramp(f, 2, 12), transform: `translateY(${Math.sin(f / 25) * 5}px) rotate(${Math.sin(f / 40) * 2}deg)`}}/>;
};
const Chips: React.FC = () => <div style={{position: 'absolute', left: 64, right: 64, top: 845, display: 'flex', gap: 14}}>{[['mic', 'Голос', C.blue], ['folder', 'Папки', C.mint], ['spark', 'ИИ', C.violet], ['export', 'Экспорт', C.gold]].map(([icon, label, color]) => <div key={label} style={{flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 13, height: 76, background: '#20343e', border: `1px solid ${color}40`, borderRadius: 20, fontSize: 28, fontWeight: 600}}><Icon name={icon} size={30} color={color}/>{label}</div>)}</div>;

const Intro: React.FC = () => {
  const f = useCurrentFrame();
  return <AbsoluteFill style={fade(f, 90)}>
    <Recorder/>
    <div style={{position: 'absolute', left: 64, top: 224, width: 500, ...enter(f)}}>
      <Label color={C.blue}>МЕСТО ДЛЯ ТВОИХ ИДЕЙ</Label>
      <h1 style={{fontSize: 80, lineHeight: 1.06, letterSpacing: -2.5, margin: '33px 0 30px'}}>Пришла идея?<br/><span style={{color: C.blue}}>Сохрани её.</span></h1>
      <div style={{fontSize: 33, lineHeight: 1.4, color: C.dim}}>Твой голосовой блокнот,<br/>который всегда с тобой.</div>
    </div>
    <div style={{position: 'absolute', left: 64, top: 722, fontSize: 32, ...enter(f, 18)}}>Голосом, текстом или фото.</div>
    <Chips/>
  </AbsoluteFill>;
};
const Voice: React.FC = () => {
  const f = useCurrentFrame();
  return <AbsoluteFill style={fade(f, 180)}>
    <Copy label="01 / 04 · СОХРАНИ" color={C.blue} title={<>Мысль пришла?<br/><span style={{color: C.blue}}>Надиктуй.</span></>} quote={<>«Идея: короткие истории<br/>о местах. Начну с маяка»</>} note={<>Мысль станет текстом.<br/>Голосовое останется рядом.</>} delay={49}/>
    <Device recording={f < 18}>
      <PhotoBubble top={45} width={300} height={176} right/>
      <Bubble top={236} width={270} right style={enter(f, 18)}><Waveform frame={f}/></Bubble>
      {f < 49 && <Bubble top={329} width={294} fontSize={20} style={{opacity: ramp(f, 27, 34) * (1 - ramp(f, 43, 49))}}>Конвертирую в текст…</Bubble>}
      <Bubble top={329} fontSize={24} style={enter(f, 49)}>{copy.voice}</Bubble>
    </Device>
  </AbsoluteFill>;
};
const Folders: React.FC = () => {
  const f = useCurrentFrame(), p = ramp(f, 30, 45);
  return <AbsoluteFill style={fade(f, 150)}>
    <Copy label="02 / 04 · РАЗЛОЖИ" color={C.mint} title={<>У каждой идеи<br/><span style={{color: C.mint}}>своё место.</span></>} quote={<>Идеи, наброски и фото —<br/>по твоим темам.</>} note={<>Один тап по папке —<br/>и всё снова перед тобой.</>} delay={43}/>
    <Device active={f >= 39 ? 'Идеи' : 'Работа'} tap={{frame: f, from: 26, x: 92, y: 668}}>
      <div style={{opacity: 1 - p, transform: `translateX(${-p * 35}px)`}}><Bubble top={54} width={300} fontSize={24}>{copy.idea}</Bubble></div>
      <div style={{opacity: p, transform: `translateX(${(1 - p) * 35}px)`}}><PhotoBubble top={54}/><Bubble top={301} fontSize={24}>{copy.voice}</Bubble></div>
    </Device>
  </AbsoluteFill>;
};
const AI: React.FC = () => {
  const f = useCurrentFrame();
  return <AbsoluteFill style={fade(f, 270)}>
    <Copy label="03 / 04 · РАЗВИВАЙ" color={C.violet} title={<>Развивай идею.<br/><span style={{color: C.violet}}>Вместе с ИИ.</span></>} quote={<>Ответь на запись:<br/>«Помоги развить идею»</>} note={<>Из мысли — в замысел.<br/>Ответ тоже сохранится.</>} delay={22}/>
    <Device>
      <Bubble top={46} width={310} fontSize={20}>{copy.voice}</Bubble>
      <Bubble top={167} width={304} right fontSize={22} style={enter(f, 22)}><Reply>{copy.voice}</Reply>{copy.ai_prompt}</Bubble>
      {f < 70 && <Bubble top={298} width={150} fontSize={20} style={{opacity: ramp(f, 44, 49) * (1 - ramp(f, 64, 70))}}>Думаю…</Bubble>}
      <Bubble top={298} width={322} fontSize={21} style={enter(f, 70)}><Reply>{copy.voice}</Reply>{copy.ai_answer}</Bubble>
    </Device>
  </AbsoluteFill>;
};
const Export: React.FC = () => {
  const f = useCurrentFrame();
  return <AbsoluteFill style={fade(f, 120)}>
    <Copy label="04 / 04 · ЗАБЕРИ" color={C.gold} title={<>Собранные идеи.<br/><span style={{color: C.gold}}>Всегда с тобой.</span></>} quote={<>Тексты в PDF.<br/>Голосовые и фото рядом.</>} note={<>Всё в одном ZIP —<br/>для тебя и твоих проектов.</>} delay={45}/>
    <Device keyboard={<ExportKeys selected={f >= 20 && f < 45}/>} tap={{frame: f, from: 15, x: 180, y: 668}}>
      <Bubble top={49} width={318} fontSize={22}>Экспорт папки «Идеи»<div style={{fontSize: 18, marginTop: 12, lineHeight: 1.35}}>Тексты: PDF, вся история папки в одном файле. Папки проектов сохранятся.</div></Bubble>
      {f < 45 && <Bubble top={216} width={185} fontSize={20} style={{opacity: ramp(f, 23, 28) * (1 - ramp(f, 39, 45))}}>Готовлю ZIP…</Bubble>}
      <Bubble top={216} width={322} fontSize={18} style={enter(f, 45)}><div style={{display: 'flex', gap: 12, alignItems: 'center'}}><div style={{width: 44, height: 53, background: '#5085ad', borderRadius: 9, display: 'flex', alignItems: 'center', justifyContent: 'center'}}><Icon name="file" size={29}/></div><div><div style={{fontSize: 17}}>voice-memory-all-001.zip</div><div style={{fontSize: 14, color: UI.dim, marginTop: 5}}>ZIP · PDF и оригиналы</div></div></div><div style={{marginTop: 12}}>PDF + файлы · ZIP</div></Bubble>
    </Device>
  </AbsoluteFill>;
};
const Outro: React.FC = () => {
  const f = useCurrentFrame();
  return <AbsoluteFill>
    <Recorder/>
    <div style={{position: 'absolute', left: 64, top: 224, width: 500, ...enter(f)}}><Label color={C.mint}>ТВОЁ МЕСТО ДЛЯ ИДЕЙ</Label><div style={{fontSize: 72, lineHeight: 1.08, letterSpacing: -2.3, fontWeight: 700, marginTop: 33}}>Идеи приходят.<br/><span style={{color: C.blue}}>Теперь<br/>остаются.</span></div></div>
    <div style={{position: 'absolute', left: 64, top: 674, width: 490, height: 84, borderRadius: 22, background: C.blue, color: '#102434', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 14, fontSize: 33, fontWeight: 700, ...enter(f, 11)}}>Сохрани первую идею<Icon name="arrow" size={30} color="#102434"/></div>
    <Chips/>
  </AbsoluteFill>;
};
export const VoiceMemorySquare: React.FC = () => {
  const scenes = [Intro, Voice, Folders, AI, Export, Outro];
  return <AbsoluteFill style={{fontFamily: 'Plex, sans-serif', color: C.text}}><Background/>{SCENES.map((scene, i) => {
    const Scene = scenes[i];
    return <Sequence key={scene.id} from={scene.from} durationInFrames={scene.duration}><Scene/></Sequence>;
  })}<Brand/></AbsoluteFill>;
};
