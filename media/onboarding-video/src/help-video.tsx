import React, {CSSProperties, ReactNode} from 'react';
import {AbsoluteFill, Img, staticFile, useCurrentFrame} from 'remotion';
import {loadFont} from '@remotion/fonts';
import {Bubble, FolderKeys as SharedFolderKeys, Icon, Phone as SharedPhone, PhotoBubble, Reply, Tap, UI, Waveform, ramp} from './phone';

loadFont({family: 'Plex', url: staticFile('fonts/IBMPlexSans-Regular.ttf'), weight: '400'});
loadFont({family: 'Plex', url: staticFile('fonts/IBMPlexSans-SemiBold.ttf'), weight: '600'});
loadFont({family: 'Plex', url: staticFile('fonts/IBMPlexSans-Bold.ttf'), weight: '700'});
const C = {blue: '#77baff', mint: '#75e4c6', violet: '#c1adff', gold: '#f1cf8c'};
// Help examples keep their own fictional folders when the main promo changes.
const FolderKeys: React.FC<{active: string}> = ({active}) => <SharedFolderKeys active={active} folders={[["Поездка", "Идеи"], ["Работа", "Личное"]]}/>;
const Phone: React.FC<React.ComponentProps<typeof SharedPhone>> = ({active = 'Поездка', keyboard, ...props}) => <SharedPhone {...props} active={active} keyboard={keyboard ?? <FolderKeys active={active}/>}/>;

export const HELP_DEMOS = {
  save: {label: 'СОХРАНИТЬ ЗАПИСЬ', title: ['Скажи мысль.', 'Сохрани текст.'], color: C.blue, icon: 'mic', steps: ['Выбери папку', 'Отправь голосовое', 'Читай расшифровку'], times: [0, 45, 125], note: 'Оригинал тоже останется в архиве.'},
  folders: {label: 'ПАПКИ И ПОРЯДОК', title: ['Новая тема?', 'Создай папку.'], color: C.mint, icon: 'folder', steps: ['Нажми «＋»', 'Напиши название', 'Отправляй записи сюда'], times: [0, 70, 170], note: 'Галочка показывает выбранную папку.'},
  ai: {label: 'РАБОТА С ИИ', title: ['Много мыслей?', 'Собери план.'], color: C.violet, icon: 'spark', steps: ['Ответь на запись: /ai', 'Напиши задачу', 'Забери готовый результат'], times: [0, 135, 210], note: 'Ответ ИИ тоже сохранится в архиве.'},
  images: {label: 'КАРТИНКИ С ИИ', title: ['Покажи фото.', 'Спроси у ИИ.'], color: C.violet, icon: 'photo', steps: ['Ответь на фото: /ai', 'Задай вопрос', 'Прочитай ответ'], times: [0, 135, 210], note: 'Так же можно попросить прочитать текст.'},
  search: {label: 'НАЙТИ ЗАПИСЬ', title: ['Помнишь слово?', 'Найдёшь запись.'], color: C.blue, icon: 'search', steps: ['Напиши /search маяк', 'Нажми на результат', 'Вот нужная запись'], times: [0, 85, 175], note: 'Поиск работает по всему архиву.'},
  actions: {label: 'ПЕРЕНОС И УДАЛЕНИЕ', title: ['Попало не туда?', 'Перенеси запись.'], color: C.mint, icon: 'folder', steps: ['Ответь на запись: /move', 'Выбери папку', 'Готово, запись перенесена'], times: [0, 100, 190], note: 'Удалить: /delete → подтвердить.'},
  export: {label: 'СКАЧАТЬ АРХИВ', title: ['Все записи —', 'в одном ZIP.'], color: C.gold, icon: 'export', steps: ['Отправь /export', 'Выбери, что скачать', 'Сохрани ZIP себе'], times: [0, 70, 195], note: 'Внутри — тексты и оригиналы файлов.'},
  settings: {label: 'НАСТРОИТЬ БОТА', title: ['Нужны кнопки?', 'Включи их.'], color: C.blue, icon: 'menu', steps: ['Открой «Настройки»', 'Включи кнопки действий', 'Они появятся у новых записей'], times: [0, 70, 185], note: 'Остальные настройки — /settings.'},
} as const;
export type HelpTopic = keyof typeof HELP_DEMOS;
const enter = (f: number, from: number): CSSProperties => ({opacity: ramp(f, from, from + 10), transform: `translateY(${(1 - ramp(f, from, from + 12)) * 14}px)`});
const Show: React.FC<{from: number; until?: number; children: ReactNode}> = ({from, until = 300, children}) => {
  const f = useCurrentFrame();
  return f < from || f >= until ? null : <div style={enter(f, from)}>{children}</div>;
};
const Keys: React.FC<{rows: string[][]; selected?: string; small?: boolean}> = ({rows, selected, small}) => <>{rows.map((row, i) => <div key={i} style={{display: 'flex', gap: 6}}>{row.map(label => <div key={label} style={{flex: 1, minWidth: 0, height: small ? 34 : 39, borderRadius: 7, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '0 4px', boxSizing: 'border-box', fontSize: small ? 15 : 16, background: label === selected ? '#397aad' : '#283a4a', boxShadow: '0 2px 0 #0a1722', color: UI.text, whiteSpace: 'nowrap'}}>{label}</div>)}</div>)}</>;
const nav = ['← Назад', '⌂ Главная'];
const note = 'В субботу — к маяку.\nВзять воду и зарядить камеру.';
const Inline: React.FC<{top: number; rows: string[][]; selected?: string}> = ({top, rows, selected}) => <div style={{position: 'absolute', left: 11, width: 310, top, display: 'flex', flexDirection: 'column', gap: 5}}><Keys rows={rows} selected={selected} small/></div>;

const Save: React.FC = () => {const f = useCurrentFrame(); return <>
  <Phone recording={f >= 22 && f < 48}>
    <Show from={0} until={48}><Bubble top={49} fontSize={21}>Папка «Поездка»</Bubble></Show>
    <Show from={48}><Bubble top={49} right width={280}><Waveform frame={f}/></Bubble></Show>
    <Show from={88} until={125}><Bubble top={157} width={284} fontSize={20}>Конвертирую в текст…</Bubble></Show>
    <Show from={125}><Bubble top={157} fontSize={24}>{note}</Bubble></Show>
  </Phone><Tap frame={f} from={8} x={272} y={978}/><Tap frame={f} from={25} x={512} y={937}/>
</>;};
const Folders: React.FC = () => {const f = useCurrentFrame();const naming = f >= 45 && f < 170; return <>
  <Phone active={f < 170 ? 'Идеи' : 'Поездка'} keyboardHeight={naming ? 47 : 137} keyboard={naming ? <Keys rows={[["Отмена"]]}/> : f < 170 ? <Keys rows={[["✓ Идеи", "Работа"], ["Личное"], ["＋", "Папка", "Настройки"]]}/> : <FolderKeys active="Поездка"/>}>
    <Show from={45} until={170}><Bubble top={49} fontSize={22}>Как назовём папку?</Bubble></Show>
    <Show from={95} until={170}><Bubble top={144} right width={186}>Поездка</Bubble></Show>
    <Show from={170}><Bubble top={49} fontSize={22}>Папка «Поездка»</Bubble></Show>
    <Show from={210}><Bubble top={153} right fontSize={24}>В субботу — к маяку.</Bubble></Show>
  </Phone><Tap frame={f} from={30} x={246} y={1067}/>
</>;};
const AI: React.FC = () => <Phone>
  <Bubble top={46} right fontSize={22}>{note}</Bubble>
  <Show from={38} until={135}><Bubble top={182} right fontSize={22}><Reply title="Ты">{note}</Reply>/ai</Bubble></Show>
  <Show from={90} until={135}><Bubble top={311} fontSize={21}>Что сделать с записью?</Bubble></Show>
  <Show from={135}><Bubble top={193} right fontSize={22}>Собери список дел</Bubble></Show>
  <Show from={170} until={210}><Bubble top={280} width={150} fontSize={21}>Думаю…</Bubble></Show>
  <Show from={210}><Bubble top={280} fontSize={22}>План на субботу:<br/><br/>1. Взять воду.<br/>2. Зарядить камеру.<br/>3. Поехать к маяку.</Bubble></Show>
</Phone>;
const Images: React.FC = () => {const f = useCurrentFrame(); return <Phone>
  <PhotoBubble top={45} height={f < 135 ? 200 : 170} right/>
  <Show from={38} until={135}><Bubble top={260} right fontSize={22}><Reply title="Ты">Фото</Reply>/ai</Bubble></Show>
  <Show from={90} until={135}><Bubble top={386} fontSize={21}>Что сделать с записью?</Bubble></Show>
  <Show from={135}><Bubble top={235} right width={260} fontSize={22}>Что на фото?</Bubble></Show>
  <Show from={175} until={210}><Bubble top={322} width={150} fontSize={21}>Думаю…</Bubble></Show>
  <Show from={210}><Bubble top={322} fontSize={23}>Маяк на скалистом берегу. Океан и закат.</Bubble></Show>
</Phone>;};
const Search: React.FC = () => {const f = useCurrentFrame();const results = f >= 70 && f < 175; return <>
  <Phone keyboard={results ? <Keys rows={[["Выходные у маяка"], ["Другой поиск"], nav]}/> : undefined}>
    <Show from={20} until={175}><Bubble top={49} right width={260} fontSize={23}>/search маяк</Bubble></Show>
    <Show from={70} until={175}><Bubble top={145} fontSize={22}>Найденные записи:</Bubble></Show>
    <Show from={175}><Bubble top={49} fontSize={24}>{note}</Bubble></Show>
  </Phone><Tap frame={f} from={150} x={360} y={978}/>
</>;};
const Actions: React.FC = () => {const f = useCurrentFrame(); return <>
  <Phone active="Идеи">
    <Show from={0} until={190}><Bubble top={45} right fontSize={22}>В субботу — к маяку.</Bubble></Show>
    <Show from={40} until={190}><Bubble top={139} right fontSize={22}><Reply title="Ты">В субботу — к маяку.</Reply>/move</Bubble></Show>
    <Show from={100} until={190}><Bubble top={269} fontSize={20}>В какую папку ты хочешь переместить запись?</Bubble><Inline top={362} rows={[["Поездка", "Работа"], ["＋ Новая папка"], ["Отмена"]]} selected={f >= 165 ? 'Поездка' : undefined}/></Show>
    <Show from={190}><Bubble top={49} fontSize={24}>Перенесено в «Поездка».</Bubble></Show>
  </Phone><Tap frame={f} from={165} x={268} y={774}/>
</>;};
const exportRows = [['Исходники: медиа, тексты и файлы'], ['Всё: PDF + файлы'], ['Только тексты · PDF', 'Только аудио'], ['Формат текста'], nav];
const Export: React.FC = () => {const f = useCurrentFrame(); return <>
  <Phone keyboardHeight={f >= 65 && f < 195 ? 227 : 137} keyboard={f >= 65 && f < 195 ? <Keys rows={exportRows} selected={f >= 140 ? 'Всё: PDF + файлы' : undefined}/> : undefined}>
    <Show from={15} until={195}><Bubble top={47} right width={170}>/export</Bubble></Show>
    <Show from={65} until={155}><Bubble top={140} fontSize={22}>Экспорт всего архива</Bubble></Show>
    <Show from={155} until={195}><Bubble top={140} width={220} fontSize={22}>Готовлю ZIP…</Bubble></Show>
    <Show from={195}><Bubble top={49} width={322} fontSize={20}><div style={{display: 'flex', gap: 11, alignItems: 'center'}}><div style={{background: '#5085ad', padding: 10, borderRadius: 10}}><Icon name="file" size={28}/></div><div><div style={{fontSize: 16}}>voice-memory-all-001.zip</div><div style={{fontSize: 14, color: UI.dim, marginTop: 6}}>ZIP · PDF и оригиналы</div></div></div><div style={{marginTop: 15}}>PDF + файлы · ZIP</div></Bubble></Show>
  </Phone><Tap frame={f} from={139} x={360} y={933}/>
</>;};
const Settings: React.FC = () => {const f = useCurrentFrame();const menu = f >= 45 && f < 185; return <>
  <Phone keyboard={menu ? <Keys rows={[[f < 115 ? 'Отправлять кнопки действий' : 'Скрывать кнопки действий'], ['Помощь'], nav]}/> : undefined}>
    <Show from={45} until={115}><Bubble top={49} fontSize={22}>Настройки · Поездка</Bubble></Show>
    <Show from={115} until={185}><Bubble top={49} fontSize={22}>Кнопки под сообщениями включены.</Bubble></Show>
    <Show from={185}><Bubble top={49} fontSize={24}>В субботу — к маяку.</Bubble><Inline top={147} rows={[["Переместить", "Удалить", "ИИ"]]}/></Show>
  </Phone><Tap frame={f} from={27} x={473} y={1067}/><Tap frame={f} from={102} x={360} y={978}/><Tap frame={f} from={157} x={445} y={1067}/>
</>;};
const scenes = {save: Save, folders: Folders, ai: AI, images: Images, search: Search, actions: Actions, export: Export, settings: Settings};
export const HelpVideo: React.FC<{topic: HelpTopic}> = ({topic}) => {
  const f = useCurrentFrame(); const info = HELP_DEMOS[topic]; const Scene = scenes[topic];
  const step = f >= info.times[2] ? 2 : f >= info.times[1] ? 1 : 0;
  return <AbsoluteFill style={{fontFamily: 'Plex, sans-serif', color: '#f0f4f1', background: '#10191f'}}>
    <AbsoluteFill style={{backgroundImage: 'radial-gradient(circle at 15% 55%, #24425b80, transparent 48%), radial-gradient(circle at 90% 18%, #223d3555, transparent 38%), radial-gradient(#ffffff09 1px, transparent 1px)', backgroundSize: 'auto, auto, 24px 24px'}}/>
    <div style={{position: 'absolute', top: 37, left: 44, display: 'flex', alignItems: 'center', gap: 13}}><Img src={staticFile('bot-avatar-v2.jpg')} style={{width: 42, height: 42, borderRadius: 13}}/><div style={{fontSize: 19, fontWeight: 600, letterSpacing: 1}}>VOICE MEMORY BOT</div></div>
    <div style={{position: 'absolute', right: 45, top: 48, color: '#a5b8c2', fontSize: 17}}>10 секунд</div>
    <div style={{position: 'absolute', top: 111, left: 44, right: 35}}><div style={{display: 'flex', alignItems: 'center', gap: 10, color: info.color, fontSize: 19, fontWeight: 600, letterSpacing: 1.2}}><Icon name={info.icon} size={22} color={info.color}/>{info.label}</div><div style={{fontSize: 53, lineHeight: 1.08, letterSpacing: -1.5, marginTop: 15, fontWeight: 700}}>{info.title[0]}<br/><span style={{color: info.color}}>{info.title[1]}</span></div></div>
    <Scene/>
    <div style={{position: 'absolute', top: 1140, left: 35, right: 35, textAlign: 'center'}}><div style={{display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 11, fontSize: 25, fontWeight: 600}}><span style={{width: 31, height: 31, background: info.color, color: '#102434', borderRadius: '50%', fontSize: 20, display: 'inline-flex', alignItems: 'center', justifyContent: 'center'}}>{step + 1}</span>{info.steps[step]}</div><div style={{marginTop: 14, color: '#a5b8c2', fontSize: 20}}>{f >= info.times[2] ? info.note : 'Пример использования · время обработки сокращено'}</div></div>
    <div style={{position: 'absolute', bottom: 26, left: 44, right: 44, height: 4, display: 'flex', gap: 8}}>{info.times.map((time, i) => <div key={time} style={{flex: 1, background: '#293b46', borderRadius: 3, overflow: 'hidden'}}><div style={{height: '100%', width: `${ramp(f, time, info.times[i + 1] ?? 299) * 100}%`, background: info.color}}/></div>)}</div>
  </AbsoluteFill>;
};
