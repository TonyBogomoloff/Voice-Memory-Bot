import React, {CSSProperties, ReactNode} from 'react';
import {Img, interpolate, staticFile} from 'remotion';
import {loadFont} from '@remotion/fonts';

loadFont({family: 'Android', url: staticFile('fonts/Roboto-Variable.ttf'), weight: '100 900'});
export const UI = {bar: '#202d3b', background: '#0f1b26', incoming: '#233343', outgoing: '#31597d', text: '#f3f6f8', blue: '#91caf3', dim: '#99acbd'};
const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;
export const ramp = (f: number, a: number, b: number) => interpolate(f, [a, b], [0, 1], clamp);

export const Icon: React.FC<{name: string; size?: number; color?: string}> = ({name, size = 24, color = UI.text}) => {
  const paths: Record<string, ReactNode> = {
    back: <path d="M20 12H4m7-7-7 7 7 7"/>,
    more: <><circle cx="12" cy="5" r="1.2" fill={color}/><circle cx="12" cy="12" r="1.2" fill={color}/><circle cx="12" cy="19" r="1.2" fill={color}/></>,
    mic: <><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0014 0v-2M12 19v3M9 22h6"/></>,
    play: <path d="M8 5v14l12-7Z" fill={color} stroke="none"/>,
    checks: <path d="m2 12 4 4L16 6M10 14l2 2L22 6"/>,
    check: <path d="m4 12 5 5L20 6"/>,
    folder: <path d="M3 6a2 2 0 012-2h5l2 3h7a2 2 0 012 2v10a2 2 0 01-2 2H5a2 2 0 01-2-2Z"/>,
    spark: <><path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4Z"/><path d="m20 2 .5 1.5L22 4l-1.5.5L20 6l-.5-1.5L18 4l1.5-.5Z"/></>,
    file: <><path d="M6 3h8l4 4v14H6Z"/><path d="M14 3v5h4M9 12h6M9 16h6"/></>,
    export: <><path d="M12 3v12m-5-5 5 5 5-5M4 15v6h16v-6"/></>,
    arrow: <path d="M4 12h16m-6-6 6 6-6 6"/>,
    plus: <path d="M12 4v16M4 12h16"/>,
    menu: <path d="M4 6h16M4 12h16M4 18h16"/>,
    clip: <path d="m8 15 7-7c4-4 8 1 4 5l-9 9c-5 4-11-2-7-6L14 5c2-2 5 1 3 3L7 18c-1 1-2-1-1-2l8-8"/>,
    smile: <><circle cx="12" cy="12" r="9"/><path d="M7.5 14.5q4.5 6 9 0M8 8h.1M16 8h.1"/></>,
    search: <><circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/></>,
    photo: <><rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="8" cy="9" r="2"/><path d="m4 17 5-5 4 4 3-3 5 5"/></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{flexShrink: 0}}>{paths[name]}</svg>;
};

const Wallpaper: React.FC = () => <svg width="100%" height="100%" style={{position: 'absolute', inset: 0, opacity: 0.17}}>
  <defs><pattern id="memory-wallpaper" width="150" height="158" patternUnits="userSpaceOnUse"><g fill="none" stroke="#476273" strokeWidth="1.4" transform="scale(.83)">
    <path d="M25 27c-8-13-23-1-14 10l14 12 15-12c10-12-8-24-15-10Z"/><path d="m111 10 29 10-21 20-3-14-13-6 37 0"/>
    <path d="M60 88h39v27H78l-12 9 2-9h-8zM68 96h22M68 103h17"/><path d="m144 113 4 9 10 1-8 7 2 10-8-5-9 5 2-10-7-7 10-1Z"/>
    <path d="M25 157q12-24 24 0t24 0"/><circle cx="154" cy="73" r="5"/><circle cx="31" cy="102" r="3"/>
  </g></pattern></defs><rect width="100%" height="100%" fill="url(#memory-wallpaper)"/>
</svg>;

const Key: React.FC<{children: ReactNode; selected?: boolean; style?: CSSProperties}> = ({children, selected = false, style}) => <div style={{height: 39, flex: 1, minWidth: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6, borderRadius: 7, fontSize: 16, background: selected ? '#397aad' : '#283a4a', boxShadow: '0 2px 0 #0a1722', color: UI.text, ...style}}>{children}</div>;
export const FolderKeys: React.FC<{active: string; folders?: string[][]}> = ({active, folders = [['Идеи', 'Работа'], ['Личное', 'Поездки']]}) => <>
  {folders.map((row, i) => <div key={i} style={{display: 'flex', gap: 6}}>{row.map(name => <Key key={name} selected={active === name}><Icon name={active === name ? 'check' : 'folder'} size={17} color={active === name ? '#d7efff' : '#79baff'}/>{name}</Key>)}</div>)}
  <div style={{display: 'flex', gap: 6}}><Key><Icon name="plus" size={19}/></Key><Key><Icon name="folder" size={17} color="#79baff"/>Папка</Key><Key style={{fontSize: 14}}><Icon name="menu" size={17} color="#79baff"/>Настройки</Key></div>
</>;
export const ExportKeys: React.FC<{selected?: boolean}> = ({selected}) => <>
  <div style={{display: 'flex'}}><Key selected={selected}>Всё: PDF + файлы</Key></div>
  <div style={{display: 'flex', gap: 6}}><Key style={{fontSize: 14}}>Только тексты · PDF</Key><Key style={{fontSize: 14}}>Только аудио</Key></div>
  <div style={{display: 'flex', gap: 6}}><Key style={{fontSize: 14}}>Формат текста</Key><Key><Icon name="back" size={16}/>Назад</Key></div>
</>;

// The 360 x 800 viewport stays at a 9:20 ratio; it is never stretched.
export const Phone: React.FC<{children: ReactNode; active?: string; keyboard?: ReactNode; keyboardHeight?: number; recording?: boolean; style?: CSSProperties}> = ({children, active = 'Идеи', keyboard, keyboardHeight = 136, recording = false, style}) => <div style={{position: 'absolute', left: 180, top: 310, width: 360, height: 800, borderRadius: 31, overflow: 'hidden', outline: '2px solid #536b7b', boxShadow: '0 20px 45px #0008', fontFamily: 'Android, sans-serif', color: UI.text, background: UI.background, ...style}}>
  <div style={{height: 25, background: UI.bar, padding: '0 18px', display: 'flex', alignItems: 'center', gap: 6, fontSize: 12}}><span>17:42</span><span style={{marginLeft: 'auto', fontSize: 10}}>4G</span><svg width="17" height="12" viewBox="0 0 20 16"><path d="M1 16h3v-4H1zm5 0h3V8H6zm5 0h3V4h-3zm5 0h3V0h-3z" fill={UI.text}/></svg><svg width="22" height="13" viewBox="0 0 27 18"><rect x="1" y="3" width="21" height="12" rx="2" fill="none" stroke={UI.text}/><path d="M24 6v6" stroke={UI.text} strokeWidth="2"/><rect x="3" y="5" width="15" height="8" rx="1" fill={UI.text}/></svg></div>
  <div style={{height: 59, background: UI.bar, display: 'flex', alignItems: 'center', padding: '0 12px', gap: 10, position: 'relative', zIndex: 1}}><Icon name="back" size={23}/><Img src={staticFile('bot-avatar-v2.jpg')} style={{width: 35, height: 35, borderRadius: '50%'}}/><div><div style={{fontSize: 18, fontWeight: 500}}>Voice Memory Bot</div><div style={{fontSize: 13, color: UI.dim, marginTop: 2}}>бот</div></div><div style={{marginLeft: 'auto'}}><Icon name="more" size={22}/></div></div>
  <div style={{position: 'absolute', left: 0, right: 0, top: 84, bottom: keyboardHeight + 67, overflow: 'hidden'}}><Wallpaper/><div style={{position: 'absolute', top: 11, left: 0, right: 0, textAlign: 'center'}}><span style={{fontSize: 12, background: '#294355c9', padding: '4px 10px', borderRadius: 18, color: '#d2e0e9'}}>Сегодня</span></div>{children}</div>
  <div style={{position: 'absolute', bottom: keyboardHeight + 15, left: 7, right: 7, height: 43, display: 'flex', gap: 6, alignItems: 'center'}}><div style={{height: 43, background: UI.bar, borderRadius: 24, padding: '0 11px', display: 'flex', alignItems: 'center', gap: 9, flex: 1, minWidth: 0}}><Icon name="smile" size={22} color={UI.dim}/>{recording ? <><span style={{width: 7, height: 7, borderRadius: '50%', background: '#ff9292'}}/><span style={{fontSize: 16}}>Запись…</span></> : <span style={{fontSize: 16, color: UI.dim, display: 'flex', gap: 5, alignItems: 'center'}}><Icon name="folder" size={16} color={UI.dim}/>{active}</span>}<span style={{marginLeft: 'auto', display: 'flex'}}><Icon name="clip" size={21} color={UI.dim}/></span></div><div style={{width: 43, height: 43, borderRadius: '50%', background: recording ? '#3375a6' : '#578fbd', display: 'flex', alignItems: 'center', justifyContent: 'center'}}><Icon name="mic"/></div></div>
  <div style={{position: 'absolute', left: 6, right: 6, bottom: 15, display: 'flex', flexDirection: 'column', gap: 6, padding: 4, background: '#162532'}}>{keyboard ?? <FolderKeys active={active}/>}</div>
  <div style={{position: 'absolute', bottom: 5, left: '50%', transform: 'translateX(-50%)', width: 98, height: 3, borderRadius: 4, background: '#becad1'}}/>
</div>;

export const Bubble: React.FC<{children: ReactNode; top: number; right?: boolean; width?: number; stamp?: string; style?: CSSProperties; fontSize?: number}> = ({children, top, right = false, width = 310, stamp = '17:42', style, fontSize = 22}) => <div data-bubble style={{position: 'absolute', top, ...(right ? {right: 11} : {left: 11}), width, boxSizing: 'border-box', padding: '10px 12px 7px', borderRadius: right ? '15px 15px 4px 15px' : '15px 15px 15px 4px', background: right ? UI.outgoing : UI.incoming, fontSize, lineHeight: 1.26, ...style}}><div style={{whiteSpace: 'pre-wrap', overflowWrap: 'break-word'}}>{children}</div><div style={{display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 4, fontSize: 11, color: right ? '#b3cce0' : '#99aebf', marginTop: 4, height: 14}}>{stamp}{right && <Icon name="checks" size={16} color="#82c5f4"/>}</div><svg width="7" height="10" viewBox="0 0 9 12" style={{position: 'absolute', bottom: 0, ...(right ? {right: -4, transform: 'scaleX(-1)'} : {left: -4})}}><path d="M9 0v12H0Q7 8 9 0" fill={right ? UI.outgoing : UI.incoming}/></svg></div>;

export const Waveform: React.FC<{frame: number}> = ({frame}) => <div style={{display: 'flex', alignItems: 'center', gap: 11, height: 43}}><div style={{width: 37, height: 37, borderRadius: '50%', background: '#abd7f7', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0}}><Icon name="play" color="#1b3040" size={24}/></div><div><div style={{display: 'flex', gap: 3, alignItems: 'center', height: 24}}>{Array.from({length: 29}, (_, i) => <div key={i} style={{width: 3, height: 4 + Math.sin(i * 2.07) ** 2 * 19, borderRadius: 3, background: '#b9ddf6', opacity: 0.88 + Math.sin(frame / 15 + i) * 0.1}}/>)}</div><div style={{color: UI.blue, fontSize: 11, marginTop: 2}}>0:08</div></div></div>;

export const PhotoBubble: React.FC<{top: number; width?: number; height?: number; right?: boolean}> = ({top, width = 310, height = 232, right = false}) => <div data-photo-bubble style={{position: 'absolute', top, ...(right ? {right: 11} : {left: 11}), width, height, padding: 3, boxSizing: 'border-box', borderRadius: right ? '15px 15px 4px 15px' : '15px 15px 15px 4px', overflow: 'hidden', background: right ? UI.outgoing : UI.incoming}}>
  <Img src={staticFile('trip-lighthouse-v2.png')} style={{display: 'block', width: '100%', height: '100%', objectFit: 'cover', borderRadius: right ? '12px 12px 2px 12px' : '12px 12px 12px 2px'}}/>
  <div style={{position: 'absolute', bottom: 9, right: 10, display: 'flex', alignItems: 'center', gap: 4, padding: '2px 6px', borderRadius: 9, background: '#07192570', color: '#fff', fontSize: 11}}>17:39{right && <Icon name="checks" size={15} color="#c4eaff"/>}</div>
</div>;

export const Reply: React.FC<{children: ReactNode; title?: string}> = ({children, title = 'Voice Memory Bot'}) => <div style={{padding: '6px 8px', borderLeft: '3px solid #8cc8ef', background: '#ffffff0a', borderRadius: 4, marginBottom: 9, fontSize: 14, lineHeight: 1.3}}><div style={{color: '#a9dcff', fontWeight: 500, marginBottom: 3}}>{title}</div><div style={{whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis'}}>{children}</div></div>;

export const Tap: React.FC<{frame: number; from: number; x: number; y: number}> = ({frame, from, x, y}) => {const p = ramp(frame, from, from + 22); return <div style={{position: 'absolute', left: x, top: y, width: 44, height: 44, borderRadius: '50%', border: '3px solid #c4e7ff', background: '#98d2ff22', transform: `translate(-50%, -50%) scale(${0.45 + p * 1.3})`, opacity: frame < from ? 0 : (1 - p) * 0.9, pointerEvents: 'none'}}/>;};
