import React, {CSSProperties, ReactNode} from 'react';
import {AbsoluteFill, Img, Sequence, spring, staticFile, useCurrentFrame} from 'remotion';
import {loadFont} from '@remotion/fonts';
import {Bubble, ExportKeys, Icon, Phone, Reply, Tap, UI, Waveform, ramp} from './phone';
import {FPS, SCENES} from './timing';
import copy from './demo-copy.json';

for (const [weight, file] of [['400', 'Regular'], ['600', 'SemiBold'], ['700', 'Bold']] as const) {
  loadFont({family: 'Plex', url: staticFile(`fonts/IBMPlexSans-${file}.ttf`), weight});
}
const C = {background: '#10191f', text: '#f0f4f1', dim: '#a5b8c2', line: '#334852', blue: '#77baff', mint: '#75e4c6', violet: '#c1adff', gold: '#f1cf8c'};
const pop = (f: number, delay = 0) => spring({frame: f - delay, fps: FPS, config: {damping: 19, stiffness: 135, mass: 0.8}});
const appear = (f: number, delay = 0): CSSProperties => ({opacity: ramp(f, delay, delay + 9), transform: `translateY(${(1 - pop(f, delay)) * 22}px)`});
const end = (f: number, duration: number): CSSProperties => ({opacity: 1 - ramp(f, duration - 9, duration)});

const Background: React.FC = () => {
  const f = useCurrentFrame();
  return <AbsoluteFill style={{backgroundColor: C.background}}>
    <AbsoluteFill style={{background: 'radial-gradient(ellipse at 90% 25%, #193c5570, transparent 57%), radial-gradient(ellipse at 10% 76%, #1c453e65, transparent 56%)'}}/>
    <AbsoluteFill style={{opacity: 0.15, backgroundImage: 'radial-gradient(#91b5c5 0.7px, transparent 0.7px)', backgroundSize: '25px 25px', transform: `translateY(${f / 40}px)`}}/>
    <div style={{position: 'absolute', left: 68, top: 360, width: 584, height: 584, border: '1px solid #6cacc321', borderRadius: '50%', transform: `scale(${1 + Math.sin(f / 90) * 0.02})`}}/>
    <div style={{position: 'absolute', bottom: 28, left: 44, right: 44, display: 'flex', gap: 8}}>{SCENES.map(scene => <div key={scene.id} style={{height: 3, flex: 1, background: '#334a54', borderRadius: 4, overflow: 'hidden'}}><div style={{height: '100%', width: `${ramp(f, scene.from, scene.from + scene.duration) * 100}%`, background: scene.color}}/></div>)}</div>
  </AbsoluteFill>;
};

const Brand: React.FC = () => <div style={{position: 'absolute', top: 42, left: 44, right: 44, display: 'flex', alignItems: 'center', gap: 12}}><Img src={staticFile('bot-avatar.png')} style={{width: 43, height: 43, borderRadius: 14, border: `1px solid ${C.line}`}}/><div style={{fontSize: 17, letterSpacing: 1.8, fontWeight: 600}}>VOICE MEMORY BOT</div><div style={{marginLeft: 'auto', color: C.dim, fontSize: 14, letterSpacing: 1.2}}>В TELEGRAM</div></div>;
const Tag: React.FC<{children: ReactNode; color?: string}> = ({children, color = C.blue}) => <div style={{display: 'inline-flex', alignItems: 'center', gap: 8, padding: '8px 13px', borderRadius: 11, background: `${color}10`, border: `1px solid ${color}38`, color, fontSize: 16, fontWeight: 600, letterSpacing: 1.3}}>{children}</div>;
const Title: React.FC<{number: string; label: string; icon: string; color: string; children: ReactNode; fontSize?: number}> = ({number, label, icon, color, children, fontSize = 55}) => {const f = useCurrentFrame();return <div style={{position: 'absolute', left: 44, right: 44, top: 123, ...appear(f)}}><div style={{display: 'flex', gap: 14, alignItems: 'center', marginBottom: 18}}><Tag color={color}><Icon name={icon} size={21} color={color}/>{number} / 04 · {label}</Tag><div style={{height: 1, flex: 1, background: C.line}}/></div><div style={{fontSize, fontWeight: 700, lineHeight: 1.04, letterSpacing: -1.8}}>{children}</div></div>;};
const Footer: React.FC<{frame: number; delay: number; color: string; children: ReactNode}> = ({frame, delay, color, children}) => <div style={{position: 'absolute', top: 1142, left: 44, right: 44, display: 'flex', gap: 13, alignItems: 'center', ...appear(frame, delay)}}><div style={{width: 38, height: 38, borderRadius: '50%', background: `${color}13`, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0}}><Icon name="check" size={23} color={color}/></div><div style={{fontSize: 25, lineHeight: 1.3, fontWeight: 600}}>{children}</div></div>;
const Aside: React.FC<{frame: number; delay: number; color: string; side?: 'left' | 'right'; top: number; label: string; children: ReactNode}> = ({frame, delay, color, side = 'left', top, label, children}) => <div style={{position: 'absolute', top, left: side === 'left' ? 32 : 560, width: 128, textAlign: side === 'left' ? 'right' : 'left', ...appear(frame, delay)}}><div style={{fontSize: 16, lineHeight: 1.25, color: C.dim, marginBottom: 9}}>{label}</div><div style={{fontSize: 25, lineHeight: 1.15, color, fontWeight: 600, letterSpacing: -0.6}}>{children}</div><div style={{height: 1, width: 28, background: `${color}85`, marginTop: 14, marginLeft: side === 'left' ? 'auto' : 0}}/></div>;

const Hero: React.FC<{frame: number; top: number; size: number}> = ({frame, top, size}) => <div style={{position: 'absolute', left: (720 - size) / 2, top, width: size, height: size, opacity: ramp(frame, 1, 10), transform: `translateY(${(1 - pop(frame, 2)) * 35 + Math.sin(frame / 25) * 5}px) rotate(${Math.sin(frame / 40) * 2}deg)`}}><Img src={staticFile('recorder-cutout-v1.png')} style={{width: '100%', height: '100%', objectFit: 'contain', filter: 'drop-shadow(0 20px 18px #0006)'}}/></div>;
const FeatureChip: React.FC<{frame: number; delay: number; icon: string; label: string; color: string; left: number; top: number; angle?: number}> = ({frame, delay, icon, label, color, left, top, angle = 0}) => <div style={{position: 'absolute', left, top, display: 'flex', gap: 10, alignItems: 'center', padding: '14px 18px', border: `1.5px solid ${color}70`, borderRadius: 20, background: '#1b2e39', boxShadow: '0 7px 0 #0a151b', fontSize: 24, fontWeight: 600, opacity: ramp(frame, delay, delay + 9), transform: `translateY(${(1 - pop(frame, delay)) * 30}px) rotate(${angle}deg)`}}><Icon name={icon} size={27} color={color}/>{label}</div>;

const Intro: React.FC = () => {const f = useCurrentFrame();return <AbsoluteFill style={end(f, 90)}>
  <div style={{position: 'absolute', left: 44, right: 44, top: 150}}><Tag>ТВОЯ ПАМЯТЬ. ПРЯМО В ЧАТЕ.</Tag><h1 style={{margin: '28px 0 0', fontSize: 74, lineHeight: 1.02, letterSpacing: -2.8, fontWeight: 700}}>Хорошие мысли<br/><span style={{color: C.blue}}>не теряются.</span></h1></div>
  <Hero frame={f} top={385} size={620}/>
  <FeatureChip frame={f} delay={8} icon="mic" label="Голос" color={C.blue} left={44} top={516} angle={-7}/>
  <FeatureChip frame={f} delay={14} icon="folder" label="Папки" color={C.mint} left={490} top={641} angle={7}/>
  <FeatureChip frame={f} delay={20} icon="spark" label="ИИ" color={C.violet} left={66} top={815} angle={-5}/>
  <FeatureChip frame={f} delay={26} icon="export" label="Экспорт" color={C.gold} left={460} top={901} angle={4}/>
  <div style={{position: 'absolute', left: 44, right: 44, top: 1060, ...appear(f, 20)}}><div style={{fontSize: 30, fontWeight: 600}}>Говори. Сохраняй. Возвращайся.</div><div style={{fontSize: 22, color: C.dim, marginTop: 12}}>Заметки, которые всегда под рукой.</div></div>
</AbsoluteFill>;};

const Voice: React.FC = () => {const f = useCurrentFrame();return <AbsoluteFill style={end(f, 180)}>
  <Title number="01" label="ГОЛОС В ТЕКСТ" icon="mic" color={C.blue}>Говори сейчас.<br/><span style={{color: C.blue}}>Читай когда удобно.</span></Title>
  <Phone style={appear(f, 3)} recording={f < 18}>
    <Bubble top={50} width={270} right style={appear(f, 18)}><Waveform frame={f}/></Bubble>
    {f < 49 && <Bubble top={150} width={294} fontSize={20} style={{opacity: ramp(f, 27, 34) * (1 - ramp(f, 43, 49))}}>Конвертирую в текст…</Bubble>}
    <Bubble top={150} fontSize={24} style={appear(f, 49)}>{copy.voice}</Bubble>
  </Phone>
  <Aside frame={f} delay={58} top={605} label="Уже текстом" color={C.blue}>Не нужно<br/>слушать</Aside>
  <Aside frame={f} delay={75} top={805} side="right" label="В архиве" color={C.mint}>Оригинал<br/>сохранён</Aside>
  <Footer frame={f} delay={55} color={C.blue}>Голосовые и кружки<br/><span style={{color: C.blue}}>превращаются в текст.</span></Footer>
</AbsoluteFill>;};

const Folders: React.FC = () => {const f = useCurrentFrame();const switched = f >= 39;const p = ramp(f, 30, 45);return <AbsoluteFill style={end(f, 150)}>
  <Title number="02" label="СВОИ ПАПКИ" icon="folder" color={C.mint}>У каждой идеи<br/><span style={{color: C.mint}}>своё место.</span></Title>
  <Phone active={switched ? 'Поездка' : 'Идеи'} style={appear(f, 3)}>
    <div style={{opacity: 1 - p, transform: `translateX(${-p * 35}px)`}}><Bubble top={54} width={300} fontSize={24}>{copy.idea}</Bubble></div>
    <div style={{opacity: p, transform: `translateX(${(1 - p) * 35}px)`}}><Bubble top={54} fontSize={24}>{copy.voice}</Bubble></div>
  </Phone>
  <Tap frame={f} from={26} x={272} y={978}/>
  <Aside frame={f} delay={49} top={640} color={C.mint} label="Поездка">Всё<br/>на месте</Aside>
  <Aside frame={f} delay={64} top={952} side="right" color={C.blue} label="Один тап">Другая<br/>папка</Aside>
  <Footer frame={f} delay={43} color={C.mint}>Голосовые, тексты и фото —<br/><span style={{color: C.mint}}>по твоим проектам.</span></Footer>
</AbsoluteFill>;};

const AI: React.FC = () => {const f = useCurrentFrame();return <AbsoluteFill style={end(f, 210)}>
  <Title number="03" label="ПОМОЩЬ ИИ" icon="spark" color={C.violet}>Была мысль.<br/><span style={{color: C.violet}}>Стал готовый план.</span></Title>
  <Phone style={appear(f, 3)}>
    <Bubble top={46} width={310} fontSize={20}>{copy.voice}</Bubble>
    <Bubble top={167} width={304} right fontSize={22} style={appear(f, 22)}><Reply>{copy.voice}</Reply>{copy.ai_prompt}</Bubble>
    {f < 70 && <Bubble top={298} width={150} fontSize={20} style={{opacity: ramp(f, 44, 49) * (1 - ramp(f, 64, 70))}}>Думаю…</Bubble>}
    <Bubble top={298} width={322} fontSize={21} style={appear(f, 70)}><Reply>{copy.voice}</Reply>{copy.ai_answer}</Bubble>
  </Phone>
  <Aside frame={f} delay={31} top={590} color={C.violet} label="Ответь на запись">И добавь<br/>просьбу</Aside>
  <Aside frame={f} delay={81} top={817} side="right" color={C.mint} label="Результат">Тоже<br/>в архиве</Aside>
  <Footer frame={f} delay={78} color={C.violet}>ИИ помогает с твоими заметками.<br/><span style={{color: C.violet}}>Ответ сохраняется рядом.</span></Footer>
</AbsoluteFill>;};

const ZipFile: React.FC = () => <div style={{display: 'flex', alignItems: 'center', gap: 12}}><div style={{width: 44, height: 53, borderRadius: 9, background: '#5085ad', display: 'flex', alignItems: 'center', justifyContent: 'center'}}><Icon name="file" size={29}/></div><div style={{minWidth: 0}}><div style={{fontSize: 17, fontWeight: 500}}>voice-memory-all-001.zip</div><div style={{fontSize: 14, color: UI.dim, marginTop: 5}}>ZIP · PDF и оригиналы</div></div></div>;
const Export: React.FC = () => {const f = useCurrentFrame();return <AbsoluteFill style={end(f, 180)}>
  <Title number="04" label="ЭКСПОРТ" icon="export" color={C.gold}>Твои записи.<br/><span style={{color: C.gold}}>Всегда с тобой.</span></Title>
  <Phone style={appear(f, 3)} keyboard={<ExportKeys selected={f >= 28 && f < 61}/>}>
    <Bubble top={49} width={318} fontSize={22}>Экспорт папки «Поездка»<div style={{fontSize: 18, marginTop: 12, lineHeight: 1.35}}>Тексты: PDF, вся история папки в одном файле. Папки проектов сохранятся.</div></Bubble>
    {f < 61 && <Bubble top={216} width={185} fontSize={20} style={{opacity: ramp(f, 32, 38) * (1 - ramp(f, 55, 61))}}>Готовлю ZIP…</Bubble>}
    <Bubble top={216} width={322} fontSize={18} style={appear(f, 61)}><ZipFile/><div style={{marginTop: 12}}>PDF + файлы · ZIP</div></Bubble>
  </Phone>
  <Tap frame={f} from={23} x={360} y={978}/>
  <Aside frame={f} delay={74} top={654} color={C.gold} label="В одном ZIP">Тексты<br/>и файлы</Aside>
  <Aside frame={f} delay={92} top={824} side="right" color={C.blue} label="Формат текста">PDF или<br/>Markdown</Aside>
  <Footer frame={f} delay={71} color={C.gold}>Тексты — PDF или Markdown.<br/><span style={{color: C.gold}}>Оригиналы — рядом, в ZIP.</span></Footer>
</AbsoluteFill>;};

const Outro: React.FC = () => {const f = useCurrentFrame();return <AbsoluteFill>
  <div style={{position: 'absolute', left: 44, right: 44, top: 153, ...appear(f)}}><Tag color={C.mint}>ЗАПОМНИТЬ СТАЛО ПРОЩЕ</Tag><div style={{fontSize: 69, fontWeight: 700, lineHeight: 1.03, letterSpacing: -2.5, marginTop: 27}}>Свободная голова.<br/><span style={{color: C.blue}}>Всё под рукой.</span></div></div>
  <Hero frame={f} top={365} size={555}/>
  <div style={{position: 'absolute', left: 44, right: 44, top: 951, display: 'flex', gap: 8, ...appear(f, 7)}}>{[['mic', 'Голос', C.blue], ['folder', 'Папки', C.mint], ['spark', 'ИИ', C.violet], ['export', 'Экспорт', C.gold]].map(([icon, label, color]) => <div key={label} style={{flex: 1, padding: '14px 5px 12px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, background: '#21343c', border: `1px solid ${color}33`, borderRadius: 15, fontSize: 19}}><Icon name={icon} size={22} color={color}/>{label}</div>)}</div>
  <div style={{position: 'absolute', left: 44, right: 44, top: 1063, padding: '23px 20px', background: C.blue, color: '#102434', borderRadius: 21, boxShadow: '0 6px 0 #2d6889', display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 16, fontSize: 31, fontWeight: 700, ...appear(f, 11)}}>Начни с одной мысли<Icon name="arrow" size={30} color="#102434"/></div>
  <div style={{position: 'absolute', left: 0, right: 0, top: 1180, textAlign: 'center', color: C.dim, fontSize: 21, ...appear(f, 18)}}>Просто отправь голосовое в Telegram.</div>
</AbsoluteFill>;};

export const VoiceMemoryIntro: React.FC = () => {
  const scenes = [Intro, Voice, Folders, AI, Export, Outro];
  return <AbsoluteFill style={{fontFamily: 'Plex, sans-serif', color: C.text}}><Background/>{SCENES.map((scene, i) => {const Scene = scenes[i];return <Sequence key={scene.id} from={scene.from} durationInFrames={scene.duration}><Scene/></Sequence>;})}<Brand/></AbsoluteFill>;
};
